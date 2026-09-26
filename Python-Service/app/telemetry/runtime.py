"""Read-side runtime aggregates from ClickHouse, separate from the symbol graph."""
from __future__ import annotations

import base64
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_SCOPE = """SpanAttributes['indexer.project_id'] = {project_id:String}
    AND SpanAttributes['vcs.ref.head.revision'] = {commit_sha:String}
    AND SpanAttributes['indexer.dirty'] = 'false'
    AND SpanAttributes['indexer.schema.version'] = 'indexer-telemetry-v1'"""


def telemetry_settings() -> dict:
    """Non-secret collector/storage information safe for the authenticated UI."""
    endpoint = os.getenv("TELEMETRY_CLICKHOUSE_URL", "").strip()
    address = None
    if endpoint:
        try:
            parsed = urlsplit(endpoint)
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                port = parsed.port or (443 if parsed.scheme == "https" else 8123)
                address = f"{parsed.scheme}://{parsed.hostname}:{port}"
        except ValueError:
            pass
    try:
        retention_hours = max(1, min(24 * 365, int(os.getenv("TELEMETRY_RETENTION_HOURS", "168"))))
    except ValueError:
        retention_hours = 168
    try:
        recording_ratio = float(os.getenv("TELEMETRY_RECORDING_PERCENT", "100")) / 100
    except ValueError:
        recording_ratio = 1.0
    recording_ratio = min(1.0, max(0.0, recording_ratio))
    return {
        "enabled": bool(endpoint and address),
        "store": "ClickHouse",
        "connection": address,
        "retention_hours": retention_hours,
        "retention_days": retention_hours / 24,
        "recording_ratio": recording_ratio,
        "recording_percent": round(recording_ratio * 100, 1),
        "otlp_endpoint": os.getenv("TELEMETRY_OTLP_ENDPOINT", "").strip() or None,
        "scope": "project + full commit SHA + canonical symbol ID",
        "sample_window": "The full configured ClickHouse retention window.",
    }


