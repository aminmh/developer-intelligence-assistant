"""Browser screen views read from runtime spans."""

from __future__ import annotations

import pytest

from telemetry.browser import screen_view, screen_views

A, B = "a" * 32, "b" * 32


class Runtime:
    configured, database, table, window_hours = True, "otel", "otel_spans_full", 24

    def __init__(self, answers):
        self.answers, self.queries = list(answers), []

    def rows(self, sql, project_id, revision_id, extra=None):
        self.queries.append((sql, project_id))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def page(trace, route, lcp, **extra):
    return {"trace_id": trace, "span_id": "1" * 16, "timestamp": "2026-11-14 10:00:00", "revision_id": "rev_x", "route": route,
            "duration_ms": 4000.0, "lcp_ms": lcp, "inp_ms": None, "cls": 0.01, "frontend_project_id": "", **extra}


def test_views_join_children_and_requests_and_summarise_screens():
    runtime = Runtime([
        [page(A, "Orders", 900), page(B, "Orders", 2100), page("c" * 32, "Cart", None), {"trace_id": "bad", "span_id": "x"}],
        [{"trace_id": A, "errors": 2, "slow_interactions": 1}],
        [{"view": A, "requests": 3, "request_ms": 120.44, "failed": 1}],
    ])
    result = screen_views("shop", runtime=runtime)
    assert result["available"] and len(result["views"]) == 3
    first = result["views"][0]
    assert (first["errors"], first["slow_interactions"], first["requests"], first["failed_requests"], first["request_ms"]) == (2, 1, 3, 1, 120.4)
    assert result["views"][1]["requests"] == 0 and result["views"][1]["errors"] == 0
    orders, cart = result["screens"]
    assert (orders["route"], orders["views"], orders["lcp_p75_ms"], orders["errors"], orders["requests_per_view"]) == ("Orders", 2, 2100.0, 2, 1.5)
    assert cart["lcp_p75_ms"] is None
    # Every query is scoped to the project and to eligible spans; IDs in SQL are the validated ones only.
    assert all(project == "shop" and "ProjectId = {project_id:String} AND Eligible" in sql for sql, project in runtime.queries)
    assert f"'{A}', '{B}'" in runtime.queries[1][0] and "bad" not in runtime.queries[1][0]
    assert "SpanKind = 'Server'" in runtime.queries[2][0] and "indexer.browser.trace_id" in runtime.queries[2][0]


def test_views_report_unavailable_states():
    assert screen_views("shop", runtime=Runtime([[]]))["views"] == []
    broken = screen_views("shop", runtime=Runtime([OSError("down")]))
    assert broken["available"] is False and broken["views"] == []
    assert screen_views("shop", runtime=type("Off", (), {"configured": False})())["available"] is False


def test_one_view_lists_its_errors_interactions_and_requests():
    runtime = Runtime([
        [{"name": "browser.page", "span_id": "1" * 16, "route": "Orders", "duration_ms": 4000.0, "lcp_ms": 900, "inp_ms": 240, "cls": 0.02,
          "timestamp": "t0", "revision_id": "rev_x"},
         {"name": "browser.interaction", "timestamp": "t1", "duration_ms": 240.0},
         {"name": "browser.error", "timestamp": "t2", "error_type": "TypeError"}],
        [{"trace_id": B, "method": "GET", "route": "/api/orders", "status_code": 200, "duration_ms": 35.0, "span_status": "Unset", "revision_id": "rev_x"},
         {"trace_id": "C" * 32, "method": "POST", "route": "/api/orders", "status_code": 500, "duration_ms": 80.0, "span_status": "Error"},
         {"trace_id": "not-a-trace", "method": "GET"}],
    ])
    result = screen_view("shop", A.upper(), runtime=runtime)
    assert result["view"]["route"] == "Orders" and result["view"]["lcp_ms"] == 900
    assert result["errors"] == [{"timestamp": "t2", "error_type": "TypeError"}]
    assert result["slow_interactions"] == [{"timestamp": "t1", "duration_ms": 240.0}]
    assert [(r["trace_id"], r["failed"]) for r in result["requests"]] == [(B, False), ("c" * 32, True)]
    assert f"TraceId = '{A}'" in runtime.queries[0][0]

    assert screen_view("shop", A, runtime=Runtime([[], []]))["available"] is False
    with pytest.raises(ValueError):
        screen_view("shop", "'; DROP TABLE x; --", runtime=Runtime([]))
