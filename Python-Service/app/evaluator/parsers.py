"""Native database telemetry parsers.

These parsers are intentionally dependency-free and tolerant of truncated log
files. A parser never executes SQL; execution belongs to the caller.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Iterable, Mapping

from .models import EvaluatorReport, TelemetryNormalizer


_MYSQL_HEADER = re.compile(
    r"#\s*Query_time:\s*(?P<query>[0-9.]+)\s+"
    r"Lock_time:\s*(?P<lock>[0-9.]+)\s+"
    r"Rows_sent:\s*(?P<sent>\d+)\s+Rows_examined:\s*(?P<examined>\d+)",
    re.I,
)
_MYSQL_TIME = re.compile(r"#\s*Time:\s*(?P<time>.+)", re.I)
_PG_DURATION = re.compile(r"duration:\s*(?P<duration>[0-9.]+)\s*ms", re.I)
_PG_STATEMENT = re.compile(r"\bstatement:\s*(?P<query>.+)", re.I)
_PG_EXECUTE = re.compile(r"\bexecute\s+[^:]+:\s*(?P<query>.+)", re.I)


def fingerprint_query(query: str) -> str:
    """Stable, low-cardinality query fingerprint with literals redacted."""
    cleaned = re.sub(r"'[^']*'|\"[^\"]*\"|\b\d+(?:\.\d+)?\b", "?", query or "")
    return re.sub(r"\s+", " ", cleaned).strip().lower() or "unknown"


def _issue_metrics(metrics: dict[str, Any], *, slow_ms: float = 1000.0) -> list[str]:
    issues: list[str] = []
    access = str(metrics.get("access_type") or metrics.get("node_type") or "").lower()
    if access in {"all", "seq scan", "sequential scan", "table scan", "full scan"}:
        issues.append("full_table_scan")
    if metrics.get("possible_keys") in (None, "", "none", "null", []) and metrics.get("key") in (
        None,
        "",
        "none",
        "null",
    ):
        # Only flag this when the plan actually carries index fields.  A log
        # record with no EXPLAIN data cannot establish missing index coverage.
        if "possible_keys" in metrics or "key" in metrics:
            issues.append("missing_index_coverage")
    examined = metrics.get("rows_examined")
    if isinstance(examined, (int, float)) and examined >= 10000:
        issues.append("high_rows_examined")
    elapsed = metrics.get("execution_time_ms")
    if isinstance(elapsed, (int, float)) and elapsed >= slow_ms:
        issues.append("excessive_execution_time")
    return issues


def _n_plus_one(reports: list[EvaluatorReport]) -> None:
    counts = Counter(report.query_fingerprint for report in reports)
    for report in reports:
        count = counts[report.query_fingerprint]
        if count >= 3 and "n_plus_one" not in report.issues:
            report.issues.append("n_plus_one")
            report.metrics["n_plus_one_occurrences"] = count


class MySQLSlowLogParser:
    """Parse MySQL slow-query log entries into normalized reports."""

    name = "mysql-slow-log"

    def parse(self, text: str | bytes | None) -> list[EvaluatorReport]:
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        if not text:
            return []
        lines = text.splitlines()
        reports: list[EvaluatorReport] = []
        current: dict[str, Any] | None = None
        query_lines: list[str] = []

        def finish() -> None:
            nonlocal current, query_lines
            if current is None:
                return
            query = " ".join(line.strip() for line in query_lines if line.strip())
            query = re.sub(r"^SET\s+timestamp\s*=\s*\d+\s*;?\s*", "", query, flags=re.I)
            if not query:
                current = None
                query_lines = []
                return
            metrics = {
                key: current[key]
                for key in ("execution_time_ms", "lock_time_ms", "rows_sent", "rows_examined")
                if key in current
            }
            issues = _issue_metrics(metrics)
            reports.append(
                TelemetryNormalizer.report(
                    query,
                    metrics,
                    profiler=self.name,
                    provenance={"format": "mysql-slow-log", "timestamp": current.get("timestamp")},
                    issues=issues,
                )
            )
            current = None
            query_lines = []

        for raw_line in lines:
            line = raw_line.rstrip()
            match = _MYSQL_HEADER.search(line)
            if match:
                finish()
                current = {
                    "execution_time_ms": float(match.group("query")) * 1000,
                    "lock_time_ms": float(match.group("lock")) * 1000,
                    "rows_sent": int(match.group("sent")),
                    "rows_examined": int(match.group("examined")),
                }
                continue
            time_match = _MYSQL_TIME.search(line)
            if time_match and current is not None:
                current["timestamp"] = time_match.group("time").strip()
                continue
            if current is not None and not line.lstrip().startswith("#"):
                query_lines.append(line)
        finish()
        _n_plus_one(reports)
        return reports


class PostgreSQLSlowLogParser:
    """Parse PostgreSQL ``duration/statement`` and ``execute`` log lines."""

    name = "postgres-slow-log"

    def parse(self, text: str | bytes | None) -> list[EvaluatorReport]:
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        if not text:
            return []
        reports: list[EvaluatorReport] = []
        pending_duration: float | None = None
        pending_timestamp: str | None = None
        pending_lines: list[str] = []

        def emit(query: str, duration: float, timestamp: str | None = None) -> None:
            query = query.strip().rstrip(";") + (";" if query.strip().endswith(";") else "")
            if not query.strip():
                return
            metrics = {"execution_time_ms": duration}
            reports.append(
                TelemetryNormalizer.report(
                    query,
                    metrics,
                    profiler=self.name,
                    provenance={"format": "postgres-slow-log", "timestamp": timestamp},
                    issues=_issue_metrics(metrics),
                )
            )

        for raw_line in text.splitlines():
            line = raw_line.strip()
            duration_match = _PG_DURATION.search(line)
            if duration_match:
                pending_duration = float(duration_match.group("duration"))
                pending_timestamp = line.split(" ", 1)[0] if line else None
            statement_match = _PG_STATEMENT.search(line)
            execute_match = _PG_EXECUTE.search(line)
            query_match = statement_match or execute_match
            if query_match and pending_duration is not None:
                emit(query_match.group("query"), pending_duration, pending_timestamp)
                pending_duration = None
                pending_lines = []
                continue
            # PostgreSQL can emit a continuation line after ``statement:``.
            if pending_duration is not None and line and not line.startswith("LOG:"):
                pending_lines.append(line)
                if pending_lines:
                    emit(" ".join(pending_lines), pending_duration, pending_timestamp)
                    pending_duration = None
                    pending_lines = []
        _n_plus_one(reports)
        return reports


def parse_mysql_slow_log(text: str | bytes | None) -> list[EvaluatorReport]:
    return MySQLSlowLogParser().parse(text)


def parse_postgres_slow_log(text: str | bytes | None) -> list[EvaluatorReport]:
    return PostgreSQLSlowLogParser().parse(text)


def _walk_plan(node: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(node, Mapping):
        if isinstance(node.get("Plan"), Mapping):
            yield from _walk_plan(node["Plan"])
        if "Node Type" in node or "node_type" in node or "access_type" in node:
            yield node
        for key in ("Plans", "plans"):
            children = node.get(key)
            if isinstance(children, list):
                for child in children:
                    yield from _walk_plan(child)
    elif isinstance(node, list):
        for child in node:
            yield from _walk_plan(child)


def analyze_explain(
    explain: str | Mapping[str, Any] | list[Any],
    query: str | None = None,
    *,
    high_rows_threshold: int = 10000,
    slow_ms: float = 1000.0,
) -> EvaluatorReport:
    """Normalize EXPLAIN text/JSON and report evidence-backed plan issues."""
    raw: Any = explain
    if isinstance(explain, str):
        try:
            raw = json.loads(explain)
        except (TypeError, ValueError):
            raw = explain
    metrics: dict[str, Any] = {"explain_format": "json" if isinstance(raw, (dict, list)) else "text"}
    issues: list[str] = []
    nodes = list(_walk_plan(raw))
    if nodes:
        metrics["plan_nodes"] = len(nodes)
        scans = []
        rows_examined = 0
        for node in nodes:
            node_type = str(node.get("Node Type") or node.get("node_type") or node.get("type") or "")
            access_type = str(node.get("access_type") or node.get("type") or "")
            scans.append(node_type or access_type)
            examined = node.get("Rows Examined", node.get("rows_examined", node.get("Actual Rows", node.get("rows"))))
            if isinstance(examined, (int, float)):
                rows_examined += int(examined)
            if node_type.lower() in {"seq scan", "sequential scan"} or access_type.lower() == "all":
                issues.append("full_table_scan")
            possible = node.get("possible_keys")
            key = node.get("key", node.get("Index Name"))
            if (possible in (None, "", "none", []) and key in (None, "", "none")) and (
                "possible_keys" in node or "key" in node or "Index Name" in node
            ):
                issues.append("missing_index_coverage")
            elapsed = node.get("Actual Total Time", node.get("execution_time_ms"))
            if isinstance(elapsed, (int, float)):
                metrics["execution_time_ms"] = max(float(metrics.get("execution_time_ms", 0)), float(elapsed))
        metrics["scan_types"] = scans
        if rows_examined:
            metrics["rows_examined"] = rows_examined
            if rows_examined >= high_rows_threshold:
                issues.append("high_rows_examined")
    else:
        # Handle the common tabular EXPLAIN output without pretending that all
        # columns are available.
        lower = str(raw).lower()
        if "seq scan" in lower or re.search(r"\btype\s*[:| ]+all\b", lower):
            issues.append("full_table_scan")
        rows_match = re.search(r"rows(?: examined)?\s*[:|= ]\s*([\d,]+)", lower)
        if rows_match:
            metrics["rows_examined"] = int(rows_match.group(1).replace(",", ""))
            if metrics["rows_examined"] >= high_rows_threshold:
                issues.append("high_rows_examined")
        time_match = re.search(r"(?:actual total time|execution time|duration)\s*[:|= ]\s*([\d.]+)", lower)
        if time_match:
            metrics["execution_time_ms"] = float(time_match.group(1))
    if isinstance(metrics.get("execution_time_ms"), (int, float)) and metrics["execution_time_ms"] >= slow_ms:
        issues.append("excessive_execution_time")
    issues = list(dict.fromkeys(issues))
    return TelemetryNormalizer.report(
        query,
        metrics,
        profiler="explain",
        provenance={"format": metrics["explain_format"]},
        issues=issues,
        availability="available" if explain is not None else "unavailable",
        error=None if explain is not None else "EXPLAIN output unavailable",
    )


def parse_explain(explain: str | Mapping[str, Any] | list[Any], query: str | None = None) -> EvaluatorReport:
    return analyze_explain(explain, query)
