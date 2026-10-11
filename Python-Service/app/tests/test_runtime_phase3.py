from __future__ import annotations

import asyncio

from telemetry.aggregation import _graph_facts
from telemetry.events import publish_runtime_update, runtime_events
from telemetry.registry import SnapshotRegistry
from telemetry.runtime import ClickHouseRuntime, runtime_sampling_policy, update_runtime_sampling_policy
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


def test_runtime_summary_compares_clickhouse_span_status_as_text(monkeypatch):
    queries = []

    def rows(_self, sql, _project_id, _revision_id):
        queries.append(sql)
        if "AS spans" in sql:
            return [{"spans": 0, "symbol_spans": 0, "errors": 0, "last_seen": None}]
        return []

    monkeypatch.setattr(ClickHouseRuntime, "configured", property(lambda _self: True))
    monkeypatch.setattr(ClickHouseRuntime, "rows", rows)
    monkeypatch.setattr(
        "telemetry.runtime.runtime_sampling_policy",
        lambda _registry=None: {
            "p95_window_hours": 24,
            "refresh_minutes": 5,
            "aggregation_retention_hours": 168,
        },
    )

    summary = ClickHouseRuntime().summary("pizza", "rev_test", set())

    assert summary["available"] is True
    assert summary["total_spans"] == 0
    assert summary["reason"] == "No runtime spans have arrived for this project commit."
    status_queries = [query for query in queries if "countIf(StatusCode" in query]
    assert len(status_queries) == 3
    assert all("StatusCode = 'Error'" in query for query in status_queries)


def test_runtime_request_reads_are_revision_scoped_and_privacy_shaped(monkeypatch):
    captured = []

    def rows(self, sql, project_id, revision_id, extra_parameters=None):
        captured.append((sql, project_id, revision_id, extra_parameters))
        if "ParentSpanId AS parent_span_id" in sql:
            return [
                {"trace_id": "a" * 32, "span_id": "1" * 16, "parent_span_id": "", "span_kind": "Server",
                 "method": "GET", "route": "/api/pizzas", "status_code": 200, "duration_ms": 12.5,
                 "php_cpu_time_us": 900, "php_peak_memory_bytes": 65536, "timestamp": "2026-10-08 10:00:00", "span_status": "Unset"},
                {"trace_id": "a" * 32, "span_id": "2" * 16, "parent_span_id": "1" * 16, "span_kind": "Client",
                 "db_system": "mysql", "db_namespace": "pizza", "db_operation": "SELECT",
                 "table_ids": '["table:mysql.pizzas"]', "symbol_id": "method:PizzaRepository::all", "duration_ms": 2.0,
                 "timestamp": "2026-10-08 10:00:00", "span_status": "Unset", "db.query.text": "SELECT secret"},
            ]
        return [{"trace_id": "a" * 32, "span_id": "1" * 16, "timestamp": "2026-10-08 10:00:00",
                 "method": "GET", "route": "/api/pizzas", "status_code": 200, "duration_ms": 12.5,
                 "php_cpu_time_us": 900, "php_peak_memory_bytes": 65536, "span_status": "Unset"}]

    monkeypatch.setattr(ClickHouseRuntime, "configured", property(lambda _self: True))
    monkeypatch.setattr(ClickHouseRuntime, "rows", rows)
    monkeypatch.setattr(
        "telemetry.runtime.runtime_sampling_policy",
        lambda _registry=None: {"p95_window_hours": 24, "refresh_minutes": 5, "aggregation_retention_hours": 168},
    )
    clickhouse = ClickHouseRuntime()

    listing = clickhouse.requests("pizza", "rev_test")
    detail = clickhouse.request_detail("pizza", "rev_test", "A" * 32)

    assert "GROUP BY TraceId" in captured[0][0]
    assert "argMin(SpanId, tuple(Timestamp, SpanId)) AS span_id" in captured[0][0]
    assert "LIMIT 50" in captured[0][0]
    assert listing["requests"][0]["route"] == "/api/pizzas"
    assert listing["requests"][0]["php_cpu_time_us"] == 900
    assert "db.query.text" not in listing["requests"][0]
    assert detail["database_calls"][0]["table_ids"] == ["table:mysql.pizzas"]
    assert detail["database_calls"][0]["db_namespace"] == "pizza"
    assert "db.query.text" not in detail["database_calls"][0]
    for sql, project_id, revision_id, params in captured:
        assert project_id == "pizza" and revision_id == "rev_test"
        if "`telemetry`.`otel_spans_full`" in sql:
            assert "indexer.project_id" in sql and "indexer.revision_id" in sql
        elif "`telemetry`.`otel_profiles`" in sql:
            assert "project_id = {project_id:String}" in sql
            assert "revision_id = {revision_id:String}" in sql
    assert captured[1][3] == {"param_trace_id": "a" * 32}


