"""Per-user custom Semgrep rules with a deliberately small YAML surface."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import sqlite3
from typing import Any
from uuid import uuid4

import yaml

from code_quality.service import CATEGORIES, AssessmentError, _store_path

MAX_RULE_BYTES = 16_000
MAX_RULES_PER_DOCUMENT = 20
MAX_RULES_PER_USER = 100
MAX_ACTIVE_RULE_BYTES = 128_000
ALLOWED_KEYS = {"id", "message", "severity", "languages", "pattern", "patterns", "pattern-either", "pattern-regex", "metadata"}
ALLOWED_METADATA = {"category", "confidence", "technology"}
RULE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
SUPPORTED_SEMGREP_LANGUAGES = {
    "python", "javascript", "typescript", "php", "java", "kotlin", "go", "rust", "ruby",
    "csharp", "c", "cpp", "swift", "scala", "bash", "sql", "html", "css", "vue", "svelte",
    "dart", "elixir", "lua", "r", "pl", "erlang", "clojure", "terraform", "generic",
}


def validate_rule_yaml(document: Any) -> list[dict[str, Any]]:
    if not isinstance(document, str) or not document.strip() or len(document.encode("utf-8")) > MAX_RULE_BYTES:
        raise AssessmentError("Enter a non-empty rule document under 16 KB.")
    try:
        data = yaml.safe_load(document)
    except yaml.YAMLError as exc:
        raise AssessmentError("Rule YAML is invalid. Check indentation and YAML syntax.") from exc
    if not isinstance(data, dict) or set(data) != {"rules"} or not isinstance(data["rules"], list):
        raise AssessmentError("Rule YAML must contain only a top-level rules list.")
    if not 1 <= len(data["rules"]) <= MAX_RULES_PER_DOCUMENT:
        raise AssessmentError("A rule document must contain between 1 and 20 rules.")
    validated = []
    seen_ids: set[str] = set()
    for index, rule in enumerate(data["rules"], start=1):
        if not isinstance(rule, dict) or set(rule) - ALLOWED_KEYS:
            raise AssessmentError(f"Rule {index} contains unsupported fields.")
        rule_id = rule.get("id")
        languages = rule.get("languages")
        category = (rule.get("metadata") or {}).get("category") if isinstance(rule.get("metadata"), dict) else None
        patterns = [key for key in ("pattern", "patterns", "pattern-either", "pattern-regex") if key in rule]
        if not isinstance(rule_id, str) or not RULE_ID.fullmatch(rule_id):
            raise AssessmentError(f"Rule {index} needs a short identifier using letters, numbers, dots, underscores, or hyphens.")
        if rule_id in seen_ids:
            raise AssessmentError(f"Rule identifier {rule_id} is duplicated in this rule set.")
        seen_ids.add(rule_id)
        if not isinstance(rule.get("message"), str) or not 1 <= len(rule["message"]) <= 500:
            raise AssessmentError(f"Rule {index} needs a message of at most 500 characters.")
        if rule.get("severity") not in {"INFO", "WARNING", "ERROR"}:
            raise AssessmentError(f"Rule {index} severity must be INFO, WARNING, or ERROR.")
        if not isinstance(languages, list) or not 1 <= len(languages) <= 20 or any(item not in SUPPORTED_SEMGREP_LANGUAGES for item in languages):
            raise AssessmentError(f"Rule {index} needs one or more supported Semgrep language names.")
        if category not in CATEGORIES or not isinstance(rule.get("metadata"), dict) or set(rule["metadata"]) - ALLOWED_METADATA:
            raise AssessmentError(f"Rule {index} metadata.category must be one of: {', '.join(CATEGORIES)}.")
        if len(patterns) != 1:
            raise AssessmentError(f"Rule {index} must use exactly one of pattern, patterns, or pattern-either.")
        if "metadata" in rule and set(rule["metadata"]) - ALLOWED_METADATA:
            raise AssessmentError(f"Rule {index} metadata contains unsupported fields.")
        pattern_value = rule[patterns[0]]
        if patterns[0] in {"pattern", "pattern-regex"} and not isinstance(pattern_value, str):
            raise AssessmentError(f"Rule {index} pattern must be a string.")
        if patterns[0] in {"patterns", "pattern-either"} and not isinstance(pattern_value, list):
            raise AssessmentError(f"Rule {index} patterns must be a YAML list.")
        try:
            pattern_bytes = len(json.dumps(pattern_value, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError, RecursionError) as exc:
            raise AssessmentError(f"Rule {index} pattern contains unsupported YAML values.") from exc
        if pattern_bytes > 2_000:
            raise AssessmentError(f"Rule {index} pattern exceeds 2 KB.")
        validated.append(rule)
    return validated


class CustomRuleStore:
    """User-isolated persistent custom rule definitions."""

    def __init__(self, path=None):
        self.path = path or _store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS code_quality_custom_rules (
                rule_id TEXT PRIMARY KEY, user_key TEXT NOT NULL, title TEXT NOT NULL,
                yaml_text TEXT NOT NULL, enabled INTEGER NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS code_quality_custom_rules_user ON code_quality_custom_rules(user_key, updated_at DESC)")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _user_key(user_id: str) -> str:
        return sha256(user_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _public(row) -> dict[str, Any]:
        return {"rule_id": row["rule_id"], "title": row["title"], "yaml": row["yaml_text"],
                "enabled": bool(row["enabled"]), "created_at": row["created_at"],
                "updated_at": row["updated_at"], "content_hash": sha256(row["yaml_text"].encode()).hexdigest()}

    def list(self, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM code_quality_custom_rules WHERE user_key=? ORDER BY updated_at DESC", (self._user_key(user_id),)).fetchall()
        return [self._public(row) for row in rows]

    def get(self, user_id: str, rule_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM code_quality_custom_rules WHERE user_key=? AND rule_id=?", (self._user_key(user_id), rule_id)).fetchone()
        return self._public(row) if row else None

    def save(self, user_id: str, title: Any, document: Any, rule_id: str | None = None) -> dict[str, Any]:
        rules = validate_rule_yaml(document)
        title = title.strip() if isinstance(title, str) else ""
        if not 1 <= len(title) <= 100:
            raise AssessmentError("Rule set name must be between 1 and 100 characters.")
        user_key = self._user_key(user_id)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            if rule_id:
                changed = db.execute("UPDATE code_quality_custom_rules SET title=?,yaml_text=?,updated_at=? WHERE user_key=? AND rule_id=?",
                    (title, document, now, user_key, rule_id)).rowcount
                if not changed:
                    return None
            else:
                count = db.execute("SELECT COUNT(*) FROM code_quality_custom_rules WHERE user_key=?", (user_key,)).fetchone()[0]
                if count >= MAX_RULES_PER_USER:
                    raise AssessmentError("You have reached the limit of 100 custom rule sets.")
                rule_id = "cqr_" + uuid4().hex
                db.execute("INSERT INTO code_quality_custom_rules VALUES(?,?,?,?,?,?,?)",
                    (rule_id, user_key, title, document, 1, now, now))
        return self.get(user_id, rule_id)

    def set_enabled(self, user_id: str, rule_id: str, enabled: bool) -> dict[str, Any] | None:
        with self._connect() as db:
            changed = db.execute("UPDATE code_quality_custom_rules SET enabled=?,updated_at=? WHERE user_key=? AND rule_id=?",
                (int(enabled), datetime.now(timezone.utc).isoformat(), self._user_key(user_id), rule_id)).rowcount
        return self.get(user_id, rule_id) if changed else None

    def delete(self, user_id: str, rule_id: str) -> bool:
        with self._connect() as db:
            return bool(db.execute("DELETE FROM code_quality_custom_rules WHERE user_key=? AND rule_id=?", (self._user_key(user_id), rule_id)).rowcount)

    def enabled(self, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM code_quality_custom_rules WHERE user_key=? AND enabled=1 ORDER BY rule_id", (self._user_key(user_id),)).fetchall()
        results = [self._public(row) for row in rows]
        if sum(len(item["yaml"].encode("utf-8")) for item in results) > MAX_ACTIVE_RULE_BYTES:
            raise AssessmentError("Enabled custom rules exceed the 128 KB assessment limit. Disable or reduce some rules.")
        return results
