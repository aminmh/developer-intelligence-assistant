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


from telemetry.span_schema import SPAN_SCOPE

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_METHOD_HOOK_ID = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*\\)*[A-Za-z_][A-Za-z0-9_]*::[A-Za-z_][A-Za-z0-9_]*$"
)
# Materialized scope columns from the owned span schema (telemetry/span_schema.py).
_SCOPE = SPAN_SCOPE
REQUEST_OUTCOMES = ("all", "errors", "slow")


def _table_display_name(table_id: str) -> str:
    value = table_id.strip()
    if value.startswith("table:"):
        value = value.removeprefix("table:")
        if "." in value:
            value = value.split(".", 1)[1]
    return value.rsplit(".", 1)[-1]


def _timestamp_epoch_ms(value) -> float | None:
    if isinstance(value, datetime):
        timestamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return timestamp.timestamp() * 1000
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.timestamp() * 1000


def _method_activity(raw) -> dict:
    """Normalize the bounded method-hook edge list from one request root span."""
    if raw in (None, ""):
        return {
            "available": False,
            "reason": "No method-call data was captured for this request.",
            "edges": [],
        }
    try:
        entries = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        entries = None
    if not isinstance(entries, list):
        return {
            "available": False,
            "reason": "Method-call data could not be decoded.",
            "edges": [],
        }

    counts: dict[tuple[str, str], int] = {}
    for item in entries[:256]:
        if not isinstance(item, dict):
            continue
        caller = item.get("caller_symbol_id")
        callee = item.get("symbol_id")
        calls = item.get("calls")
        if (
            isinstance(caller, str)
            and isinstance(callee, str)
            and _METHOD_HOOK_ID.fullmatch(caller)
            and _METHOD_HOOK_ID.fullmatch(callee)
            and type(calls) is int
            and 0 < calls <= 1_000_000
        ):
            key = (caller, callee)
            counts[key] = min(1_000_000, counts.get(key, 0) + calls)

    edges = [
        {
            "caller_symbol_id": caller,
            "symbol_id": callee,
            "calls": calls,
            "source": "hook",
        }
        for (caller, callee), calls in sorted(
            counts.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    return {
        "available": bool(edges),
        "reason": None if edges else "No valid method-call data was available for this request.",
        "edges": edges,
    }


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
    from telemetry.runtime_storage import clickhouse_connection, public_storage_target
    connection = clickhouse_connection(registry)
    target = public_storage_target(registry)
    display_connection = None
    if target["mode"] == "remote" and target.get("host"):
        display_connection = f"https://{target['host']}:{target['http_port']}"
    elif target["mode"] == "local":
        endpoint = os.getenv("TELEMETRY_CLICKHOUSE_URL", "").strip()
        if endpoint:
            try:
                parsed = urlsplit(endpoint)
                if parsed.scheme in {"http", "https"} and parsed.hostname:
                    port = parsed.port or (443 if parsed.scheme == "https" else 8123)
                    display_connection = f"{parsed.scheme}://{parsed.hostname}:{port}"
            except ValueError:
                pass
    elif connection["url"]:
        parsed = urlsplit(connection["url"])
        port = parsed.port or (443 if parsed.scheme == "https" else 8123)
        display_connection = f"{parsed.scheme}://{parsed.hostname}:{port}"
    return {
        "enabled": bool(connection["url"]),
        "store": "ClickHouse",
        "connection": display_connection,
        "storage_mode": target["mode"],
        "storage_target": target,
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


_DETAIL_SPAN_LIMIT = 500
# The same query (operation, tables, issuing method) this many times in one request is flagged as repeated.
REPEATED_QUERY_MIN = 3


def _database_summary(calls: list[dict], request_duration_ms) -> dict:
    """Totals and same-shape groups for one request's database calls.

    A group is the operation, the tables, and the application method that issued the query (the
    innermost app frame). Without the SQL text two queries in a group may differ in their columns or
    conditions, so a repeated group is a strong hint (often N+1 or a per-call cache read), not proof.
    """
    def ms(value) -> float:
        try:
            return max(0.0, float(value or 0))
        except (TypeError, ValueError):
            return 0.0

    total_ms = sum(ms(call.get("duration_ms")) for call in calls)
    try:
        request_ms = float(request_duration_ms) if request_duration_ms is not None else None
    except (TypeError, ValueError):
        request_ms = None
    slowest = max(calls, key=lambda call: ms(call.get("duration_ms")), default=None)

    groups: dict[tuple, dict] = {}
    for call in calls:
        key = (call.get("operation") or None, tuple(call.get("table_ids") or ()), call.get("symbol_id") or None)
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "operation": key[0],
                "table_ids": list(key[1]),
                "symbol_id": key[2],
                "caller_symbol_ids": [],
                "count": 0,
                "total_ms": 0.0,
                "max_ms": 0.0,
                "span_ids": [],
            }
        duration = ms(call.get("duration_ms"))
        group["count"] += 1
        group["total_ms"] += duration
        group["max_ms"] = max(group["max_ms"], duration)
        group["span_ids"].append(call.get("span_id"))
        caller = call.get("caller_symbol_id")
        if caller and caller not in group["caller_symbol_ids"] and len(group["caller_symbol_ids"]) < 5:
            group["caller_symbol_ids"].append(caller)

    ordered = sorted(groups.values(), key=lambda group: (-group["count"], -group["total_ms"]))
    for group in ordered:
        group["total_ms"] = round(group["total_ms"], 3)
        group["max_ms"] = round(group["max_ms"], 3)
        group["repeated"] = group["count"] >= REPEATED_QUERY_MIN
    return {
        "query_count": len(calls),
        "total_ms": round(total_ms, 3),
        # Queries in a PHP request run one after another, so their sum is a fair share of the request time.
        "request_share": round(min(1.0, total_ms / request_ms), 4) if request_ms and request_ms > 0 else None,
        "slowest": None if slowest is None else {
            "span_id": slowest.get("span_id"),
            "duration_ms": round(ms(slowest.get("duration_ms")), 3),
            "operation": slowest.get("operation"),
            "table_ids": slowest.get("table_ids") or [],
            "symbol_id": slowest.get("symbol_id"),
        },
        "groups": ordered,
        "repeated_groups": sum(1 for group in ordered if group["repeated"]),
        "repeated_min": REPEATED_QUERY_MIN,
    }
# Routes with fewer server requests in the window than this use the revision p95 as their slow threshold.
SLOW_ROUTE_MIN_REQUESTS = 20


def _ratio(value) -> float | None:
    """Head-sampling ratio from the indexer.sampling.ratio resource attribute; None when unknown."""
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return None
    return ratio if 0 < ratio <= 1 else None


def _sampling(rows: list[dict]) -> dict:
    """Sampling ratios behind the summary's spans, and an estimate of real span volume.

    Spans recorded at ratio r stand for about 1/r real spans. The estimate is None when any
    spans have an unknown ratio, since they cannot be scaled.
    """
    by_ratio: dict[float | None, int] = {}
    for row in rows:
        ratio = _ratio(row.get("k1"))
        by_ratio[ratio] = by_ratio.get(ratio, 0) + int(row.get("calls", 0) or 0)
    known = {ratio: spans for ratio, spans in by_ratio.items() if ratio is not None}
    estimated = round(sum(spans / ratio for ratio, spans in known.items())) if known and None not in by_ratio else None
    return {
        "sampling_ratio": next(iter(by_ratio)) if len(by_ratio) == 1 else None,
        "sampling_ratios": [
            {"ratio": ratio, "spans": spans}
            for ratio, spans in sorted(by_ratio.items(), key=lambda item: -item[1])
        ],
        "estimated_total_spans": estimated,
    }


def _top(rows: list[dict], count_key: str, limit: int) -> list[dict]:
    return sorted(rows, key=lambda row: row[count_key], reverse=True)[:limit]


class ClickHouseRuntime:
    def __init__(self) -> None:
        from telemetry.runtime_storage import clickhouse_connection
        configured = clickhouse_connection()
        self.url = configured["url"]
        parsed = urlsplit(self.url) if self.url else None
        if parsed and (parsed.scheme not in {"http", "https"} or not parsed.hostname
                       or parsed.username or parsed.password or parsed.query or parsed.fragment):
            self.url = ""
        self.user = configured["username"]
        self.password = configured["password"]
        self.database = configured["database"]
        self.ssl_context = configured["ssl_context"]
        self.table = os.getenv("TELEMETRY_CLICKHOUSE_FULL_TABLE", "otel_spans_full")
        self.trace_table = os.getenv("TELEMETRY_CLICKHOUSE_TABLE", "otel_traces")
        self.profile_table = os.getenv("TELEMETRY_PROFILE_TABLE", "otel_profiles")
        self.window_hours = runtime_sampling_policy()["p95_window_hours"]
        if (not _IDENTIFIER.fullmatch(self.database) or not _IDENTIFIER.fullmatch(self.table)
                or not _IDENTIFIER.fullmatch(self.profile_table)):
            raise ValueError("Invalid ClickHouse telemetry table configuration")

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def rows(self, sql: str, project_id: str, revision_id: str, extra_parameters: dict[str, str] | None = None) -> list[dict]:
        parameters = urlencode({"param_project_id": project_id, "param_revision_id": revision_id, **(extra_parameters or {})})
        url = f"{self.url}/?{parameters}"
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode("ascii")
        request = Request(
            url,
            data=(sql.rstrip() + " FORMAT JSONEachRow").encode("utf-8"),
            headers={"Authorization": f"Basic {token}", "Content-Type": "text/plain"},
            method="POST",
        )
        with urlopen(request, timeout=5, context=self.ssl_context) as response:
            return [json.loads(line) for line in response.read().decode("utf-8").splitlines() if line]

    def requests(self, project_id: str, revision_id: str, outcome: str = "all") -> dict:
        """Return bounded, privacy-shaped server request spans for one revision.

        ``outcome`` narrows the list: ``errors`` keeps failed or 5xx requests, ``slow`` keeps requests at or
        above this revision's p95 server duration in the same window. Filtering happens before the
        50-row limit, so problems are not pushed out by newer normal requests.
        """
        if outcome not in REQUEST_OUTCOMES:
            raise ValueError("Unknown request outcome filter")
        if not self.configured:
            return {"available": False, "reason": "ClickHouse is not configured.", "requests": []}
        table = f"`{self.database}`.`{self.table}`"
        window = " AND Timestamp >= now() - INTERVAL " + str(self.window_hours) + " HOUR"
        server_scope = _SCOPE + window + " AND SpanKind = 'Server' AND HttpRoute != ''"
        having = ""
        slow_threshold_ms = None
        try:
            if outcome == "errors":
                having = " HAVING span_status = 'Error' OR status_code >= 500"
            elif outcome == "slow":
                threshold = self.rows(
                    "SELECT quantile(0.95)(toFloat64(Duration)) / 1000000 AS p95_ms FROM " + table + " WHERE " + server_scope,
                    project_id, revision_id,
                )
                value = threshold[0].get("p95_ms") if threshold else None
                if value is None or not isinstance(value, (int, float)) or value != value:
                    return {"available": True, "reason": None, "window_hours": self.window_hours, "sampled": True,
                            "outcome": outcome, "slow_threshold_ms": None, "requests": []}
                slow_threshold_ms = round(float(value), 3)
            per_trace = (
                "SELECT TraceId AS trace_id, "
                "argMin(SpanId, tuple(Timestamp, SpanId)) AS span_id, "
                "argMin(Timestamp, tuple(Timestamp, SpanId)) AS timestamp, "
                "argMin(SpanAttributes['http.request.method'], tuple(Timestamp, SpanId)) AS method, "
                "argMin(SpanAttributes['http.route'], tuple(Timestamp, SpanId)) AS route, "
                "argMin(toInt32OrNull(SpanAttributes['http.response.status_code']), tuple(Timestamp, SpanId)) AS status_code, "
                "argMin(toFloat64(Duration) / 1000000, tuple(Timestamp, SpanId)) AS duration_ms, "
                "argMin(toInt64OrNull(SpanAttributes['indexer.php.cpu_time_us']), tuple(Timestamp, SpanId)) AS php_cpu_time_us, "
                "argMin(toInt64OrNull(SpanAttributes['indexer.php.peak_memory_bytes']), tuple(Timestamp, SpanId)) AS php_peak_memory_bytes, "
                "argMin(ResourceAttributes['indexer.sampling.ratio'], tuple(Timestamp, SpanId)) AS sampling_ratio, "
                "argMin(StatusCode, tuple(Timestamp, SpanId)) AS span_status FROM " + table + " WHERE " + server_scope +
                " GROUP BY TraceId" + having
            )
            if outcome == "slow":
                # Judge each request against its own route's p95, so a regression on a fast route is
                # not hidden behind slow ones. Routes with too few requests use the revision p95.
                route_p95 = (
                    "SELECT SpanAttributes['http.request.method'] AS method, HttpRoute AS route, count() AS route_requests, "
                    "quantile(0.95)(toFloat64(Duration)) / 1000000 AS route_p95_ms FROM " + table + " WHERE " + server_scope +
                    " GROUP BY method, route"
                )
                rows = self.rows(
                    f"SELECT requests.*, if(thresholds.route_requests >= {SLOW_ROUTE_MIN_REQUESTS}, thresholds.route_p95_ms, "
                    f"{float(slow_threshold_ms)!r}) AS slow_threshold_ms FROM ({per_trace}) AS requests "
                    f"LEFT JOIN ({route_p95}) AS thresholds ON requests.method = thresholds.method AND requests.route = thresholds.route "
                    "WHERE requests.duration_ms >= slow_threshold_ms ORDER BY requests.timestamp DESC LIMIT 50",
                    project_id, revision_id,
                )
            else:
                rows = self.rows(per_trace + " ORDER BY timestamp DESC LIMIT 50", project_id, revision_id)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError):
            return {"available": False, "reason": "ClickHouse request spans could not be queried.", "requests": []}
        clean = []
        for row in rows:
            trace_id = str(row.get("trace_id", "")).lower()
            if not re.fullmatch(r"[0-9a-f]{32}", trace_id):
                continue
            clean.append({
                "trace_id": trace_id,
                "span_id": str(row.get("span_id", "")),
                "timestamp": row.get("timestamp"),
                "method": str(row.get("method", "")),
                "route": str(row.get("route", "")),
                "status_code": row.get("status_code"),
                "duration_ms": row.get("duration_ms"),
                "span_status": str(row.get("span_status", "")),
                "php_cpu_time_us": row.get("php_cpu_time_us"),
                "php_peak_memory_bytes": row.get("php_peak_memory_bytes"),
                "sampling_ratio": _ratio(row.get("sampling_ratio")),
                **({"slow_threshold_ms": round(float(row["slow_threshold_ms"]), 3)}
                   if isinstance(row.get("slow_threshold_ms"), (int, float)) else {}),
            })
        ratios = {request["sampling_ratio"] for request in clean}
        ratio = ratios.pop() if len(ratios) == 1 else None
        return {"available": True, "reason": None, "window_hours": self.window_hours,
                "sampled": ratio is None or ratio < 1, "sampling_ratio": ratio,
                "outcome": outcome, "slow_threshold_ms": slow_threshold_ms, "requests": clean}

    def request_detail(self, project_id: str, revision_id: str, trace_id: str) -> dict:
        """Return only the request root and database spans from a scoped trace."""
        if not re.fullmatch(r"[0-9a-fA-F]{32}", trace_id):
            raise ValueError("Invalid trace ID")
        if not self.configured:
            return {
                "available": False,
                "reason": "ClickHouse is not configured.",
                "request": None,
                "database_calls": [],
                "method_activity": _method_activity(None),
            }
        table = f"`{self.database}`.`{self.table}`"
        try:
            rows = self.rows(
                "SELECT TraceId AS trace_id, SpanId AS span_id, ParentSpanId AS parent_span_id, "
                "Timestamp AS timestamp, SpanKind AS span_kind, StatusCode AS span_status, "
                "SpanAttributes['db.system.name'] AS db_system, SpanAttributes['db.operation.name'] AS db_operation, "
                "SpanAttributes['http.request.method'] AS method, SpanAttributes['http.route'] AS route, "
                "toInt32OrNull(SpanAttributes['http.response.status_code']) AS status_code, "
                "toFloat64(Duration) / 1000000 AS duration_ms, "
                "SpanAttributes['db.system.name'] AS db_system, SpanAttributes['db.namespace'] AS db_namespace, "
                "SpanAttributes['db.operation.name'] AS db_operation, "
                "SpanAttributes['indexer.table_ids'] AS table_ids, "
                "SpanAttributes['indexer.symbol_id'] AS symbol_id, "
                "SpanAttributes['indexer.caller.symbol_id'] AS caller_symbol_id, "
                "toInt64OrNull(SpanAttributes['indexer.php.cpu_time_us']) AS php_cpu_time_us, "
                "toInt64OrNull(SpanAttributes['indexer.php.peak_memory_bytes']) AS php_peak_memory_bytes, "
                "SpanAttributes['indexer.method_hook_counts'] AS method_hook_counts, "
                "toInt64OrNull(SpanAttributes['indexer.dropped.hook_calls']) AS dropped_hook_calls, "
                "SpanAttributes['error.type'] AS error_type, "
                "ResourceAttributes['indexer.sampling.ratio'] AS sampling_ratio "
                "FROM " + table + " WHERE " + _SCOPE + " AND TraceId = {trace_id:String} "
                "AND Timestamp >= now() - INTERVAL " + str(self.window_hours) + f" HOUR ORDER BY Timestamp ASC LIMIT {_DETAIL_SPAN_LIMIT + 1}",
                project_id, revision_id, {"param_trace_id": trace_id.lower()},
            )
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError):
            return {
                "available": False,
                "reason": "ClickHouse trace details could not be queried.",
                "request": None,
                "database_calls": [],
                "method_activity": _method_activity(None),
            }

        def table_ids(value) -> list[str]:
            try:
                decoded = json.loads(value) if isinstance(value, str) else value
            except (ValueError, TypeError):
                return []
            if not isinstance(decoded, list):
                return []
            return [item for item in decoded if isinstance(item, str) and len(item) <= 256][:32]

        truncated = len(rows) > _DETAIL_SPAN_LIMIT
        rows = rows[:_DETAIL_SPAN_LIMIT]
        root = next((row for row in rows if row.get("span_kind") == "Server"), None)
        if root is None:
            return {
                "available": False,
                "reason": "The sampled request was not found in this revision.",
                "request": None,
                "database_calls": [],
                "method_activity": _method_activity(None),
            }
        request = {
            "trace_id": str(root.get("trace_id", "")).lower(),
            "span_id": str(root.get("span_id", "")),
            "timestamp": root.get("timestamp"),
            "method": str(root.get("method", "")),
            "route": str(root.get("route", "")),
            "status_code": root.get("status_code"),
            "duration_ms": root.get("duration_ms"),
            "span_status": str(root.get("span_status", "")),
            "php_cpu_time_us": root.get("php_cpu_time_us"),
            "php_peak_memory_bytes": root.get("php_peak_memory_bytes"),
            "error_type": str(root.get("error_type") or "") or None,
        }
        ai_summary = self._trace_ai_summary(project_id, revision_id, trace_id, request, rows)
        calls = []
        for row in rows:
            operation = str(row.get("db_operation", ""))
            tables = table_ids(row.get("table_ids"))
            if not operation and not tables:
                continue
            calls.append({
                "span_id": str(row.get("span_id", "")),
                "parent_span_id": str(row.get("parent_span_id", "")),
                "timestamp": row.get("timestamp"),
                "duration_ms": row.get("duration_ms"),
                "db_system": str(row.get("db_system", "")) or None,
                "db_namespace": str(row.get("db_namespace", "")) or None,
                "operation": operation or None,
                "table_ids": tables,
                "symbol_id": str(row.get("symbol_id", "")) or None,
                "caller_symbol_id": str(row.get("caller_symbol_id", "")) or None,
                "span_status": str(row.get("span_status", "")),
            })
        return {
            "available": True,
            "reason": None,
            "request": request,
            "database_calls": calls,
            "database_summary": _database_summary(calls, request.get("duration_ms")),
            "method_activity": _method_activity(root.get("method_hook_counts")),
            "ai_summary": ai_summary,
            "sampled": True,
            "sampling_ratio": _ratio(root.get("sampling_ratio")),
            # Spans beyond the limit are not shown; the UI says the trace is incomplete.
            "truncated": truncated,
            "span_limit": _DETAIL_SPAN_LIMIT,
            "dropped_hook_calls": max(0, int(root.get("dropped_hook_calls") or 0)),
        }

    def _trace_ai_summary(self, project_id: str, revision_id: str, trace_id: str, request: dict, rows: list[dict]) -> dict:
        """Build a privacy-shaped, trace-scoped summary from spans and one retained profile."""
        total_ms = max(0.0, float(request.get("duration_ms") or 0.0))
        root = next((row for row in rows if row.get("span_kind") == "Server"), None)
        buckets: dict[str, list[tuple[float, float]]] = {}
        root_start = _timestamp_epoch_ms((root or {}).get("timestamp"))
        for row in rows:
            if row is root:
                continue
            db_system = str(row.get("db_system") or "").lower()
            if "mysql" in db_system:
                bucket = "mysql_queries_ms"
            elif "redis" in db_system:
                operation = re.sub(r"[^a-z0-9]+", "_", str(row.get("db_operation") or "other").lower()).strip("_")
                bucket = f"redis_{operation or 'other'}_ms"
            else:
                continue
            duration_ms = max(0.0, float(row.get("duration_ms") or 0.0))
            if duration_ms <= 0 or total_ms <= 0:
                continue
            start = _timestamp_epoch_ms(row.get("timestamp"))
            if root_start is None or start is None:
                buckets.setdefault(bucket, []).append((0.0, min(total_ms, duration_ms)))
                continue
            offset = start - root_start
            begin = max(0.0, offset)
            end = min(total_ms, offset + duration_ms)
            if end > begin:
                buckets.setdefault(bucket, []).append((begin, end))

        external_ms = {name: 0.0 for name in buckets}
        if root_start is None:
            for name, intervals in buckets.items():
                external_ms[name] = min(total_ms, sum(end - start for start, end in intervals))
        else:
            boundaries = sorted({point for intervals in buckets.values() for interval in intervals for point in interval})
            for start, end in zip(boundaries, boundaries[1:]):
                if end <= start:
                    continue
                covering = next((name for name in sorted(buckets) if any(left <= start and right >= end for left, right in buckets[name])), None)
                if covering is not None:
                    external_ms[covering] += end - start
        breakdown = {name: round(value, 3) for name, value in sorted(external_ms.items())}
        breakdown["php_app_time_ms"] = round(max(0.0, total_ms - sum(external_ms.values())), 3)
        hotspots, profile_event_type = self._trace_profile_hotspots(project_id, revision_id, trace_id)
        endpoint = f"{request.get('method') or 'UNKNOWN'} {request.get('route') or 'unknown route'}"
        if hotspots:
            hottest = hotspots[0]
            insight = (
                f"{endpoint} took {total_ms:.1f} ms; the largest retained profile hotspot was "
                f"{hottest['frame']} ({hottest['sample_percentage']:.1f}% of samples)."
            )
        else:
            insight = f"{endpoint} took {total_ms:.1f} ms; no retained Excimer profile was available for this trace."
        return {
            "trace_id": trace_id.lower(),
            "endpoint": endpoint,
            "total_latency_ms": round(total_ms, 3),
            "otel_breakdown": breakdown,
            "excimer_hotspots": hotspots,
            "profile_event_type": profile_event_type,
            "actionable_insight": insight,
        }

    def _trace_profile_hotspots(self, project_id: str, revision_id: str, trace_id: str) -> tuple[list[dict], str | None]:
        if not self.configured:
            return [], None
        table = f"`{self.database}`.`{self.profile_table}`"
        try:
            rows = self.rows(
                f"SELECT samples_collapsed, event_type FROM {table} FINAL WHERE project_id = {{project_id:String}} "
                "AND revision_id = {revision_id:String} AND trace_id = {trace_id:String} "
                "ORDER BY received_at DESC LIMIT 1",
                project_id, revision_id, {"param_trace_id": trace_id.lower()},
            )
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError):
            return [], None
        if not rows or not isinstance(rows[0].get("samples_collapsed"), str):
            return [], None
        frame_samples: dict[str, int] = {}
        total_samples = 0
        method_id = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\\[A-Za-z_][A-Za-z0-9_]*)*::[A-Za-z_][A-Za-z0-9_]*$")
        for line in rows[0]["samples_collapsed"].splitlines()[:2000]:
            try:
                stack_text, count_text = line.rsplit(" ", 1)
                count = int(count_text)
            except (ValueError, TypeError):
                continue
            if not 1 <= count <= 1_000_000:
                continue
            frames = set(stack_text.split(";"))
            frames = {frame for frame in frames if method_id.fullmatch(frame)}
            if not frames:
                continue
            total_samples += count
            for frame in frames:
                frame_samples[frame] = frame_samples.get(frame, 0) + count
        if total_samples <= 0:
            return [], None
        return [
            {"frame": frame, "sample_percentage": round(100 * count / total_samples, 1)}
            for frame, count in sorted(frame_samples.items(), key=lambda item: (-item[1], item[0]))[:10]
        ], str(rows[0].get("event_type") or "") or None

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

    def execute(
        self,
        sql: str,
        body: bytes | None = None,
        extra_parameters: dict[str, str] | None = None,
        timeout: int = 15,
    ) -> None:
        if not self.configured:
            raise RuntimeError("ClickHouse is not configured")
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode("ascii")
        query = urlencode({"database": self.database, **(extra_parameters or {}), **({"query": sql} if body is not None else {})})
        request = Request(
            f"{self.url}/?{query}",
            data=body if body is not None else sql.encode("utf-8"),
            headers={"Authorization": f"Basic {token}", "Content-Type": "text/plain"},
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:
            response.read()

    def delete_requests(self, project_id: str, revision_id: str, trace_ids: list[str]) -> dict:
        """Permanently remove the selected request traces within one indexed revision."""
        if not self.configured:
            raise RuntimeError("ClickHouse is not configured")
        if not isinstance(trace_ids, list) or not 1 <= len(trace_ids) <= 50:
            raise ValueError("Select between 1 and 50 request traces")

        normalized_ids = list(dict.fromkeys(
            trace_id.lower() for trace_id in trace_ids
            if isinstance(trace_id, str) and re.fullmatch(r"[0-9a-fA-F]{32}", trace_id)
        ))
        if len(normalized_ids) != len(trace_ids):
            raise ValueError("Request trace IDs are invalid")
        current_requests = self.requests(project_id, revision_id)
        if not current_requests.get("available"):
            raise RuntimeError("Runtime request telemetry is unavailable")
        allowed_ids = {request["trace_id"] for request in current_requests.get("requests", [])}
        if not set(normalized_ids).issubset(allowed_ids):
            raise ValueError("Only currently listed request traces can be deleted")

        parameters = {
            # Lightweight DELETE masks the rows before returning; merges drop them later. Unlike
            # ALTER TABLE ... DELETE, it does not rewrite every data part the trace touches.
            "lightweight_deletes_sync": "2",
            "param_project_id": project_id,
            "param_revision_id": revision_id,
        }
        placeholders = []
        for index, trace_id in enumerate(normalized_ids):
            parameter_name = f"trace_id_{index}"
            placeholders.append(f"{{{parameter_name}:String}}")
            parameters[f"param_{parameter_name}"] = trace_id

        table = f"`{self.database}`.`{self.table}`"
        sql = (
            f"DELETE FROM {table} WHERE {_SCOPE} "
            f"AND TraceId IN ({', '.join(placeholders)})"
        )
        try:
            from telemetry.profiles import delete_profiles
            from telemetry.rollups import hours_of_traces, invalidate_hours
            trace_parameters = {key: value for key, value in parameters.items() if key.startswith("param_trace_id_")}
            hours = hours_of_traces(self, project_id, revision_id, normalized_ids, trace_parameters)
            delete_profiles(self, project_id, revision_id, normalized_ids)
            self.execute(sql, extra_parameters=parameters, timeout=120)
            # The deleted spans are still counted in those hours' rollups; drop them so the
            # next aggregation cycle rolls the hours up again from what remains.
            invalidate_hours(self, project_id, revision_id, hours)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise RuntimeError("Request telemetry could not be deleted") from exc
        return {"deleted_trace_count": len(normalized_ids)}

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

    def span_signature(self, project_id: str, revision_id: str) -> tuple[int, str] | None:
        """Span count and newest span time in the summary window; None when it cannot be read.

        One small query that changes whenever summary() could change: new spans raise the count or
        the newest time, and spans ageing out of the window lower the count.
        """
        if not self.configured:
            return None
        table = f"`{self.database}`.`{self.table}`"
        try:
            rows = self.rows(
                "SELECT count() AS spans, toString(max(Timestamp)) AS newest FROM " + table + " WHERE " + _SCOPE +
                f" AND Timestamp >= now() - INTERVAL {self.window_hours} HOUR",
                project_id, revision_id,
            )
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError):
            return None
        row = rows[0] if rows else {}
        return int(row.get("spans", 0) or 0), str(row.get("newest", ""))

    def summary(self, project_id: str, revision_id: str, known_symbol_ids: set[str]) -> dict:
        if not self.configured:
            return self._empty("ClickHouse is not configured.")
        try:
            # Closed hours come from hourly rollups; only hours not rolled up yet are read raw.
            from telemetry.rollups import read_summary
            grouped = read_summary(self, project_id, revision_id)
            sampling = _sampling(grouped["all"])
            totals = [{
                "spans": sum(int(row["calls"]) for row in grouped["all"]),
                "symbol_spans": sum(int(row["calls"]) for row in grouped["symbol"]),
                "errors": sum(int(row["failures"]) for row in grouped["all"]),
                "last_seen": max((row["last_seen"] for row in grouped["all"]), default=None),
            }]
            symbols = _top([
                {"symbol_id": row["k1"], "runs": int(row["calls"]), "last_seen": row["last_seen"],
                 "p95_ms": row["p95_ms"], "failures": int(row["failures"])}
                for row in grouped["symbol"]
            ], "runs", 5000)
            routes = _top([
                {"method": row["k1"], "route": row["k2"], "requests": int(row["calls"]), "route_id": row["k3"],
                 "p95_ms": row["p95_ms"], "failures": int(row["failures"]), "last_seen": row["last_seen"]}
                for row in grouped["route"]
            ], "requests", 1000)
            tables = _top([
                {"table_name": row["k1"], "operation": row["k2"], "operations": int(row["calls"]), "last_seen": row["last_seen"]}
                for row in grouped["table"]
            ], "operations", 1000)
            parents = _top([
                {"symbol_id": row["k1"], "caller_symbol_id": row["k2"], "calls": int(row["calls"])}
                for row in grouped["parent"]
            ], "calls", 5000)
            from telemetry.profiles import merge_method_edges, method_hook_edges, profile_edges
            profile_data = profile_edges(self, project_id, revision_id, known_symbol_ids)
            hook_edges = method_hook_edges(self, project_id, revision_id, known_symbol_ids)
            method_calls = merge_method_edges(parents, hook_edges, profile_data)
            symbol_routes = _top([
                {"symbol_id": row["k1"], "route": row["k2"], "calls": int(row["calls"])}
                for row in grouped["symbol_route"]
            ], "calls", 5000)
            symbol_tables = _top([
                {"symbol_id": row["k1"], "table_name": row["k2"], "calls": int(row["calls"])}
                for row in grouped["symbol_table"]
            ], "calls", 5000)
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
            "method_calls": method_calls,
            "hot_path": [
                {"symbol_id": symbol_id, "sample_share": share}
                for symbol_id, share in sorted(profile_data.get("hot_path_share", {}).items(), key=lambda item: item[1], reverse=True)[:5000]
            ],
            "profile_count": int(profile_data.get("profiles", 0) or 0),
            "profiled_samples": int(profile_data.get("samples", 0) or 0),
            "symbol_routes": symbol_routes,
            "symbol_tables": symbol_tables,
            "retention_hours": runtime_sampling_policy()["aggregation_retention_hours"],
            "sampled_trace_retention_hours": telemetry_settings()["retention_hours"],
            "aggregation_window_hours": self.window_hours,
            "pipeline_health": pipeline_health,
            **sampling,
        }

    @staticmethod
    def _empty(reason: str) -> dict:
        return {
            "available": False, "reason": reason, "total_spans": 0,
            "symbol_spans": 0, "matched_spans": 0, "match_percent": 0.0,
            "error_spans": 0, "last_seen": None, "symbols": [], "routes": [],
            "tables": [], "callers": [], "method_calls": [], "hot_path": [], "profile_count": 0,
            "profiled_samples": 0, "symbol_routes": [], "symbol_tables": [],
            "retention_hours": runtime_sampling_policy()["aggregation_retention_hours"],
            "sampled_trace_retention_hours": telemetry_settings()["retention_hours"],
            "unmatched_spans": 0, "unmatched_percent": 0.0,
            "aggregation_window_hours": runtime_sampling_policy()["p95_window_hours"],
            "pipeline_health": "unavailable",
            "sampling_ratio": None, "sampling_ratios": [], "estimated_total_spans": None,
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
    if index_available and commit_sha and revision_id:
        try:
            from inspection.graph import InspectionGraph
            from telemetry.snapshot import storage_id
            collection = "sym_snapshot_" + storage_id(project_id, commit_sha, "")
            static_edges = InspectionGraph().static_call_edges(collection)
            observed_edges = {
                (row.get("caller_symbol_id"), row.get("symbol_id"))
                for row in telemetry.get("method_calls", [])
            }
            expected_edges = {
                (row["caller_symbol_id"], row["symbol_id"])
                for row in static_edges
                if row["caller_symbol_id"] in known_ids and row["symbol_id"] in known_ids
            }
            telemetry["method_edge_coverage"] = {
                "observed_edges": len(expected_edges & observed_edges),
                "static_edges": len(expected_edges),
                "percent": round(100 * len(expected_edges & observed_edges) / len(expected_edges), 1) if expected_edges else None,
                "basis": "runtime samples within the selected aggregation window",
            }
        except Exception:
            telemetry["method_edge_coverage"] = {"observed_edges": 0, "static_edges": 0, "percent": None, "basis": "static graph unavailable"}
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