def test_runtime_request_detail_rejects_malformed_trace_id(monkeypatch):
    import pytest
    monkeypatch.setattr(
        "telemetry.runtime.runtime_sampling_policy",
        lambda _registry=None: {"p95_window_hours": 24, "refresh_minutes": 5, "aggregation_retention_hours": 168},
    )

    with pytest.raises(ValueError, match="Invalid trace ID"):
        ClickHouseRuntime().request_detail("pizza", "rev_test", "not-a-trace")


def test_runtime_request_delete_is_bounded_parameterized_and_revision_scoped(monkeypatch):
    captured = []
    trace_ids = ["a" * 32, "B" * 32]
    monkeypatch.setattr(
        "telemetry.runtime.runtime_sampling_policy",
        lambda _registry=None: {"p95_window_hours": 24, "refresh_minutes": 5, "aggregation_retention_hours": 168},
    )
    monkeypatch.setattr(ClickHouseRuntime, "configured", property(lambda _self: True))
    monkeypatch.setattr(ClickHouseRuntime, "rows", lambda *_args, **_kwargs: [{"tables": 1}])
    monkeypatch.setattr(
        ClickHouseRuntime,
        "execute",
        lambda self, sql, body=None, extra_parameters=None, timeout=15: captured.append((sql, extra_parameters, timeout)),
    )
    monkeypatch.setattr(
        ClickHouseRuntime,
        "requests",
        lambda self, project_id, revision_id: {
            "available": True,
            "requests": [{"trace_id": "a" * 32}, {"trace_id": "b" * 32}],
        },
    )

    result = ClickHouseRuntime().delete_requests("pizza", "rev_test", trace_ids)

    profile_sql, profile_parameters, _profile_timeout = next(
        item for item in captured if "`telemetry`.`otel_profiles`" in item[0]
    )
    sql, parameters, timeout = next(
        item for item in captured if "`telemetry`.`otel_spans_full`" in item[0]
    )
    assert result == {"deleted_trace_count": 2}
    assert "trace_id IN ({trace_id_0:String}, {trace_id_1:String})" in profile_sql
    assert profile_parameters["param_trace_id_0"] == "a" * 32
    assert profile_parameters["param_trace_id_1"] == "b" * 32
    assert "ALTER TABLE `telemetry`.`otel_spans_full` DELETE WHERE" in sql
    assert "indexer.project_id" in sql and "indexer.revision_id" in sql
    assert "TraceId IN ({trace_id_0:String}, {trace_id_1:String})" in sql
    assert all(trace_id.lower() not in sql for trace_id in trace_ids)
    assert parameters == {
        "mutations_sync": "1",
        "param_project_id": "pizza",
        "param_revision_id": "rev_test",
        "param_trace_id_0": "a" * 32,
        "param_trace_id_1": "b" * 32,
    }
    assert timeout == 120


def test_runtime_request_delete_rejects_unbounded_or_invalid_trace_ids(monkeypatch):
    import pytest

    monkeypatch.setattr(
        "telemetry.runtime.runtime_sampling_policy",
        lambda _registry=None: {"p95_window_hours": 24, "refresh_minutes": 5, "aggregation_retention_hours": 168},
    )
    monkeypatch.setattr(ClickHouseRuntime, "configured", property(lambda _self: True))
    monkeypatch.setattr(ClickHouseRuntime, "execute", lambda *_args, **_kwargs: pytest.fail("delete must not run"))
    monkeypatch.setattr(
        ClickHouseRuntime,
        "requests",
        lambda *_args, **_kwargs: {"available": True, "requests": [{"trace_id": "a" * 32}]},
    )

    with pytest.raises(ValueError, match="between 1 and 50"):
        ClickHouseRuntime().delete_requests("pizza", "rev_test", [])
    with pytest.raises(ValueError, match="invalid"):
        ClickHouseRuntime().delete_requests("pizza", "rev_test", ["not-a-trace"])


def test_runtime_request_delete_rejects_traces_outside_the_current_list(monkeypatch):
    import pytest

    monkeypatch.setattr(
        "telemetry.runtime.runtime_sampling_policy",
        lambda _registry=None: {"p95_window_hours": 24, "refresh_minutes": 5, "aggregation_retention_hours": 168},
    )
    monkeypatch.setattr(ClickHouseRuntime, "configured", property(lambda _self: True))
    monkeypatch.setattr(ClickHouseRuntime, "execute", lambda *_args, **_kwargs: pytest.fail("delete must not run"))
    monkeypatch.setattr(
        ClickHouseRuntime,
        "requests",
        lambda *_args, **_kwargs: {"available": True, "requests": [{"trace_id": "a" * 32}]},
    )

    with pytest.raises(ValueError, match="currently listed"):
        ClickHouseRuntime().delete_requests("pizza", "rev_test", ["b" * 32])


