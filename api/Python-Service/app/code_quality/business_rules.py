"""Project-scoped plain-language requirements for evidence-grounded review."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import sqlite3
from typing import Any
from uuid import uuid4

from code_quality.service import AssessmentError, CATEGORIES, _store_path

MAX_PROJECT_RULES = 50
MAX_TITLE_CHARS = 120
MAX_STATEMENT_CHARS = 600
_RULE_ID = re.compile(r"^[0-9a-f]{32}$")


def _normalize_rule(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AssessmentError("Business rule must be an object.")
    title = value.get("title")
    statement = value.get("statement")
    category = value.get("category")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > MAX_TITLE_CHARS:
        raise AssessmentError(f"Business rule title must contain 1 to {MAX_TITLE_CHARS} characters.")
    if not isinstance(statement, str) or not statement.strip() or len(statement.strip()) > MAX_STATEMENT_CHARS:
        raise AssessmentError(f"Business rule statement must contain 1 to {MAX_STATEMENT_CHARS} characters.")
    if category not in CATEGORIES:
        raise AssessmentError("Select a valid Code Quality category for this business rule.")
    enabled = value.get("enabled", True)
    if type(enabled) is not bool:
        raise AssessmentError("Business rule enabled must be true or false.")
    return {"title": title.strip(), "statement": statement.strip(),
            "category": category, "enabled": enabled}


class BusinessRuleStore:
    """Store editable business requirements separately from executable rules."""

    def __init__(self, path=None):
        self.path = path or _store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS code_quality_business_rules (
                project_id TEXT NOT NULL, rule_id TEXT NOT NULL, version INTEGER NOT NULL,
                title TEXT NOT NULL, statement TEXT NOT NULL, category TEXT NOT NULL,
                enabled INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, rule_id))""")
            db.execute("CREATE INDEX IF NOT EXISTS code_quality_business_rules_project "
                       "ON code_quality_business_rules(project_id, enabled, updated_at)")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _serialize(row: sqlite3.Row) -> dict[str, Any]:
        return {"rule_id": row["rule_id"], "version": row["version"],
                "title": row["title"], "statement": row["statement"],
                "category": row["category"], "enabled": bool(row["enabled"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def list(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM code_quality_business_rules WHERE project_id=? "
                               "ORDER BY created_at, rule_id", (project_id,)).fetchall()
        return [self._serialize(row) for row in rows]

    def enabled(self, project_id: str) -> list[dict[str, Any]]:
        return [rule for rule in self.list(project_id) if rule["enabled"]]

    def save(self, project_id: str, value: Any, rule_id: str | None = None) -> dict[str, Any] | None:
        normalized = _normalize_rule(value)
        now = datetime.now(timezone.utc).isoformat()
        selected_id = rule_id or uuid4().hex
        if not _RULE_ID.fullmatch(selected_id):
            raise AssessmentError("Business rule identity is invalid.")
        with self._connect() as db:
            row = db.execute("SELECT * FROM code_quality_business_rules WHERE project_id=? AND rule_id=?",
                              (project_id, selected_id)).fetchone()
            if rule_id and row is None:
                return None
            if row is None:
                count = db.execute("SELECT COUNT(*) FROM code_quality_business_rules WHERE project_id=?",
                                   (project_id,)).fetchone()[0]
                if count >= MAX_PROJECT_RULES:
                    raise AssessmentError(f"A project can have at most {MAX_PROJECT_RULES} business rules.")
                version, created_at = 1, now
            else:
                same_content = all(row[key] == normalized[key]
                                   for key in ("title", "statement", "category", "enabled"))
                version = row["version"] if same_content else row["version"] + 1
                created_at = row["created_at"]
            db.execute("""INSERT INTO code_quality_business_rules
                (project_id, rule_id, version, title, statement, category, enabled, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id, rule_id) DO UPDATE SET
                version=excluded.version, title=excluded.title, statement=excluded.statement,
                category=excluded.category, enabled=excluded.enabled, updated_at=excluded.updated_at""",
                       (project_id, selected_id, version, normalized["title"], normalized["statement"],
                        normalized["category"], int(normalized["enabled"]), created_at, now))
            saved = db.execute("SELECT * FROM code_quality_business_rules WHERE project_id=? AND rule_id=?",
                               (project_id, selected_id)).fetchone()
        return self._serialize(saved)

    def delete(self, project_id: str, rule_id: str) -> bool:
        if not _RULE_ID.fullmatch(rule_id):
            return False
        with self._connect() as db:
            result = db.execute("DELETE FROM code_quality_business_rules WHERE project_id=? AND rule_id=?",
                                (project_id, rule_id))
        return result.rowcount == 1


def rule_snapshot(rule: dict[str, Any]) -> dict[str, Any]:
    """Keep only immutable user intent fields in assessment reports."""
    return {key: rule[key] for key in ("rule_id", "version", "title", "statement", "category")}
