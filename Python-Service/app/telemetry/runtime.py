"""Read-side runtime aggregates from ClickHouse, separate from the symbol graph."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_SCOPE = """ResourceAttributes['indexer.project_id'] = {project_id:String}
    AND ResourceAttributes['indexer.revision_id'] = {revision_id:String}
    AND ResourceAttributes['indexer.code_dirty'] = 'false'
    AND ResourceAttributes['indexer.schema.version'] = 'indexer-telemetry-v2'"""


def _table_display_name(table_id: str) -> str:
    value = table_id.strip()
    if value.startswith("table:"):
        value = value.removeprefix("table:")
        if "." in value:
            value = value.split(".", 1)[1]
    return value.rsplit(".", 1)[-1]


RUNTIME_POLICY_KEY = "sampling_policy"


def runtime_sampling_policy(registry=None) -> dict:
    """Read the persisted sampling controls, bounded by pre-tail span retention."""
    if registry is None:
        from telemetry.registry import default_registry
        registry = default_registry()
    try:
        retention_hours = max(1, min(24 * 365, int(os.getenv("TELEMETRY_AGGREGATION_RETENTION_HOURS", "168"))))
    except ValueError:
        retention_hours = 168
    saved = registry.get_runtime_setting(RUNTIME_POLICY_KEY)
    value = saved["value"] if isinstance(saved, dict) and isinstance(saved.get("value"), dict) else {}
    default_window = min(24, retention_hours)
    window_hours = value.get("p95_window_hours", default_window)
    refresh_minutes = value.get("refresh_minutes", 5)
    if type(window_hours) is not int or not 1 <= window_hours <= retention_hours:
        window_hours = default_window
    if type(refresh_minutes) is not int or not 1 <= refresh_minutes <= 60:
        refresh_minutes = 5
    return {
        "p95_window_hours": window_hours,
        "refresh_minutes": refresh_minutes,
        "aggregation_retention_hours": retention_hours,
        "updated_at": saved.get("updated_at") if isinstance(saved, dict) else None,
    }


def update_runtime_sampling_policy(payload: dict, registry=None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Runtime sampling settings must be an object")
    if registry is None:
        from telemetry.registry import default_registry
        registry = default_registry()
    current = runtime_sampling_policy(registry)
    window = payload.get("p95_window_hours")
    refresh = payload.get("refresh_minutes")
    if type(window) is not int or not 1 <= window <= current["aggregation_retention_hours"]:
        raise ValueError(f"p95_window_hours must be between 1 and {current['aggregation_retention_hours']}")
    if type(refresh) is not int or not 1 <= refresh <= 60:
        raise ValueError("refresh_minutes must be between 1 and 60")
    value = {"p95_window_hours": window, "refresh_minutes": refresh}
    registry.set_runtime_setting(RUNTIME_POLICY_KEY, value)
    return runtime_sampling_policy(registry)


def telemetry_settings(registry=None) -> dict:
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
        recording_ratio = float(os.getenv("TELEMETRY_RECORDING_PERCENT", "10")) / 100
    except ValueError:
        recording_ratio = 0.1
    recording_ratio = min(1.0, max(0.0, recording_ratio))
    policy = runtime_sampling_policy(registry)
    return {
        "enabled": bool(endpoint and address),
        "store": "ClickHouse",
        "connection": address,
        "retention_hours": retention_hours,
        "retention_days": retention_hours / 24,
        "aggregation_retention_days": policy["aggregation_retention_hours"] / 24,
        "collector_baseline_ratio": recording_ratio,
        "collector_baseline_percent": round(recording_ratio * 100, 1),
        "otlp_endpoint": os.getenv("TELEMETRY_OTLP_ENDPOINT", "").strip() or None,
        "scope": "project + immutable revision ID + canonical symbol ID",
        "sample_window": f"Rolling p95 over the last {policy['p95_window_hours']} hours.",
        **policy,
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
        self.table = os.getenv("TELEMETRY_CLICKHOUSE_FULL_TABLE", "otel_spans_full")
        self.trace_table = os.getenv("TELEMETRY_CLICKHOUSE_TABLE", "otel_traces")
        self.window_hours = runtime_sampling_policy()["p95_window_hours"]
        if not _IDENTIFIER.fullmatch(self.database) or not _IDENTIFIER.fullmatch(self.table):
            raise ValueError("Invalid ClickHouse telemetry table configuration")

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def rows(self, sql: str, project_id: str, revision_id: str) -> list[dict]:
        parameters = urlencode({"param_project_id": project_id, "param_revision_id": revision_id})
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

    def unscoped_rows(self, sql: str) -> list[dict]:
        if not self.configured:
            raise RuntimeError("ClickHouse is not configured")
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode("ascii")
        request = Request(
            f"{self.url}/",
            data=(sql.rstrip() + " FORMAT JSONEachRow").encode("utf-8"),
            headers={"Authorization": f"Basic {token}", "Content-Type": "text/plain"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            return [json.loads(line) for line in response.read().decode("utf-8").splitlines() if line]

    def execute(self, sql: str, body: bytes | None = None) -> None:
        if not self.configured:
            raise RuntimeError("ClickHouse is not configured")
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode("ascii")
        query = urlencode({"database": self.database, **({"query": sql} if body is not None else {})})
        request = Request(
            f"{self.url}/?{query}",
            data=body if body is not None else sql.encode("utf-8"),
            headers={"Authorization": f"Basic {token}", "Content-Type": "text/plain"},
            method="POST",
        )
        with urlopen(request, timeout=15) as response:
            response.read()

    def store_unresolved_symbols(self, rows: list[dict]) -> None:
        if not rows or not self.configured:
            return
        table = f"`{self.database}`.`unresolved_symbols`"
        retention_hours = runtime_sampling_policy()["aggregation_retention_hours"]
        self.execute(
            f"CREATE TABLE IF NOT EXISTS {table} (project_id String, revision_id String, commit_sha String, "
            "symbol_id String, observed_spans UInt64, last_seen DateTime64(9), "
            "snapshot_updated_at DateTime64(3), is_unresolved UInt8) "
            "ENGINE = ReplacingMergeTree(snapshot_updated_at) "
            "ORDER BY (project_id, commit_sha, symbol_id) "
            f"TTL snapshot_updated_at + INTERVAL {retention_hours} HOUR"
        )
        # Existing installations created this table before revision IDs were
        # introduced. Keep commit provenance and add the generated identity in
        # place so an upgrade does not discard retained diagnostics.
        self.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS revision_id String DEFAULT ''")
        body = "\n".join(json.dumps(row, separators=(",", ":")) for row in rows).encode("utf-8")
        self.execute(f"INSERT INTO {table} FORMAT JSONEachRow", body)

    def summary(self, project_id: str, revision_id: str, known_symbol_ids: set[str]) -> dict:
        if not self.configured:
            return self._empty("ClickHouse is not configured.")
        table = f"`{self.database}`.`{self.table}`"
        try:
            scope = f"{_SCOPE} AND Timestamp >= now() - INTERVAL {self.window_hours} HOUR"
            scoped = f"SELECT TraceId, SpanId, ParentSpanId, Timestamp, Duration, StatusCode, SpanAttributes, SpanAttributes['indexer.symbol_id'] AS symbol_id FROM {table} WHERE {scope}"
            totals = self.rows(
                f"SELECT count() AS spans, countIf(symbol_id != '') AS symbol_spans, "
                f"countIf(StatusCode = 2) AS errors, max(Timestamp) AS last_seen "
                f"FROM ({scoped})", project_id, revision_id,
            )
            symbols = self.rows(
                f"SELECT symbol_id, count() AS runs, max(Timestamp) AS last_seen, "
                f"quantile(0.95)(toFloat64(Duration)) / 1000000 AS p95_ms, "
                f"countIf(StatusCode = 2) AS failures FROM ({scoped}) "
                "WHERE symbol_id != '' GROUP BY symbol_id ORDER BY runs DESC LIMIT 5000",
                project_id, revision_id,
            )
            routes = self.rows(
                f"SELECT SpanAttributes['http.request.method'] AS method, "
                f"SpanAttributes['http.route'] AS route, count() AS requests, "
                "if(SpanAttributes['indexer.route_id'] != '', SpanAttributes['indexer.route_id'], "
                "concat('route:', upper(SpanAttributes['http.request.method']), ' ', SpanAttributes['http.route'])) AS route_id, "
                f"quantile(0.95)(toFloat64(Duration)) / 1000000 AS p95_ms, "
                f"countIf(StatusCode = 2) AS failures, max(Timestamp) AS last_seen FROM {table} WHERE {scope} "
                "AND route != '' GROUP BY method, route, route_id ORDER BY requests DESC LIMIT 1000",
                project_id, revision_id,
            )
            tables = self.rows(
                "SELECT table_name, operation, count() AS operations, max(Timestamp) AS last_seen FROM ("
                "SELECT arrayJoin(if(length(JSONExtractArrayRaw(SpanAttributes['indexer.table_ids'])) > 0, "
                "arrayMap(value -> replaceAll(value, '\"', ''), JSONExtractArrayRaw(SpanAttributes['indexer.table_ids'])), "
                "[if(SpanAttributes['indexer.db.table'] != '', SpanAttributes['indexer.db.table'], SpanAttributes['db.collection.name'])])) AS table_name, "
                "upper(SpanAttributes['db.operation.name']) AS operation, Timestamp "
                f"FROM {table} WHERE {scope}) WHERE table_name != '' "
                "GROUP BY table_name, operation ORDER BY operations DESC LIMIT 1000",
                project_id, revision_id,
            )
            parents = self.rows(
                f"SELECT child.symbol_id AS symbol_id, parent.symbol_id AS caller_symbol_id, count() AS calls "
                f"FROM ({scoped}) AS child INNER JOIN ({scoped}) AS parent "
                "ON child.TraceId = parent.TraceId AND child.ParentSpanId = parent.SpanId "
                "WHERE child.symbol_id != '' AND parent.symbol_id != '' "
                "GROUP BY symbol_id, caller_symbol_id ORDER BY calls DESC LIMIT 5000",
                project_id, revision_id,
            )
            symbol_routes = self.rows(
                "WITH traces AS (SELECT TraceId, groupUniqArrayIf(if(SpanAttributes['indexer.route_id'] != '', "
                "SpanAttributes['indexer.route_id'], concat('route:', upper(SpanAttributes['http.request.method']), ' ', SpanAttributes['http.route'])), "
                f"SpanAttributes['http.route'] != '') AS routes FROM {table} WHERE {scope} GROUP BY TraceId) "
                f"SELECT spans.symbol_id AS symbol_id, route AS route, count() AS calls FROM ({scoped}) AS spans "
                "INNER JOIN traces USING (TraceId) ARRAY JOIN traces.routes AS route "
                "WHERE spans.symbol_id != '' GROUP BY symbol_id, route ORDER BY calls DESC LIMIT 5000",
                project_id, revision_id,
            )
            symbol_tables = self.rows(
                "SELECT symbol_id, table_name, count() AS calls FROM ("
                "SELECT SpanAttributes['indexer.symbol_id'] AS symbol_id, arrayJoin(if(length(JSONExtractArrayRaw(SpanAttributes['indexer.table_ids'])) > 0, "
                "arrayMap(value -> replaceAll(value, '\"', ''), JSONExtractArrayRaw(SpanAttributes['indexer.table_ids'])), "
                "[if(SpanAttributes['indexer.db.table'] != '', SpanAttributes['indexer.db.table'], SpanAttributes['db.collection.name'])])) AS table_name "
                f"FROM {table} WHERE {scope} AND symbol_id != '') "
                "WHERE table_name != '' GROUP BY symbol_id, table_name ORDER BY calls DESC LIMIT 5000",
                project_id, revision_id,
            )
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError):
            return self._empty("ClickHouse could not be queried.")

        total = totals[0] if totals else {}
        span_count = int(total.get("spans", 0) or 0)
        symbol_span_count = int(total.get("symbol_spans", 0) or 0)
        matched = sum(int(row.get("runs", 0) or 0) for row in symbols if row.get("symbol_id") in known_symbol_ids)
        unmatched = max(0, symbol_span_count - matched)
        for row in symbols:
            runs = int(row.get("runs", 0) or 0)
            row["error_rate"] = round(100 * int(row.get("failures", 0) or 0) / runs, 2) if runs else 0.0
        for row in routes:
            requests = int(row.get("requests", 0) or 0)
            row["error_rate"] = round(100 * int(row.get("failures", 0) or 0) / requests, 2) if requests else 0.0
        routes_by_id = {str(row.get("route_id", "")): row for row in routes if row.get("route_id")}
        for row in symbol_routes:
            route_id = str(row.get("route", ""))
            route = routes_by_id.get(route_id)
            row["route_id"] = route_id
            if route:
                row["route"] = route.get("route", route_id)
                row["method"] = route.get("method", "")
        for row in tables:
            raw_table_id = str(row.get("table_name", ""))
            row["table_id"] = raw_table_id
            row["table_name"] = _table_display_name(raw_table_id)
        for row in symbol_tables:
            raw_table_id = str(row.get("table_name", ""))
            row["table_id"] = raw_table_id
            row["table_name"] = _table_display_name(raw_table_id)
        last_seen = total.get("last_seen")
        try:
            last_seen_at = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
            if last_seen_at.tzinfo is None:
                last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
            freshness_minutes = max(2, runtime_sampling_policy().get("refresh_minutes", 5) * 2)
            pipeline_health = (
                "receiving" if (datetime.now(timezone.utc) - last_seen_at).total_seconds() <= freshness_minutes * 60
                else "stale"
            )
        except (TypeError, ValueError):
            pipeline_health = "waiting_for_spans"
        return {
            "available": True,
            "reason": None if span_count else "No runtime spans have arrived for this project commit.",
            "total_spans": span_count,
            "symbol_spans": symbol_span_count,
            "matched_spans": matched,
            "match_percent": round(100 * matched / symbol_span_count, 1) if symbol_span_count else 0.0,
            "unmatched_spans": unmatched,
            "unmatched_percent": round(100 * unmatched / symbol_span_count, 1) if symbol_span_count else 0.0,
            "error_spans": int(total.get("errors", 0) or 0),
            "last_seen": last_seen,
            "symbols": symbols,
            "routes": routes,
            "tables": tables,
            "callers": parents,
            "symbol_routes": symbol_routes,
            "symbol_tables": symbol_tables,
            "retention_hours": runtime_sampling_policy()["aggregation_retention_hours"],
            "sampled_trace_retention_hours": telemetry_settings()["retention_hours"],
            "aggregation_window_hours": self.window_hours,
            "pipeline_health": pipeline_health,
        }

    @staticmethod
    def _empty(reason: str) -> dict:
        return {
            "available": False, "reason": reason, "total_spans": 0,
            "symbol_spans": 0, "matched_spans": 0, "match_percent": 0.0,
            "error_spans": 0, "last_seen": None, "symbols": [], "routes": [],
            "tables": [], "callers": [], "symbol_routes": [], "symbol_tables": [],
            "retention_hours": runtime_sampling_policy()["aggregation_retention_hours"],
            "sampled_trace_retention_hours": telemetry_settings()["retention_hours"],
            "unmatched_spans": 0, "unmatched_percent": 0.0,
            "aggregation_window_hours": runtime_sampling_policy()["p95_window_hours"],
            "pipeline_health": "unavailable",
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


def _project_runtime(project_id: str, snapshot: dict, registry) -> dict:
    commit_sha = snapshot.get("commit_sha")
    revision_id = snapshot.get("revision_id")
    deployments = registry.running_deployments(project_id)
    matching = [row for row in deployments if commit_sha and row["commit_sha"] == commit_sha]
    index_available = snapshot.get("state") == "ready"
    index_reason = None
    try:
        definitions = snapshot_definitions(project_id, commit_sha) if index_available and commit_sha else []
    except Exception:  # Neo4j/index may be offline while ClickHouse is reachable.
        definitions = []
        index_available = False
        index_reason = "The symbol snapshot is temporarily unavailable."
    known_ids = {item["symbol_id"] for item in definitions if item.get("symbol_id")}
    clickhouse = ClickHouseRuntime()
    telemetry = clickhouse.summary(project_id, revision_id, known_ids) if revision_id else clickhouse._empty(
        "This snapshot has no indexed revision ID yet."
    )
    return {
        "project_id": project_id,
        "commit_sha": commit_sha,
        "revision_id": revision_id,
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


def project_runtime(project_id: str, commit_sha: str, registry) -> dict:
    return _project_runtime(project_id, registry.get(project_id, commit_sha), registry)


def project_runtime_by_revision(project_id: str, revision_id: str, registry) -> dict:
    snapshot = registry.get_by_revision_id(project_id, revision_id)
    if snapshot is None:
        raise KeyError("Indexed revision was not found")
    return _project_runtime(project_id, snapshot, registry)