def test_runtime_delete_endpoint_publishes_refresh_after_success(monkeypatch, tmp_path):
    monkeypatch.setenv("SETTINGS_DB_PATH", str(tmp_path / "settings.db"))
    from telemetry import events, http

    class Registry:
        def get_by_revision_id(self, project_id, revision_id):
            return {"project_id": project_id, "revision_id": revision_id}

    deleted = []
    published = []
    monkeypatch.setattr(http, "authorize_internal", lambda _request: None)
    monkeypatch.setattr(http, "get_registry", Registry)
    monkeypatch.setattr(
        "telemetry.runtime.runtime_sampling_policy",
        lambda _registry=None: {"p95_window_hours": 24, "refresh_minutes": 5, "aggregation_retention_hours": 168},
    )
    monkeypatch.setattr(
        ClickHouseRuntime,
        "delete_requests",
        lambda self, project_id, revision_id, trace_ids: deleted.append((project_id, revision_id, trace_ids))
        or {"deleted_trace_count": len(trace_ids)},
    )
    monkeypatch.setattr(events, "publish_runtime_update", lambda: published.append(True))

    revision_id = "rev_" + "c" * 32
    result = http.runtime_delete_requests("pizza", revision_id, None, {"trace_ids": ["a" * 32]})

    assert deleted == [("pizza", revision_id, ["a" * 32])]
    assert published == [True]
    assert result == {"deleted_trace_count": 1}


def test_runtime_request_detail_exposes_bounded_method_hook_counts(monkeypatch):
    captured = []
    hook_counts = (
        '[{"caller_symbol_id":"App\\\\Http\\\\Controllers\\\\PizzaController::index",'
        '"symbol_id":"App\\\\Services\\\\PizzaService::list", "calls":3}]'
    )

    def rows(self, sql, project_id, revision_id, extra_parameters=None):
        captured.append((sql, project_id, revision_id, extra_parameters))
        return [{
            "trace_id": "a" * 32,
            "span_id": "1" * 16,
            "parent_span_id": "",
            "span_kind": "Server",
            "timestamp": "2026-10-08 10:00:00",
            "method": "GET",
            "route": "/api/pizzas",
            "status_code": 200,
            "duration_ms": 12.5,
            "span_status": "Unset",
            "method_hook_counts": hook_counts,
        }]

    monkeypatch.setattr(ClickHouseRuntime, "configured", property(lambda _self: True))
    monkeypatch.setattr(ClickHouseRuntime, "rows", rows)
    monkeypatch.setattr(
        ClickHouseRuntime,
        "_trace_ai_summary",
        lambda *_args: {"excimer_hotspots": []},
    )
    monkeypatch.setattr(
        "telemetry.runtime.runtime_sampling_policy",
        lambda _registry=None: {
            "p95_window_hours": 24,
            "refresh_minutes": 5,
            "aggregation_retention_hours": 168,
        },
    )

    detail = ClickHouseRuntime().request_detail("pizza", "rev_test", "a" * 32)

    assert "indexer.method_hook_counts" in captured[0][0]
    assert captured[0][1:3] == ("pizza", "rev_test")
    assert detail["method_activity"] == {
        "available": True,
        "reason": None,
        "edges": [{
            "caller_symbol_id": "App\\Http\\Controllers\\PizzaController::index",
            "symbol_id": "App\\Services\\PizzaService::list",
            "calls": 3,
            "source": "hook",
        }],
    }


def test_runtime_method_activity_omits_invalid_edges_and_reports_missing_data():
    from telemetry.runtime import _method_activity

    activity = _method_activity([
        {"caller_symbol_id": "App\\Caller::run", "symbol_id": "App\\Callee::work", "calls": 2},
        {"caller_symbol_id": "../../secret", "symbol_id": "App\\Callee::work", "calls": 9},
        {"caller_symbol_id": "App\\Caller::run", "symbol_id": "App\\Callee::work", "calls": True},
    ])
    missing = _method_activity(None)

    assert activity["available"] is True
    assert activity["edges"][0]["calls"] == 2
    assert len(activity["edges"]) == 1
    assert missing == {
        "available": False,
        "reason": "No method-call data was captured for this request.",
        "edges": [],
    }