class ClickHouseRuntime:
    def __init__(self) -> None:
        self.url = os.getenv("TELEMETRY_CLICKHOUSE_URL", "").strip().rstrip("/")
        parsed = urlsplit(self.url) if self.url else None
        if parsed and (parsed.scheme not in {"http", "https"} or not parsed.hostname
                       or parsed.username or parsed.password or parsed.query or parsed.fragment):
            self.url = ""
        self.user = os.getenv("TELEMETRY_CLICKHOUSE_USER", "telemetry")
        self.password = os.getenv("TELEMETRY_CLICKHOUSE_PASSWORD", "")
        self.database = os.getenv("TELEMETRY_CLICKHOUSE_DATABASE", "telemetry")
        self.table = os.getenv("TELEMETRY_CLICKHOUSE_TABLE", "otel_traces")
        if not _IDENTIFIER.fullmatch(self.database) or not _IDENTIFIER.fullmatch(self.table):
            raise ValueError("Invalid ClickHouse telemetry table configuration")

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def rows(self, sql: str, project_id: str, commit_sha: str) -> list[dict]:
        parameters = urlencode({"param_project_id": project_id, "param_commit_sha": commit_sha})
        url = f"{self.url}/?{parameters}"
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode("ascii")
        request = Request(
            url,
            data=(sql.rstrip() + " FORMAT JSONEachRow").encode("utf-8"),
            headers={"Authorization": f"Basic {token}", "Content-Type": "text/plain"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            return [json.loads(line) for line in response.read().decode("utf-8").splitlines() if line]

    def summary(self, project_id: str, commit_sha: str, known_symbol_ids: set[str]) -> dict:
        if not self.configured:
            return self._empty("ClickHouse is not configured.")
        table = f"`{self.database}`.`{self.table}`"
        try:
            scoped = f"SELECT TraceId, SpanId, ParentSpanId, Timestamp, Duration, StatusCode, SpanAttributes, SpanAttributes['indexer.symbol_id'] AS symbol_id FROM {table} WHERE {_SCOPE}"
            totals = self.rows(
                f"SELECT count() AS spans, countIf(symbol_id != '') AS symbol_spans, "
                f"countIf(StatusCode = 2) AS errors, max(Timestamp) AS last_seen "
                f"FROM ({scoped})", project_id, commit_sha,
            )
            symbols = self.rows(
                f"SELECT symbol_id, count() AS runs, max(Timestamp) AS last_seen, "
                f"quantile(0.95)(toFloat64(Duration)) / 1000000 AS p95_ms, "
                f"countIf(StatusCode = 2) AS failures FROM ({scoped}) "
                "WHERE symbol_id != '' GROUP BY symbol_id ORDER BY runs DESC LIMIT 5000",
                project_id, commit_sha,
            )
            routes = self.rows(
                f"SELECT SpanAttributes['http.request.method'] AS method, "
                f"SpanAttributes['http.route'] AS route, count() AS requests, "
                f"max(Timestamp) AS last_seen FROM {table} WHERE {_SCOPE} "
                "AND route != '' GROUP BY method, route ORDER BY requests DESC LIMIT 1000",
                project_id, commit_sha,
            )
            tables = self.rows(
                f"SELECT if(SpanAttributes['indexer.db.table'] != '', SpanAttributes['indexer.db.table'], "
                "SpanAttributes['db.collection.name']) AS table_name, "
                "upper(SpanAttributes['db.operation.name']) AS operation, count() AS operations, "
                f"max(Timestamp) AS last_seen FROM {table} WHERE {_SCOPE} AND table_name != '' "
                "GROUP BY table_name, operation ORDER BY operations DESC LIMIT 1000",
                project_id, commit_sha,
            )
            parents = self.rows(
                f"SELECT child.symbol_id AS symbol_id, parent.symbol_id AS caller_symbol_id, count() AS calls "
                f"FROM ({scoped}) AS child INNER JOIN ({scoped}) AS parent "
                "ON child.TraceId = parent.TraceId AND child.ParentSpanId = parent.SpanId "
                "WHERE child.symbol_id != '' AND parent.symbol_id != '' "
                "GROUP BY symbol_id, caller_symbol_id ORDER BY calls DESC LIMIT 5000",
                project_id, commit_sha,
            )
            symbol_routes = self.rows(
                f"WITH traces AS (SELECT TraceId, groupUniqArrayIf(SpanAttributes['http.route'], "
                f"SpanAttributes['http.route'] != '') AS routes FROM {table} WHERE {_SCOPE} GROUP BY TraceId) "
                f"SELECT spans.symbol_id AS symbol_id, route, count() AS calls FROM ({scoped}) AS spans "
                "INNER JOIN traces USING (TraceId) ARRAY JOIN traces.routes AS route "
                "WHERE spans.symbol_id != '' GROUP BY symbol_id, route ORDER BY calls DESC LIMIT 5000",
                project_id, commit_sha,
            )
            symbol_tables = self.rows(
                f"WITH traces AS (SELECT TraceId, groupUniqArrayIf(if(SpanAttributes['indexer.db.table'] != '', "
                "SpanAttributes['indexer.db.table'], SpanAttributes['db.collection.name']), "
                "if(SpanAttributes['indexer.db.table'] != '', SpanAttributes['indexer.db.table'], "
                f"SpanAttributes['db.collection.name']) != '') AS tables FROM {table} WHERE {_SCOPE} GROUP BY TraceId) "
                f"SELECT spans.symbol_id AS symbol_id, table_name, count() AS calls FROM ({scoped}) AS spans "
                "INNER JOIN traces USING (TraceId) ARRAY JOIN traces.tables AS table_name "
                "WHERE spans.symbol_id != '' GROUP BY symbol_id, table_name ORDER BY calls DESC LIMIT 5000",
                project_id, commit_sha,
            )
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError):
            return self._empty("ClickHouse could not be queried.")

        total = totals[0] if totals else {}
        span_count = int(total.get("spans", 0) or 0)
        symbol_span_count = int(total.get("symbol_spans", 0) or 0)
        matched = sum(int(row.get("runs", 0) or 0) for row in symbols if row.get("symbol_id") in known_symbol_ids)
        return {
            "available": True,
            "reason": None if span_count else "No runtime spans have arrived for this project commit.",
            "total_spans": span_count,
            "symbol_spans": symbol_span_count,
            "matched_spans": matched,
            "match_percent": round(100 * matched / span_count, 1) if span_count else 0.0,
            "error_spans": int(total.get("errors", 0) or 0),
            "last_seen": total.get("last_seen"),
            "symbols": symbols,
            "routes": routes,
            "tables": tables,
            "callers": parents,
            "symbol_routes": symbol_routes,
            "symbol_tables": symbol_tables,
            "retention_hours": telemetry_settings()["retention_hours"],
        }

    @staticmethod
    def _empty(reason: str) -> dict:
        return {
            "available": False, "reason": reason, "total_spans": 0,
            "symbol_spans": 0, "matched_spans": 0, "match_percent": 0.0,
            "error_spans": 0, "last_seen": None, "symbols": [], "routes": [],
            "tables": [], "callers": [], "symbol_routes": [], "symbol_tables": [],
            "retention_hours": telemetry_settings()["retention_hours"],
        }


def snapshot_definitions(project_id: str, commit_sha: str) -> list[dict]:
    """Read the immutable Phase 0 PhpSymbol snapshot; never inspect legacy rows."""
    from inspection.service import InspectionService
    from telemetry.snapshot import storage_id

    collection = "sym_snapshot_" + storage_id(project_id, commit_sha, "")
    service = InspectionService()
    result: list[dict] = []
    offset = 0
    while True:
        page = service.list_definitions(collection, offset=offset, limit=200)
        items = page.get("items", [])
        result.extend(items)
        if len(items) < 200:
            break
        offset += len(items)
    return result


def project_runtime(project_id: str, commit_sha: str, registry) -> dict:
    snapshot = registry.get(project_id, commit_sha)
    deployments = registry.running_deployments(project_id)
    matching = [row for row in deployments if row["commit_sha"] == commit_sha]
    index_available = snapshot.get("state") == "ready"
    index_reason = None
    try:
        definitions = snapshot_definitions(project_id, commit_sha) if index_available else []
    except Exception:  # Neo4j/index may be offline while ClickHouse is reachable.
        definitions = []
        index_available = False
        index_reason = "The symbol snapshot is temporarily unavailable."
    known_ids = {item["symbol_id"] for item in definitions if item.get("symbol_id")}
    telemetry = ClickHouseRuntime().summary(project_id, commit_sha, known_ids)
    return {
        "project_id": project_id,
        "commit_sha": commit_sha,
        "snapshot": snapshot,
        "definitions": definitions,
        "symbol_index_available": index_available,
        "symbol_index_reason": index_reason,
        "deployments": deployments,
        "matching_deployments": matching,
        "deployment_mismatch": bool(deployments) and not matching,
        "telemetry": telemetry,
        "settings": telemetry_settings(),
    }
