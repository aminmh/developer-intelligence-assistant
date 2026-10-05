from __future__ import annotations

import asyncio

from telemetry.aggregation import _graph_facts
from telemetry.events import publish_runtime_update, runtime_events
from telemetry.registry import SnapshotRegistry
from telemetry.runtime import runtime_sampling_policy, update_runtime_sampling_policy
from telemetry.snapshot import storage_id


def test_sampling_controls_are_persisted_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_AGGREGATION_RETENTION_HOURS", "96")
    registry = SnapshotRegistry(tmp_path)

    defaults = runtime_sampling_policy(registry)
    assert defaults["p95_window_hours"] == 24
    assert defaults["refresh_minutes"] == 5

    updated = update_runtime_sampling_policy(
        {"p95_window_hours": 72, "refresh_minutes": 15}, registry
    )
    assert updated["p95_window_hours"] == 72
    assert updated["refresh_minutes"] == 15
    assert runtime_sampling_policy(registry)["p95_window_hours"] == 72


def test_graph_facts_keep_canonical_route_and_table_identity():
    project_id = "billing"
    commit_sha = "a" * 40
    symbol_id = "App\\Orders\\OrderService::store"
    route_id = "route:POST /orders/{order}"
    table_id = "table:mysql.orders"
    telemetry = {
        "aggregation_window_hours": 12,
        "symbols": [{"symbol_id": symbol_id, "runs": 8, "p95_ms": 25.0, "error_rate": 12.5}],
        "callers": [],
        "routes": [{"route_id": route_id, "method": "POST", "route": "/orders/{order}",
                    "p95_ms": 45.0, "error_rate": 0.0}],
        "symbol_routes": [{"symbol_id": symbol_id, "route": route_id, "calls": 8}],
        "tables": [{"table_id": table_id, "table_name": "orders", "operation": "SELECT", "operations": 8}],
        "symbol_tables": [{"symbol_id": symbol_id, "table_id": table_id, "table_name": "orders", "calls": 8}],
    }

    facts = _graph_facts(
        project_id, "revision", commit_sha, "sym_snapshot", [{"symbol_id": symbol_id}], telemetry
    )

    assert facts["symbols"][0]["window_hours"] == 12
    assert facts["routes"][0]["properties"]["route_id"] == route_id
    assert facts["routes"][0]["id"] == storage_id(project_id, commit_sha, route_id)
    assert facts["tables"][0]["properties"]["table_id"] == table_id
    assert facts["tables"][0]["properties"]["table_name"] == "orders"
    assert facts["tables"][0]["id"] == storage_id(project_id, commit_sha, table_id)


def test_runtime_events_publish_invalidation_without_project_data():
    async def receive_event():
        stream = runtime_events()
        try:
            assert "event: connected" in await stream.__anext__()
            publish_runtime_update()
            return await asyncio.wait_for(stream.__anext__(), timeout=0.1)
        finally:
            await stream.aclose()

    event = asyncio.run(receive_event())
    assert "event: runtime.updated" in event
    assert '"type":"runtime.updated"' in event
    assert "project_id" not in event
