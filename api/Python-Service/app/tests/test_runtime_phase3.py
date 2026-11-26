from __future__ import annotations

import asyncio
import socket

import pytest

from telemetry import runtime_storage
from telemetry.aggregation import _graph_facts
from telemetry.events import publish_runtime_update, runtime_events
from telemetry.registry import SnapshotRegistry
from telemetry.runtime import ClickHouseRuntime, runtime_sampling_policy, update_runtime_sampling_policy
from telemetry.snapshot import storage_id


def test_stale_collector_restart_socket_is_treated_as_no_active_relay(tmp_path, monkeypatch):
    socket_path = tmp_path / "collector-reload.sock"
    socket_path.touch()
    monkeypatch.setattr(runtime_storage, "_CONTROL_DIR", tmp_path)

    class RefusedClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            raise ConnectionRefusedError

    monkeypatch.setattr(socket, "socket", lambda *_args: RefusedClient())

    assert runtime_storage.request_collector_restart() is False


def test_active_collector_restart_socket_still_requests_restart(tmp_path, monkeypatch):
    socket_path = tmp_path / "collector-reload.sock"
    socket_path.touch()
    monkeypatch.setattr(runtime_storage, "_CONTROL_DIR", tmp_path)

    class ActiveClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, _timeout):
            pass

        def connect(self, path):
            assert path == str(socket_path)

        def sendall(self, command):
            assert command == b"restart\n"

        def recv(self, _size):
            return b"restarting\n"

    monkeypatch.setattr(socket, "socket", lambda *_args: ActiveClient())

    assert runtime_storage.request_collector_restart() is True


def _configured_disabled_remote_target(registry):
    registry.set_runtime_setting(runtime_storage.STORAGE_KEY, {
        "mode": "disabled",
        "host": "clickhouse.example.test",
        "ssh_port": 22,
        "ssh_user": "telemetry",
        "host_fingerprint": "SHA256:test-fingerprint",
        "health": "disabled",
        "verified_at": None,
        "last_error": None,
    })


def test_enabling_remote_applies_tunnel_target_before_verification(tmp_path, monkeypatch):
    registry = SnapshotRegistry(tmp_path)
    _configured_disabled_remote_target(registry)
    monkeypatch.setenv("TELEMETRY_CLICKHOUSE_USER", "telemetry")
    monkeypatch.setenv("TELEMETRY_CLICKHOUSE_PASSWORD", "x" * 40)
    applied_modes = []
    monkeypatch.setattr(runtime_storage, "write_router_target", lambda target: applied_modes.append(target["mode"]))
    monkeypatch.setattr(runtime_storage, "write_ssh_target", lambda _target: None)
    monkeypatch.setattr(runtime_storage, "write_collector_config", lambda _target: None)
    monkeypatch.setattr(runtime_storage, "request_collector_restart", lambda: False)

    def verify_remote():
        assert applied_modes[-1] == "remote"
        assert runtime_storage.get_storage_target(registry)["mode"] == "remote"

    monkeypatch.setattr(runtime_storage, "verify_router_clickhouse_connection", verify_remote)

    result = runtime_storage.set_storage_mode("remote", registry)

    assert result["mode"] == "remote"
    assert applied_modes == ["remote"]


def test_failed_remote_verification_restores_previous_disabled_target(tmp_path, monkeypatch):
    registry = SnapshotRegistry(tmp_path)
    _configured_disabled_remote_target(registry)
    monkeypatch.setenv("TELEMETRY_CLICKHOUSE_USER", "telemetry")
    monkeypatch.setenv("TELEMETRY_CLICKHOUSE_PASSWORD", "x" * 40)
    applied_modes = []
    monkeypatch.setattr(runtime_storage, "write_router_target", lambda target: applied_modes.append(target["mode"]))
    monkeypatch.setattr(runtime_storage, "write_ssh_target", lambda _target: None)
    monkeypatch.setattr(runtime_storage, "write_collector_config", lambda _target: None)
    monkeypatch.setattr(runtime_storage, "request_collector_restart", lambda: False)

    def fail_verification():
        assert runtime_storage.get_storage_target(registry)["mode"] == "remote"
        raise RuntimeError("Remote endpoint is unavailable")

    monkeypatch.setattr(runtime_storage, "verify_router_clickhouse_connection", fail_verification)

    try:
        runtime_storage.set_storage_mode("remote", registry)
    except RuntimeError as exc:
        assert str(exc) == "Remote endpoint is unavailable"
    else:
        raise AssertionError("Expected failed Remote verification to be propagated")

    assert runtime_storage.get_storage_target(registry)["mode"] == "disabled"
    assert applied_modes == ["remote", "disabled"]


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


