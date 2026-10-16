"""Bounded, read-only assessment of immutable source archives."""
from __future__ import annotations

import ast
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import sys
from typing import Any
from uuid import uuid4
import zipfile

from infra.settings import settings

CATEGORIES = ("security", "clean_code", "maintainability", "reliability", "performance")
DEFAULT_WEIGHTS = {category: 20 for category in CATEGORIES}
POLICY_VERSION = "code-quality-policy-1"
RUBRIC_VERSION = "code-quality-rubric-2"
SCORING_PARAMETERS = {"temperature": 0.0, "top_p": 1.0}
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_FILES = 20_000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SCAN_BYTES = 48 * 1024 * 1024
MAX_FINDINGS = 500
MAX_EVIDENCE_BYTES = 48_000
MAX_MODEL_OUTPUT = 24_000

LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".php": "php", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".go": "go", ".rs": "rust", ".rb": "ruby", ".cs": "csharp", ".c": "c",
    ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".swift": "swift",
    ".scala": "scala", ".sh": "shell", ".bash": "shell", ".sql": "sql",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "css",
    ".vue": "vue", ".svelte": "svelte", ".dart": "dart", ".ex": "elixir",
    ".exs": "elixir", ".lua": "lua", ".r": "r", ".pl": "perl", ".erl": "erlang",
    ".clj": "clojure", ".cljs": "clojure", ".tf": "hcl", ".hcl": "hcl",
    ".m": "objective-c", ".mm": "objective-c",
}
SUPPORTED_LANGUAGES = sorted(set(LANGUAGE_BY_SUFFIX.values()))
NON_SOURCE_SUFFIXES = {".md", ".json", ".json5", ".yaml", ".yml", ".toml", ".xml", ".txt",
                       ".csv", ".lock", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
                       ".woff", ".woff2", ".ttf", ".ico", ".pdf", ".map", ".zip", ".gz",
                       ".br", ".wasm", ".bin", ".mp4", ".mp3", ".mov", ".sqlite", ".db",
                       ".pem", ".key"}

# These bounded source-pattern checks provide cross-language signals without
# installing or invoking arbitrary project-defined executables.
PATTERNS = {
    "security": [
        ("dynamic-code-execution", re.compile(r"\b(?:eval|exec)\s*\("), "Dynamic code execution call"),
        ("shell-command-construction", re.compile(r"\b(?:shell_exec|system|passthru|popen|subprocess\.Popen)\s*\("), "Shell execution call"),
        ("disabled-tls-verification", re.compile(r"(?i)(?:verify\s*=\s*False|rejectUnauthorized\s*:\s*false|CURLOPT_SSL_VERIFYPEER\s*,\s*false)"), "TLS verification appears disabled"),
        ("embedded-credential", re.compile(r"(?i)\b(?:api[_-]?key|password|client[_-]?secret)\s*[:=]\s*['\"][^'\"]{8,}['\"]"), "Possible credential literal"),
        ("unsafe-deserialization", re.compile(r"\b(?:pickle\.loads|yaml\.unsafe_load|unserialize)\s*\("), "Unsafe deserialization call"),
    ],
    "reliability": [
        ("empty-exception-handler", re.compile(r"(?m)^\s*(?:except(?:\s+[^:]+)?|catch\s*\([^)]*\))\s*:?\s*(?:pass|\{\s*\})\s*$"), "Exception handler appears to discard errors"),
        ("unbounded-retry", re.compile(r"(?i)\bwhile\s+true\b|\bwhile\s*\(\s*true\s*\)"), "Unbounded loop requires review"),
    ],
    "performance": [
        ("select-star", re.compile(r"(?i)\bselect\s+\*\s+from\b"), "SQL SELECT * query"),
        ("nested-loop", re.compile(r"(?m)^\s*(?:for|while)\b.*:\s*$"), "Loop construct; inspect nested or large-input cost"),
    ],
}


class AssessmentError(ValueError):
    """Safe, client-actionable assessment error."""


class AssessmentModelUnavailable(RuntimeError):
    """The configured model could not complete the scoring request."""


class AssessmentReportStoreUnavailable(RuntimeError):
    """The completed report could not be persisted."""


