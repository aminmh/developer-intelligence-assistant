"""The Database tab summarizes a request's queries and flags the same query run many times."""
import pytest

from telemetry import runtime
from telemetry.runtime import ClickHouseRuntime, _database_summary

SETTING = "App\\Models\\Setting::allCached"
CONTROLLER = "App\\Http\\Controllers\\HomeController::pizza"


def _call(span: int, operation: str, table: str, duration: float, symbol: str | None = None, caller: str | None = None) -> dict:
    return {"span_id": f"{span:016x}", "operation": operation, "table_ids": [table], "duration_ms": duration,
            "symbol_id": symbol, "caller_symbol_id": caller}


def test_summary_totals_share_and_slowest():
    calls = [
        _call(1, "SELECT", "table:mysql.pizzas", 0.65, CONTROLLER),
        _call(2, "UPDATE", "table:mysql.sessions", 4.56),
        _call(3, "SELECT", "table:mysql.pizza_sizes", 0.25, CONTROLLER),
    ]

    summary = _database_summary(calls, 20.0)

    assert summary["query_count"] == 3
    assert summary["total_ms"] == pytest.approx(5.46)
    assert summary["request_share"] == pytest.approx(0.273)
    assert summary["slowest"]["span_id"] == f"{2:016x}"
    assert summary["slowest"]["operation"] == "UPDATE"
    assert summary["repeated_groups"] == 0


def test_same_query_from_one_method_is_grouped_and_flagged():
    calls = [_call(index, "SELECT", "table:mysql.cache", 0.2, SETTING, "App\\Models\\Setting::get") for index in range(9)]
    calls += [_call(20, "SELECT", "table:mysql.cache", 0.5), _call(21, "SELECT", "table:mysql.cache", 0.6)]

    summary = _database_summary(calls, 30.0)

    first, second = summary["groups"]
    assert (first["symbol_id"], first["count"], first["repeated"]) == (SETTING, 9, True)
    assert first["total_ms"] == pytest.approx(1.8)
    assert first["caller_symbol_ids"] == ["App\\Models\\Setting::get"]
    assert len(first["span_ids"]) == 9
    # Unattributed framework queries form their own group and stay below the threshold here.
    assert (second["symbol_id"], second["count"], second["repeated"]) == (None, 2, False)
    assert summary["repeated_groups"] == 1
    assert summary["repeated_min"] == runtime.REPEATED_QUERY_MIN


def test_different_tables_or_operations_are_not_grouped():
    calls = [
        _call(1, "SELECT", "table:mysql.pizzas", 1.0, CONTROLLER),
        _call(2, "SELECT", "table:mysql.pizza_sizes", 1.0, CONTROLLER),
        _call(3, "UPDATE", "table:mysql.pizzas", 1.0, CONTROLLER),
    ]

    assert len(_database_summary(calls, 10.0)["groups"]) == 3


def test_no_calls_or_unknown_duration_is_safe():
    empty = _database_summary([], 10.0)
    assert (empty["query_count"], empty["total_ms"], empty["slowest"], empty["groups"]) == (0, 0, None, [])
    assert _database_summary([_call(1, "SELECT", "table:mysql.pizzas", None)], None)["request_share"] is None


def test_request_detail_returns_the_database_summary(monkeypatch):
    monkeypatch.setattr(ClickHouseRuntime, "configured", property(lambda _self: True))
    monkeypatch.setattr(ClickHouseRuntime, "_trace_ai_summary", lambda *_args: None)
    server = {"trace_id": "a" * 32, "span_id": "1" * 16, "parent_span_id": "", "span_kind": "Server",
              "method": "GET", "route": "/pizzas/{pizza}", "status_code": 200, "duration_ms": 10.0,
              "timestamp": "2026-11-07 10:00:00", "span_status": "Unset"}
    queries = [{"trace_id": "a" * 32, "span_id": f"{index:016x}", "parent_span_id": "1" * 16, "span_kind": "Client",
                "db_operation": "SELECT", "table_ids": '["table:mysql.cache"]', "duration_ms": 0.5,
                "symbol_id": SETTING} for index in range(2, 6)]
    monkeypatch.setattr(ClickHouseRuntime, "rows", lambda *_args, **_kwargs: [server, *queries])

    detail = ClickHouseRuntime().request_detail("pizza", "rev_a", "a" * 32)

    summary = detail["database_summary"]
    assert summary["query_count"] == 4
    assert summary["request_share"] == pytest.approx(0.2)
    assert summary["groups"][0]["repeated"] is True


def test_framework_queries_are_labelled_and_split_from_app_queries():
    calls = [
        _call(1, "INSERT", "table:mysql.telescope_entries", 3.0),
        _call(2, "UPDATE", "table:mysql.sessions", 1.0),
        # App code reading a framework table is still app work.
        _call(3, "SELECT", "table:mysql.cache", 0.5, SETTING),
        _call(4, "SELECT", "table:mysql.pizzas", 0.5, CONTROLLER),
        # Unattributed but on an app table: not labelled framework.
        _call(5, "SELECT", "table:mysql.pizzas", 0.5),
    ]

    summary = _database_summary(calls, 20.0)

    labels = {(group["table_ids"][0], group["symbol_id"]): group["framework"] for group in summary["groups"]}
    assert labels[("table:mysql.telescope_entries", None)] is True
    assert labels[("table:mysql.sessions", None)] is True
    assert labels[("table:mysql.cache", SETTING)] is False
    assert labels[("table:mysql.pizzas", None)] is False
    assert (summary["framework_query_count"], summary["framework_total_ms"]) == (2, pytest.approx(4.0))
    assert (summary["app_query_count"], summary["app_total_ms"]) == (3, pytest.approx(1.5))
    assert summary["slowest"]["framework"] is True