def test_runtime_summary_reads_rollups_and_live_spans_in_one_query(monkeypatch):
    queries = []

    def rows(_self, sql, _project_id, _revision_id, _extra=None):
        queries.append(sql)
        return [
            {"kind": "all", "k1": "", "k2": "", "k3": "", "calls": 12, "failures": 2,
             "last_seen": "2026-11-06 12:00:00.000000000", "p95_ms": 0},
            {"kind": "symbol", "k1": "App\\A::run", "k2": "", "k3": "", "calls": 5, "failures": 1,
             "last_seen": "2026-11-06 12:00:00.000000000", "p95_ms": 4.5},
            {"kind": "route", "k1": "GET", "k2": "/a", "k3": "route:GET /a", "calls": 4, "failures": 0,
             "last_seen": "2026-11-06 12:00:00.000000000", "p95_ms": 9.0},
            {"kind": "parent", "k1": "App\\A::run", "k2": "App\\B::call", "k3": "", "calls": 3, "failures": 0,
             "last_seen": "2026-11-06 12:00:00.000000000", "p95_ms": 0},
            {"kind": "symbol_route", "k1": "App\\A::run", "k2": "route:GET /a", "k3": "", "calls": 5, "failures": 0,
             "last_seen": "2026-11-06 12:00:00.000000000", "p95_ms": 0},
        ]

    monkeypatch.setattr(ClickHouseRuntime, "configured", property(lambda _self: True))
    monkeypatch.setattr(ClickHouseRuntime, "rows", rows)
    monkeypatch.setattr("telemetry.profiles.profile_edges", lambda *_args: {"edges": [], "samples": 0, "profiles": 0, "hot_path_share": {}})
    monkeypatch.setattr("telemetry.profiles.method_hook_edges", lambda *_args: [])
    monkeypatch.setattr(
        "telemetry.runtime.runtime_sampling_policy",
        lambda _registry=None: {
            "p95_window_hours": 24,
            "refresh_minutes": 5,
            "aggregation_retention_hours": 168,
        },
    )

    summary = ClickHouseRuntime().summary("pizza", "rev_test", {"App\\A::run"})

    assert len(queries) == 1
    assert "FROM `telemetry`.`runtime_span_rollups` FINAL" in queries[0]
    assert "NOT IN (SELECT hour FROM" in queries[0]
    assert "StatusCode = 'Error'" in queries[0]
    assert (summary["total_spans"], summary["symbol_spans"], summary["error_spans"]) == (12, 5, 2)
    assert summary["matched_spans"] == 5
    assert summary["symbols"][0]["error_rate"] == 20.0
    assert summary["routes"][0]["route_id"] == "route:GET /a"
    assert summary["callers"] == [{"symbol_id": "App\\A::run", "caller_symbol_id": "App\\B::call", "calls": 3}]
    assert summary["symbol_routes"][0]["route"] == "/a" and summary["symbol_routes"][0]["method"] == "GET"


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
            assert "ProjectId = {project_id:String}" in sql and "RevisionId = {revision_id:String}" in sql and "Eligible" in sql
        elif "`telemetry`.`otel_profiles`" in sql:
            assert "project_id = {project_id:String}" in sql
            assert "revision_id = {revision_id:String}" in sql
    assert captured[1][3] == {"param_trace_id": "a" * 32}


