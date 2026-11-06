"""Publish per-project rolling p95 thresholds for the runtime Collector."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from telemetry.runtime import ClickHouseRuntime, runtime_sampling_policy
from telemetry.registry import default_registry


# Per-route slow thresholds: the busiest routes per project with enough requests for a stable p95.
ROUTE_THRESHOLDS_PER_PROJECT = 50
ROUTE_THRESHOLD_MIN_REQUESTS = 20


def refresh_sampling_policy(registry=None) -> dict:
    registry = registry or default_registry()
    controls = runtime_sampling_policy(registry)
    clickhouse = ClickHouseRuntime()
    if not clickhouse.configured:
        raise RuntimeError("ClickHouse is not configured")
    table = f"`{clickhouse.database}`.`{clickhouse.table}`"
    hours = controls["p95_window_hours"]
    rows = clickhouse.unscoped_rows(
        f"SELECT ProjectId AS project_id, "
        f"quantile(0.95)(toFloat64(Duration)) / 1000000 AS p95_ms FROM {table} "
        "WHERE SpanKind = 'Server' "
        "AND ProjectId != '' AND RevisionId != '' AND Eligible "
        f"AND Timestamp >= now() - INTERVAL {hours} HOUR "
        "GROUP BY project_id"
    )
    thresholds = {
        str(row["project_id"]): max(0.0, float(row["p95_ms"]))
        for row in rows
        if row.get("project_id") and row.get("p95_ms") is not None
    }
    # A slow request on a fast route never crosses the project-wide p95, so each project's busiest
    # routes get their own threshold. Routes with too few requests keep the project p95.
    route_rows = clickhouse.unscoped_rows(
        "SELECT ProjectId AS project_id, "
        "concat(upper(SpanAttributes['http.request.method']), ' ', HttpRoute) AS route, count() AS requests, "
        f"quantile(0.95)(toFloat64(Duration)) / 1000000 AS p95_ms FROM {table} "
        "WHERE SpanKind = 'Server' AND HttpRoute != '' AND ProjectId != '' AND RevisionId != '' AND Eligible "
        f"AND Timestamp >= now() - INTERVAL {hours} HOUR "
        f"GROUP BY project_id, route HAVING requests >= {ROUTE_THRESHOLD_MIN_REQUESTS} "
        f"ORDER BY project_id, requests DESC LIMIT {ROUTE_THRESHOLDS_PER_PROJECT} BY project_id"
    )
    route_thresholds: dict[str, dict[str, float]] = {}
    for row in route_rows:
        if row.get("project_id") and row.get("route") and row.get("p95_ms") is not None:
            route_thresholds.setdefault(str(row["project_id"]), {})[str(row["route"])] = max(0.0, float(row["p95_ms"]))
    try:
        baseline_percent = float(os.getenv("TELEMETRY_RECORDING_PERCENT", "10"))
    except ValueError:
        baseline_percent = 10.0
    document = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "window_hours": hours,
        "refresh_minutes": controls["refresh_minutes"],
        "baseline_percent": round(min(100.0, max(0.0, baseline_percent)), 1),
        "thresholds_ms": thresholds,
        "route_thresholds_ms": route_thresholds,
    }
    target = Path(os.getenv("TELEMETRY_SAMPLING_POLICY_PATH", "/var/lib/devassistant/runtime/sampling-policy.json"))
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return {"updated_at": document["updated_at"], "project_count": len(thresholds), "thresholds_ms": thresholds,
            "route_thresholds_ms": route_thresholds}
