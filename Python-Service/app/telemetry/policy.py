"""Publish per-project rolling p95 thresholds for the runtime Collector."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from telemetry.runtime import ClickHouseRuntime, runtime_sampling_policy
from telemetry.registry import default_registry


def refresh_sampling_policy(registry=None) -> dict:
    registry = registry or default_registry()
    controls = runtime_sampling_policy(registry)
    clickhouse = ClickHouseRuntime()
    if not clickhouse.configured:
        raise RuntimeError("ClickHouse is not configured")
    table = f"`{clickhouse.database}`.`{clickhouse.table}`"
    hours = controls["p95_window_hours"]
    rows = clickhouse.unscoped_rows(
        f"SELECT ResourceAttributes['indexer.project_id'] AS project_id, "
        f"quantile(0.95)(toFloat64(Duration)) / 1000000 AS p95_ms FROM {table} "
        "WHERE SpanKind = 'Server' "
        "AND ResourceAttributes['indexer.project_id'] != '' "
        "AND ResourceAttributes['indexer.revision_id'] != '' "
        "AND ResourceAttributes['indexer.code_dirty'] = 'false' "
        "AND ResourceAttributes['indexer.schema.version'] = 'indexer-telemetry-v2' "
        f"AND Timestamp >= now() - INTERVAL {hours} HOUR "
        "GROUP BY project_id"
    )
    thresholds = {
        str(row["project_id"]): max(0.0, float(row["p95_ms"]))
        for row in rows
        if row.get("project_id") and row.get("p95_ms") is not None
    }
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
    return {"updated_at": document["updated_at"], "project_count": len(thresholds), "thresholds_ms": thresholds}