def test_runtime_request_list_filters_errors_and_slow_requests_before_the_limit(monkeypatch):
    from telemetry.runtime import ClickHouseRuntime

    captured = []

    def rows(_self, sql, project_id, revision_id, extra_parameters=None):
        captured.append(sql)
        if sql.startswith("SELECT quantile(0.95)"):
            return [{"p95_ms": 42.5}]
        return [{"trace_id": "b" * 32, "span_id": "1" * 16, "timestamp": "2026-11-06 10:00:00",
                 "method": "GET", "route": "/api/pizzas", "status_code": 500, "duration_ms": 80.0, "span_status": "Error",
                 "slow_threshold_ms": 61.25}]

    monkeypatch.setattr(ClickHouseRuntime, "configured", property(lambda _self: True))
    monkeypatch.setattr(ClickHouseRuntime, "rows", rows)
    monkeypatch.setattr(
        "telemetry.runtime.runtime_sampling_policy",
        lambda _registry=None: {"p95_window_hours": 24, "refresh_minutes": 5, "aggregation_retention_hours": 168},
    )
    clickhouse = ClickHouseRuntime()

    errors = clickhouse.requests("pizza", "rev_test", "errors")
    assert "HAVING span_status = 'Error' OR status_code >= 500 ORDER BY timestamp DESC LIMIT 50" in captured[-1]
    assert errors["outcome"] == "errors" and errors["slow_threshold_ms"] is None

    slow = clickhouse.requests("pizza", "rev_test", "slow")
    threshold_sql, list_sql = captured[-2], captured[-1]
    assert "SpanKind = 'Server'" in threshold_sql and "RevisionId = {revision_id:String}" in threshold_sql
    # Each request is compared with its route's p95; routes with few requests fall back to the revision p95.
    assert "LEFT JOIN (SELECT SpanAttributes['http.request.method'] AS method, HttpRoute AS route" in list_sql
    assert "if(thresholds.route_requests >= 20, thresholds.route_p95_ms, 42.5) AS slow_threshold_ms" in list_sql
    assert "WHERE requests.duration_ms >= slow_threshold_ms ORDER BY requests.timestamp DESC LIMIT 50" in list_sql
    assert slow["slow_threshold_ms"] == 42.5
    assert slow["requests"][0]["trace_id"] == "b" * 32
    assert slow["requests"][0]["slow_threshold_ms"] == 61.25

    everything = clickhouse.requests("pizza", "rev_test")
    assert "HAVING" not in captured[-1] and everything["outcome"] == "all"

    exclusion = "NOT (route = '(unmatched)' AND ifNull(status_code, 0) = 404 AND span_status != 'Error')"
    clickhouse.requests("pizza", "rev_test", hide_unmatched_404=True)
    assert "HAVING " + exclusion + " ORDER BY timestamp DESC LIMIT 50" in captured[-1]

    clickhouse.requests("pizza", "rev_test", "errors", hide_unmatched_404=True)
    assert "HAVING (span_status = 'Error' OR status_code >= 500) AND " + exclusion in captured[-1]

    clickhouse.requests("pizza", "rev_test", "slow", hide_unmatched_404=True)
    assert "HAVING " + exclusion + ") AS requests" in captured[-1]
    assert "WHERE requests.duration_ms >= slow_threshold_ms" in captured[-1]

    with pytest.raises(ValueError):
        clickhouse.requests("pizza", "rev_test", "1 OR 1=1")


def test_runtime_request_endpoint_validates_and_forwards_unmatched_filter(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from telemetry import http

    class Registry:
        def get_by_revision_id(self, project_id, revision_id):
            return {"revision_id": revision_id}

    captured = []
    monkeypatch.setattr(http, "authorize_internal", lambda _request: None)
    monkeypatch.setattr(http, "get_registry", Registry)
    monkeypatch.setattr(
        ClickHouseRuntime, "requests",
        lambda self, project_id, revision_id, outcome, **filters:
            captured.append((outcome, filters)) or {"requests": []},
    )
    app = FastAPI()
    app.include_router(http.runtime_router)
    client = TestClient(app)
    path = "/telemetry/runtime/projects/pizza/revisions/rev_" + "c" * 32 + "/requests"
    assert client.get(path).status_code == 200
    assert captured[-1] == ("all", {"hide_unmatched_404": False})
    assert client.get(path + "?outcome=slow&hide_unmatched_404=true").status_code == 200
    assert captured[-1] == ("slow", {"hide_unmatched_404": True})
    assert client.get(path + "?hide_unmatched_404=invalid").status_code == 422
    assert len(captured) == 2


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
    assert sql.startswith("DELETE FROM `telemetry`.`otel_spans_full` WHERE")
    assert profile_sql.startswith("DELETE FROM ")
    assert "ProjectId = {project_id:String} AND RevisionId = {revision_id:String}" in sql
    assert "TraceId IN ({trace_id_0:String}, {trace_id_1:String})" in sql
    assert all(trace_id.lower() not in sql for trace_id in trace_ids)
    assert parameters == {
        "lightweight_deletes_sync": "2",
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


def test_storage_mode_failure_shows_mode_start_command(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from telemetry import http, runtime_storage

    monkeypatch.setattr(http, "authorize_internal", lambda _request: None)
    monkeypatch.setattr(http, "get_registry", lambda: object())

    def fail_setting(_mode, _registry):
        raise RuntimeError("backend is stopped")

    monkeypatch.setattr(runtime_storage, "set_storage_mode", fail_setting)
    app = FastAPI()
    app.include_router(http.runtime_router)

    response = TestClient(app).put("/telemetry/runtime/settings/storage/mode", json={"mode": "local"})

    assert response.status_code == 503
    assert "just runtime-storage local" in response.json()["detail"]


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
        "reason": (
            "No method-call data was captured for this request. If this is true for every request, the app's PHP "
            "container probably lacks the ext-opentelemetry extension, which method activity needs."
        ),
        "edges": [],
    }