def _store_path() -> Path:
    root = Path(os.environ.get("TELEMETRY_SNAPSHOT_ROOT") or Path(settings.workspace_root).parent / "snapshots")
    root.mkdir(parents=True, exist_ok=True)
    return root / "code_quality.sqlite"


class ReportStore:
    """Own immutable assessment reports and per-user weight preferences."""

    def __init__(self, path: Path | None = None):
        self.path = path or _store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS assessment_reports (
                report_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, revision_id TEXT NOT NULL,
                created_at TEXT NOT NULL, report_json TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS assessment_reports_project_revision ON assessment_reports(project_id, revision_id, created_at DESC)")
            db.execute("""CREATE TABLE IF NOT EXISTS assessment_preferences (
                user_key TEXT PRIMARY KEY, weights_json TEXT NOT NULL, updated_at TEXT NOT NULL)""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def weights(self, user_id: str) -> dict[str, int]:
        user_key = sha256(user_id.encode("utf-8")).hexdigest()
        with self._connect() as db:
            row = db.execute("SELECT weights_json FROM assessment_preferences WHERE user_key=?", (user_key,)).fetchone()
        return _validate_weights(json.loads(row["weights_json"])) if row else dict(DEFAULT_WEIGHTS)

    def save_weights(self, user_id: str, weights: dict) -> dict[str, int]:
        value = _validate_weights(weights)
        user_key = sha256(user_id.encode("utf-8")).hexdigest()
        with self._connect() as db:
            db.execute("INSERT INTO assessment_preferences(user_key,weights_json,updated_at) VALUES(?,?,?) "
                       "ON CONFLICT(user_key) DO UPDATE SET weights_json=excluded.weights_json,updated_at=excluded.updated_at",
                       (user_key, json.dumps(value, sort_keys=True), datetime.now(timezone.utc).isoformat()))
        return value

    def save_report(self, report: dict) -> dict:
        with self._connect() as db:
            db.execute("INSERT INTO assessment_reports(report_id,project_id,revision_id,created_at,report_json) VALUES(?,?,?,?,?)",
                       (report["report_id"], report["project_id"], report["revision_id"], report["created_at"], json.dumps(report, ensure_ascii=False, separators=(",", ":"))))
        return report

    def history(self, project_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT report_json FROM assessment_reports WHERE project_id=? ORDER BY created_at DESC LIMIT 200", (project_id,)).fetchall()
        reports = [json.loads(row["report_json"]) for row in rows]
        return [{key: report.get(key) for key in (
            "report_id", "project_id", "revision_id", "commit_sha", "created_at",
            "overall_score", "categories", "weights", "rubric_version", "model", "provider",
            "scoring_parameters",
        )} | {"coverage": report.get("coverage", {}).get("files", 0),
            "languages": report.get("coverage", {}).get("languages", {})} for report in reports]

    def get_report(self, project_id: str, report_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT report_json FROM assessment_reports WHERE project_id=? AND report_id=?", (project_id, report_id)).fetchone()
        return json.loads(row["report_json"]) if row else None


def _validate_weights(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != set(CATEGORIES):
        raise AssessmentError("Provide a weight for each assessment category.")
    if any(type(value[key]) is not int or not 0 <= value[key] <= 100 for key in CATEGORIES):
        raise AssessmentError("Category weights must be whole numbers from 0 to 100.")
    if sum(value.values()) != 100:
        raise AssessmentError("Category weights must total 100 percent.")
    return {key: value[key] for key in CATEGORIES}


def _safe_archive_path(name: str) -> str | None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or not path.parts:
        return None
    return path.as_posix()


def inventory_archive(archive_data: bytes) -> tuple[dict[str, int], list[dict], list[str], dict[str, int], list[dict]]:
    if not archive_data or len(archive_data) > MAX_ARCHIVE_BYTES:
        raise AssessmentError("The indexed source archive is empty or exceeds the assessment limit.")
    languages: Counter[str] = Counter()
    unsupported_extensions: Counter[str] = Counter()
    findings: list[dict] = []
    inventory: list[str] = []
    scanned_bytes = 0
    scanned_files = 0
    skipped_files = 0
    analyzer_failures: list[dict] = []
    try:
        with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES or sum(item.file_size for item in entries) > 256 * 1024 * 1024:
                raise AssessmentError("The indexed source archive exceeds assessment limits.")
            for entry in entries:
                relative = _safe_archive_path(entry.filename.rstrip("/"))
                if not relative or entry.is_dir():
                    continue
                suffix = Path(relative).suffix.lower()
                language = LANGUAGE_BY_SUFFIX.get(suffix)
                if not language:
                    if suffix and suffix not in NON_SOURCE_SUFFIXES:
                        unsupported_extensions[suffix] += 1
                    continue
                inventory.append(relative)
                languages[language] += 1
                if len(inventory) > MAX_FILES:
                    break
                if entry.file_size > MAX_FILE_BYTES or scanned_bytes + entry.file_size > MAX_SCAN_BYTES:
                    skipped_files += 1
                    continue
                source = archive.read(entry).decode("utf-8", errors="replace")
                scanned_bytes += entry.file_size
                scanned_files += 1
                lines = source.splitlines()
                if language == "python":
                    try:
                        ast.parse(source, filename=relative)
                    except SyntaxError as exc:
                        findings.append(_finding(relative, exc.lineno or 1, "python-syntax-error", "reliability", "Python source has a syntax error."))
                    except RecursionError:
                        if not any(item["name"] == "python-ast" for item in analyzer_failures):
                            analyzer_failures.append({"name": "python-ast", "reason": "A file exceeded the parser recursion limit."})
                measures = {
                    "large-file": (len(lines) > 800, "maintainability", f"File contains {len(lines)} lines; review responsibility boundaries."),
                    "long-lines": (sum(1 for line in lines if len(line) > 160) > 10, "clean_code", "File contains more than ten lines over 160 characters."),
                    "high-branching": (sum(1 for line in lines if re.search(r"\b(if|for|while|case|catch)\b", line)) > 100, "maintainability", "File has many branching constructs; review complexity."),
                }
                for rule, (matched, category, message) in measures.items():
                    if matched:
                        findings.append(_finding(relative, 1, rule, category, message))
                for category, rules in PATTERNS.items():
                    for rule, pattern, message in rules:
                        for match in pattern.finditer(source):
                            line = source.count("\n", 0, match.start()) + 1
                            findings.append(_finding(relative, line, rule, category, message, match.start()))
                            if len(findings) >= MAX_FINDINGS:
                                break
                        if len(findings) >= MAX_FINDINGS:
                            break
                if len(findings) >= MAX_FINDINGS:
                    break
    except AssessmentError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise AssessmentError("The indexed source archive could not be read safely.") from exc
    unsupported_file_count = sum(unsupported_extensions.values())
    unsupported_extensions = dict(unsupported_extensions.most_common(100))
    scan_stats = {"detected_files": len(inventory) + unsupported_file_count,
                  "supported_files": len(inventory), "scanned_files": scanned_files,
                  "skipped_files": skipped_files, "scanned_bytes": scanned_bytes}
    scan_stats["unsupported_extensions"] = dict(sorted(unsupported_extensions.items()))
    return dict(sorted(languages.items())), findings[:MAX_FINDINGS], inventory, scan_stats, analyzer_failures


def _finding(file: str, line: int, rule: str, category: str, message: str, offset: int = 0) -> dict:
    identifier = "ev_" + sha256(f"{file}:{line}:{rule}:{offset}".encode()).hexdigest()[:16]
    return {"evidence_id": identifier, "category": category, "rule": rule, "file": file[:300], "line": line, "message": message[:240]}


def _coverage_evidence(project_id: str, revision_id: str, languages: dict[str, int], findings: list[dict], scan_stats: dict[str, Any]) -> list[dict]:
    if not scan_stats["scanned_files"]:
        return []
    result = []
    for category in CATEGORIES:
        identifier = "ev_" + sha256(f"{project_id}:{revision_id}:{POLICY_VERSION}:{category}".encode()).hexdigest()[:16]
        count = sum(1 for item in findings if item["category"] == category)
        result.append({"evidence_id": identifier, "kind": "coverage", "category": category,
                       "rule": "bounded-scan-coverage", "file": None, "line": None,
                       "message": (f"Policy {POLICY_VERSION} scanned {scan_stats['scanned_files']} files "
                                   f"across {', '.join(sorted(languages))}; it emitted {count} "
                                   f"{category} findings. This heuristic scan does not prove the absence of defects.")})
    return result


def _category_coverage(languages: dict[str, int], evidence: list[dict], scan_stats: dict[str, int]) -> dict[str, dict]:
    return {
        category: {"supported": bool(languages) and scan_stats["scanned_files"] > 0,
                   "evidence_count": sum(1 for item in evidence if item["category"] == category),
                   "reason": None if scan_stats["scanned_files"] else "No supported source files were within the scanner limits."}
        for category in CATEGORIES
    }


def _parse_model_output(content: str) -> dict:
    if not isinstance(content, str) or len(content) > MAX_MODEL_OUTPUT:
        raise AssessmentError("The configured model returned an invalid scoring response.")
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        value = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise AssessmentError("The configured model returned invalid structured scoring data.") from exc
    if not isinstance(value, dict) or not isinstance(value.get("categories"), dict):
        raise AssessmentError("The configured model returned invalid structured scoring data.")
    return value


def _score(
    evidence: list[dict], coverage: dict[str, dict], languages: dict[str, int],
    scan_stats: dict[str, Any], analyzer_failures: list[dict],
) -> tuple[dict, str, str]:
    from runtime.llm import complete_with_model, remote_configured

    model_context = {"languages": languages, "coverage": coverage,
                     "scan_limits": {key: value for key, value in scan_stats.items() if key != "unsupported_extensions"},
                     "unsupported_extensions": scan_stats["unsupported_extensions"],
                     "analyzer_failures": analyzer_failures[:20], "evidence": []}
    for row in evidence:
        model_context["evidence"].append(row)
        if len(json.dumps(model_context, ensure_ascii=False, separators=(",", ":"))) > MAX_EVIDENCE_BYTES:
            model_context["evidence"].pop()
            break
    compact = json.dumps(model_context, ensure_ascii=False, separators=(",", ":"))
    prompt = (
        f"Score these five software quality categories: {', '.join(CATEGORIES)}. Rubric {RUBRIC_VERSION}. "
        "Use this fixed score scale: 0-20 means critical failures dominate; 21-40 means multiple serious risks; "
        "41-60 means mixed evidence with material risks; 61-80 means generally healthy with notable improvements; "
        "81-100 means strong positive evidence with only minor risks. Select a score within the matching band "
        "and do not reward missing evidence or treat a clean heuristic scan as proof of quality. "
        "Return only JSON: {\"categories\":{category:{\"assessed\":boolean,\"score\":integer|null,\"reason\":string,\"evidence_ids\":[string]}}}. "
        "Scores range 0-100, where higher is better. Cite only supplied evidence IDs. Mark a category unassessed if evidence is insufficient; do not infer absence of defects from missing evidence. "
        "Do not invent findings. Evidence: " + compact
    )
    try:
        content, model, provider = complete_with_model(
            prompt,
            system="You are a cautious code quality evaluator. Follow the response schema exactly.",
            max_tokens=1200,
            temperature=SCORING_PARAMETERS["temperature"],
            top_p=SCORING_PARAMETERS["top_p"],
            provider="remote" if remote_configured() else "local",
        )
    except Exception as exc:
        raise AssessmentModelUnavailable from exc
    try:
        raw = _parse_model_output(content)
    except AssessmentError:
        return ({category: {"assessed": False, "score": None,
                            "reason": "The evaluator returned invalid structured output; retry this assessment.",
                            "evidence_ids": []} for category in CATEGORIES},
                str(model)[:200], str(provider)[:40])
    categories: dict[str, dict] = {}
    valid_ids = {category: {row["evidence_id"] for row in evidence if row["category"] == category}
                 for category in CATEGORIES}
    for category in CATEGORIES:
        result = raw["categories"].get(category)
        if not isinstance(result, dict):
            categories[category] = {"assessed": False, "score": None, "reason": "The evaluator omitted this category.", "evidence_ids": []}
            continue
        ids = result.get("evidence_ids", [])
        ids = [item for item in ids if isinstance(item, str) and item in valid_ids[category]][:40] if isinstance(ids, list) else []
        assessed = (result.get("assessed") is True and type(result.get("score")) is int
                    and 0 <= result["score"] <= 100 and bool(ids) and bool(coverage[category]["supported"]))
        categories[category] = {
            "assessed": assessed,
            "score": result["score"] if assessed else None,
            "reason": str(result.get("reason") or (coverage[category]["reason"] if not coverage[category]["supported"] else "Evidence was insufficient to assign a score."))[:500] if not assessed else str(result.get("reason") or "Score based on supplied analyzer evidence.")[:500],
            "evidence_ids": ids if assessed else [],
        }
    return categories, str(model)[:200], str(provider)[:40]


def assess_revision(project_id: str, revision_id: str, snapshot: dict, archive_data: bytes, user_id: str, store: ReportStore | None = None) -> dict:
    try:
        store = store or ReportStore()
        weights = store.weights(user_id)
    except Exception as exc:
        raise AssessmentReportStoreUnavailable from exc
    languages, findings, files, scan_stats, analyzer_failures = inventory_archive(archive_data)
    coverage_evidence = _coverage_evidence(project_id, revision_id, languages, findings, scan_stats)
    evidence = [*findings, *coverage_evidence]
    coverage = _category_coverage(languages, evidence, scan_stats)
    if languages:
        categories, model, provider = _score(evidence, coverage, languages, scan_stats, analyzer_failures)
    else:
        reason = "No supported source language was detected; all categories remain unassessed."
        categories = {category: {"assessed": False, "score": None, "reason": reason, "evidence_ids": []}
                      for category in CATEGORIES}
        model, provider = "not-run", "not-run"
    overall = None
    if all(categories[key]["assessed"] for key in CATEGORIES):
        overall = round(sum(categories[key]["score"] * weights[key] for key in CATEGORIES) / 100, 1)
    analyzers = []
    if languages:
        analyzers.append({"name": "cross-language-source-signals", "version": POLICY_VERSION,
                          "languages": SUPPORTED_LANGUAGES, "selected_languages": sorted(languages),
                          "categories": list(CATEGORIES),
                          "limits": {"archive_bytes": MAX_ARCHIVE_BYTES, "file_bytes": MAX_FILE_BYTES,
                                     "total_source_bytes": MAX_SCAN_BYTES, "findings": MAX_FINDINGS}})
    if "python" in languages:
        analyzers.append({"name": "python-ast", "version": sys.version.split()[0],
                          "languages": ["python"], "selected_languages": ["python"],
                          "categories": ["reliability"],
                          "limits": {"file_bytes": MAX_FILE_BYTES, "total_source_bytes": MAX_SCAN_BYTES}})
    report = {
        "report_id": "qa_" + uuid4().hex,
        "project_id": project_id,
        "revision_id": revision_id,
        "commit_sha": snapshot["commit_sha"],
        "source_archive_sha256": snapshot.get("source_archive_sha256"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "overall_score": overall,
        "categories": categories,
        "weights": weights,
        "coverage": {"files": scan_stats["detected_files"], "supported_files": scan_stats["supported_files"],
                     "scanned_files": scan_stats["scanned_files"],
                     "skipped_files": scan_stats["skipped_files"], "scanned_bytes": scan_stats["scanned_bytes"],
                     "languages": languages, "unsupported_extensions": scan_stats["unsupported_extensions"],
                     "categories": coverage, "analyzer_failures": analyzer_failures[:20],
                     "analyzer_policy_version": POLICY_VERSION, "analyzers": analyzers},
        "findings": findings,
        "evidence": evidence,
        "rubric_version": RUBRIC_VERSION,
        "scoring_parameters": dict(SCORING_PARAMETERS),
        "model": model,
        "provider": provider,
    }
    try:
        return store.save_report(report)
    except Exception as exc:
        raise AssessmentReportStoreUnavailable from exc
