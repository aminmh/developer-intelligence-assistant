"""Runtime responses say when data is sampled or incomplete (sampling ratio, truncation, dropped calls)."""
import pytest

from telemetry import runtime
from telemetry.runtime import ClickHouseRuntime, _ratio, _sampling


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(ClickHouseRuntime, "configured", property(lambda _self: True))
    monkeypatch.setattr(
        "telemetry.runtime.runtime_sampling_policy",
        lambda _registry=None: {"p95_window_hours": 24, "refresh_minutes": 5, "aggregation_retention_hours": 72},
    )
    monkeypatch.setattr(ClickHouseRuntime, "_trace_ai_summary", lambda *_args: None)


def _server_span(**extra):
    return {"trace_id": "a" * 32, "span_id": "1" * 16, "parent_span_id": "", "span_kind": "Server",
            "method": "GET", "route": "/", "status_code": 200, "duration_ms": 5.0,
            "timestamp": "2026-11-06 10:00:00", "span_status": "Unset", **extra}


def test_ratio_accepts_only_a_fraction_in_zero_to_one():
    assert _ratio("0.1") == 0.1
    assert _ratio("1") == 1.0
    assert _ratio("") is None
    assert _ratio("0") is None
    assert _ratio("1.5") is None


def test_sampling_estimates_real_volume_from_known_ratios():
    rows = [{"k1": "0.1", "calls": 30}, {"k1": "0.1", "calls": 10}, {"k1": "1", "calls": 5}]

    sampling = _sampling(rows)

    assert sampling["sampling_ratio"] is None  # two different ratios
    assert sampling["sampling_ratios"] == [{"ratio": 0.1, "spans": 40}, {"ratio": 1.0, "spans": 5}]
    assert sampling["estimated_total_spans"] == 405


def test_sampling_estimate_is_withheld_when_some_spans_have_no_ratio():
    sampling = _sampling([{"k1": "0.1", "calls": 10}, {"k1": "", "calls": 3}])

    assert sampling["estimated_total_spans"] is None


def test_request_list_reports_one_sampling_ratio(monkeypatch):
    monkeypatch.setattr(ClickHouseRuntime, "rows", lambda *_args, **_kwargs: [
        {"trace_id": "a" * 32, "span_id": "1" * 16, "sampling_ratio": "0.1"},
        {"trace_id": "b" * 32, "span_id": "2" * 16, "sampling_ratio": "0.1"},
    ])

    listed = ClickHouseRuntime().requests("pizza", "rev_a")

    assert listed["sampling_ratio"] == 0.1
    assert listed["sampled"] is True
    assert listed["requests"][0]["sampling_ratio"] == 0.1


def test_request_list_with_full_recording_is_not_marked_sampled(monkeypatch):
    monkeypatch.setattr(ClickHouseRuntime, "rows", lambda *_args, **_kwargs: [
        {"trace_id": "a" * 32, "span_id": "1" * 16, "sampling_ratio": "1"},
    ])

    assert ClickHouseRuntime().requests("pizza", "rev_a")["sampled"] is False


def test_request_detail_flags_a_trace_beyond_the_span_limit(monkeypatch):
    queries = []
    spans = [_server_span()] + [
        {"trace_id": "a" * 32, "span_id": f"{index:016x}", "parent_span_id": "1" * 16, "span_kind": "Internal"}
        for index in range(2, runtime._DETAIL_SPAN_LIMIT + 2)
    ]

    def rows(_self, sql, *_args, **_kwargs):
        queries.append(sql)
        return spans

    monkeypatch.setattr(ClickHouseRuntime, "rows", rows)

    detail = ClickHouseRuntime().request_detail("pizza", "rev_a", "a" * 32)

    assert f"LIMIT {runtime._DETAIL_SPAN_LIMIT + 1}" in queries[0]
    assert detail["truncated"] is True
    assert detail["span_limit"] == runtime._DETAIL_SPAN_LIMIT


def test_request_detail_reports_dropped_hook_calls_and_ratio(monkeypatch):
    monkeypatch.setattr(ClickHouseRuntime, "rows", lambda *_args, **_kwargs: [
        _server_span(dropped_hook_calls=7, sampling_ratio="0.1", error_type="App\\Exceptions\\Boom"),
    ])

    detail = ClickHouseRuntime().request_detail("pizza", "rev_a", "a" * 32)

    assert detail["truncated"] is False
    assert detail["dropped_hook_calls"] == 7
    assert detail["sampling_ratio"] == 0.1
    assert detail["request"]["error_type"] == "App\\Exceptions\\Boom"


def test_policy_writes_route_thresholds_next_to_the_project_p95(monkeypatch, tmp_path):
    from telemetry import policy

    calls = []

    def unscoped_rows(_self, sql):
        calls.append(sql)
        if "GROUP BY project_id, route" in sql:
            return [{"project_id": "pizza", "route": "GET /health", "requests": 40, "p95_ms": 3.5}]
        return [{"project_id": "pizza", "p95_ms": 120.0}]

    target = tmp_path / "policy.json"
    monkeypatch.setenv("TELEMETRY_SAMPLING_POLICY_PATH", str(target))
    monkeypatch.setattr(ClickHouseRuntime, "unscoped_rows", unscoped_rows)
    monkeypatch.setattr(policy, "runtime_sampling_policy", lambda _registry=None: {"p95_window_hours": 24, "refresh_minutes": 5})

    document = policy.refresh_sampling_policy(registry=object())

    assert document["thresholds_ms"] == {"pizza": 120.0}
    assert document["route_thresholds_ms"] == {"pizza": {"GET /health": 3.5}}
    assert f"HAVING requests >= {policy.ROUTE_THRESHOLD_MIN_REQUESTS}" in calls[1]
