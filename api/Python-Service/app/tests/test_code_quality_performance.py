import json
import sqlite3

import pytest

import code_quality.performance as performance
from code_quality.performance import (
    PerformanceBusy,
    PerformanceError,
    PerformanceStore,
    build_script,
    compare_runs,
    export_script,
    fill_route,
    interpret_output,
    start_run,
    validate_origin,
    validate_route,
    validate_target,
)

PROJECT = "pizza"
REVISION = {"revision_id": "rev_" + "a" * 32, "commit_sha": "c" * 40, "source_ref": "c" * 40}


@pytest.fixture(autouse=True)
def developer_mode(monkeypatch):
    monkeypatch.setenv("APP_DEPLOYMENT_MODE", "developer")
    monkeypatch.setattr(performance, "_validate_resolved_target", lambda _origin: None)


def _target(**overrides):
    return {"origin": "http://localhost:8080", "virtual_users": 2, "duration_seconds": 10, "auth_mode": "public", **overrides}


def _summary(routes, **extra):
    return {"runner_version": "0.54.0", "login_failures": 0, "routes": [
        {"path": path, "count": 20, "throughput_rps": 2.0, "errors": 0, "error_rate": 0, "network_errors": 0, "auth_rejected": 0,
         "statuses": {"2xx": 20, "3xx": 0, "4xx": 0, "5xx": 0},
         "latency_ms": {"med": 5.0, "p(90)": 8.0, "p(95)": 9.0, "p(99)": 12.0}, **extra} for path in routes]}


def _runner(output, returncode=0, capture=None):
    def run(script, port, timeout):
        if capture is not None:
            capture.update(script=script, port=port, timeout=timeout)
        return type("Completed", (), {"returncode": returncode, "stdout": json.dumps(output).encode(), "stderr": b""})()
    return run


# ----- origins and routes --------------------------------------------------------------------------

def test_local_docker_targets_are_developer_only_and_never_reach_our_own_services(monkeypatch):
    assert validate_origin("http://localhost:8080/") == "http://host.docker.internal:8080"
    for port in (47831, 6333, 7687, 9000):
        with pytest.raises(PerformanceError, match="Developer Assistant service"):
            validate_origin(f"http://host.docker.internal:{port}")
    monkeypatch.setenv("PERFORMANCE_BLOCKED_HOST_PORTS", "8081")
    with pytest.raises(PerformanceError):
        validate_origin("http://localhost:8081")
    monkeypatch.setenv("APP_DEPLOYMENT_MODE", "production")
    with pytest.raises(PerformanceError, match="Developer Mode"):
        validate_origin("http://localhost:8080")
    with pytest.raises(PerformanceError, match="Private and link-local"):
        validate_origin("http://169.254.169.254")
    with pytest.raises(PerformanceError, match="origin"):
        validate_origin("http://user:password@example.com")


def test_credentials_require_https_except_for_local_docker():
    with pytest.raises(PerformanceError, match="HTTPS"):
        validate_target(_target(origin="http://shop.example", auth_mode="bearer"))
    assert validate_target(_target(origin="https://shop.example", auth_mode="bearer"))["auth_mode"] == "bearer"
    assert validate_target(_target(auth_mode="bearer"))["origin"] == "http://host.docker.internal:8080"


@pytest.mark.parametrize("route", ["//outside.example/path", "/../private", "/a\\b", "/a\r\nHeader: value", "/pizzas/{pizza}"])
def test_route_validation_rejects_unsafe_or_unfilled_paths(route):
    with pytest.raises(PerformanceError):
        validate_route(route)


def test_route_templates_are_filled_with_single_path_segments():
    assert fill_route("/pizzas/{pizza}", {"pizza": "12"}) == "/pizzas/12"
    assert fill_route("/posts/{post}/comments/{comment?}", {"post": 3}) == "/posts/3/comments"
    with pytest.raises(PerformanceError, match="Enter a value"):
        fill_route("/pizzas/{pizza}", {})
    with pytest.raises(PerformanceError, match="one URL path segment"):
        fill_route("/pizzas/{pizza}", {"pizza": "1/../../admin"})


def test_workload_models_and_limits():
    ramp = validate_target(_target(executor="ramping_vus", stages=[{"duration_seconds": 10, "target": 5},
                                                                   {"duration_seconds": 20, "target": 20}]))
    assert ramp["duration_seconds"] == 30 and ramp["virtual_users"] == 20
    rate = validate_target(_target(executor="constant_arrival_rate", rate_per_second=40, max_virtual_users=10))
    assert rate["rate_per_second"] == 40
    with pytest.raises(PerformanceError, match="Virtual users"):
        validate_target(_target(virtual_users=51))
    with pytest.raises(PerformanceError, match="stages"):
        validate_target(_target(executor="ramping_vus", stages=[]))
    with pytest.raises(PerformanceError, match="× routes"):
        performance.prepare_run({**validate_target(_target(virtual_users=50)), "target_id": "t"}, ["/a", "/b", "/c"])


# ----- generated k6 script -------------------------------------------------------------------------

def test_script_runs_one_scenario_per_route_and_counts_redirects_as_unsuccessful():
    workload = performance.validate_workload({"virtual_users": 3, "duration_seconds": 20, "warmup_seconds": 5, "think_time_ms": 0})
    script = build_script("https://shop.example", ["/a", "/b"], workload, {"mode": "public"},
                          thresholds={"p95_ms": 300, "error_rate": 0.01}, pinned_address="1.1.1.1", run_id="run-1")
    options = json.loads(script.split("export const options = ")[1].split(";\n")[0])
    assert {"route_0", "route_1", "warmup_0", "warmup_1"} <= set(options["scenarios"])
    assert options["scenarios"]["route_1"]["startTime"] == "5s" and options["scenarios"]["route_1"]["vus"] == 3
    assert options["thresholds"]["route_0_duration"] == ["p(95)<300"]
    assert "p(99)" in options["summaryTrendStats"]
    assert options["hosts"] == {"shop.example": "1.1.1.1"}
    assert "r.failure.add(!(s >= 200 && s < 300))" in script
    assert '"X-DevAssistant-Benchmark"' in script and '"run_id":"run-1"' in script


def test_laravel_login_uses_the_xsrf_header_and_detects_redirects_back_to_the_form():
    login = performance._validate_login({"login_path": "/login", "username_field": "email", "password_field": "password",
                                         "csrf_mode": "header", "csrf_path": "/login", "csrf_cookie_name": "XSRF-TOKEN"})
    assert login["csrf_header_name"] == "X-XSRF-TOKEN"
    script = build_script("http://host.docker.internal:8080", ["/dashboard"], performance.validate_workload({}),
                          {"mode": "http_login", **login, "username": "u", "password": "p"})
    assert "headers[a.csrf_header_name] = token" in script
    assert "Referer: cfg.origin + a.login_path" in script
    assert "pathOf(location) !== pathOf(a.login_path)" in script
    # Settings saved before CSRF modes existed keep the form-field behaviour.
    legacy = performance._validate_login({"login_path": "/login", "username_field": "email", "password_field": "password",
                                          "csrf_cookie_name": "XSRF-TOKEN", "csrf_field_name": "_token"})
    assert legacy["csrf_mode"] == "field"


def test_exported_script_carries_no_secrets_or_pinning():
    report = {"target_origin": "https://shop.example", "routes": ["/a"], "auth_mode": "bearer",
              "workload": {"executor": "constant_vus", "virtual_users": 1, "duration_seconds": 10},
              "target_config": {"origin": "https://shop.example", "auth_mode": "bearer", "login": {}}}
    script = export_script(report)
    assert "__ENV.BENCHMARK_TOKEN" in script and '"hosts":{}' in script


# ----- store ---------------------------------------------------------------------------------------

def test_targets_are_scoped_to_user_and_project_and_never_hold_credentials(tmp_path):
    store = PerformanceStore(tmp_path / "quality.sqlite")
    target = store.save_target("alice", PROJECT, _target(name="Local", auth_mode="bearer"))
    assert store.get_target("bob", PROJECT, target["target_id"]) is None
    assert store.get_target("alice", "other", target["target_id"]) is None
    assert "token" not in json.dumps(store.targets("alice", PROJECT))
    second = store.save_target("alice", PROJECT, _target(name="Local, logged in"))  # same origin, second profile
    assert second["target_id"] != target["target_id"]
    with pytest.raises(PerformanceError, match="already exists"):
        store.save_target("alice", PROJECT, _target(name="Local"))
    updated = store.save_target("alice", PROJECT, {**_target(name="Local", virtual_users=4), "target_id": target["target_id"]})
    assert updated["virtual_users"] == 4


def test_first_version_targets_migrate_as_unassigned(tmp_path):
    path = tmp_path / "quality.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE performance_targets (target_id TEXT PRIMARY KEY, user_key TEXT NOT NULL, origin TEXT NOT NULL,
            config_json TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(user_key, origin))""")
        db.execute("INSERT INTO performance_targets VALUES (?,?,?,?,?)",
                   ("11111111-1111-4111-8111-111111111111", PerformanceStore._user_key("alice"), "http://host.docker.internal:80",
                    json.dumps({"virtual_users": 1, "duration_seconds": 30, "auth_mode": "public", "login": {}}), "2026-11-01"))
    store = PerformanceStore(path)
    [legacy] = store.targets("alice", PROJECT)
    assert legacy["unassigned"] is True and legacy["executor"] == "constant_vus"
    assigned = store.save_target("alice", PROJECT, {**_target(name="Local"), "target_id": legacy["target_id"]})
    assert assigned["unassigned"] is False


def test_only_one_run_per_user_and_two_in_total(tmp_path):
    store = PerformanceStore(tmp_path / "quality.sqlite")
    first, port = store.claim_run("alice", PROJECT, "t1", 60, {})
    with pytest.raises(PerformanceBusy) as busy:
        store.claim_run("alice", PROJECT, "t1", 60, {})
    assert busy.value.active_run_id == first
    _, second_port = store.claim_run("bob", PROJECT, "t2", 60, {})
    assert {port, second_port} == set(performance.K6_API_PORTS)
    with pytest.raises(PerformanceBusy, match="maximum"):
        store.claim_run("carol", PROJECT, "t3", 60, {})
    store.finish_job(first, "completed")
    store.claim_run("carol", PROJECT, "t3", 60, {})
    assert store.recover_interrupted() == 2


# ----- running -------------------------------------------------------------------------------------

def test_background_run_saves_revision_routes_and_sanitized_settings(tmp_path):
    store = PerformanceStore(tmp_path / "quality.sqlite")
    target = store.save_target("alice", PROJECT, _target(name="Local", auth_mode="bearer"))
    captured = {}
    job = start_run(store, "alice", PROJECT, target, [{"template": "/pizzas/{pizza}", "params": {"pizza": "1"}}], REVISION,
                    bearer_token="secret-value", runner=_runner(_summary(["/pizzas/1"]), capture=captured), background=False)
    assert '"token":"secret-value"' in captured["script"]
    [run] = store.history("alice", PROJECT)
    assert run["run_id"] == job["run_id"] and run["complete"] is True
    assert run["revision_id"] == REVISION["revision_id"] and run["commit_sha"] == REVISION["commit_sha"]
    assert run["routes"] == ["/pizzas/1"] and run["route_templates"] == ["/pizzas/{pizza}"]
    assert "secret-value" not in json.dumps(run)
    assert store.job("alice", PROJECT, job["run_id"])["state"] == "completed"


def test_rejected_and_redirected_requests_make_a_partial_run_instead_of_an_error(tmp_path):
    store = PerformanceStore(tmp_path / "quality.sqlite")
    target = store.save_target("alice", PROJECT, _target(name="Local"))
    output = _summary(["/admin"], auth_rejected=5, statuses={"2xx": 10, "3xx": 5, "4xx": 5, "5xx": 0})
    start_run(store, "alice", PROJECT, target, ["/admin"], REVISION, runner=_runner(output), background=False)
    [run] = store.history("alice", PROJECT)
    assert run["complete"] is False
    assert any("401/403" in reason for reason in run["partial_reasons"])
    assert any("redirects" in reason for reason in run["partial_reasons"])


def test_failed_login_or_unreachable_target_fails_the_job_without_a_report(tmp_path):
    store = PerformanceStore(tmp_path / "quality.sqlite")
    target = store.save_target("alice", PROJECT, _target(name="Local", auth_mode="http_login", login={
        "login_path": "/login", "username_field": "email", "password_field": "password", "csrf_mode": "none"}))
    job = start_run(store, "alice", PROJECT, target, ["/dashboard"], REVISION,
                    login_credentials={"username": "u", "password": "p"},
                    runner=_runner({"login_failures": 3, "routes": [{"count": 0}]}), background=False)
    failed = store.job("alice", PROJECT, job["run_id"])
    assert failed["state"] == "failed" and "HTTP login failed" in failed["error"]
    assert store.history("alice", PROJECT) == []
    with pytest.raises(PerformanceError, match="could not be reached"):
        interpret_output({"routes": [{"count": 4, "network_errors": 4}]}, [{"path": "/a", "template": "/a"}])


def test_threshold_failures_still_produce_a_report(tmp_path):
    store = PerformanceStore(tmp_path / "quality.sqlite")
    target = store.save_target("alice", PROJECT, _target(name="Local", thresholds={"p95_ms": 1}))
    start_run(store, "alice", PROJECT, target, ["/a"], REVISION, runner=_runner(_summary(["/a"]), returncode=99), background=False)
    [run] = store.history("alice", PROJECT)
    assert run["thresholds_failed"] is True and run["thresholds"] == {"p95_ms": 1}


def test_runner_failure_message_does_not_leak_output(tmp_path):
    store = PerformanceStore(tmp_path / "quality.sqlite")
    target = store.save_target("alice", PROJECT, _target(name="Local"))
    def failing(script, port, timeout):
        return type("Completed", (), {"returncode": 1, "stdout": b"secret-value", "stderr": b"password=secret-value"})()
    job = start_run(store, "alice", PROJECT, target, ["/a"], REVISION, runner=failing, background=False)
    assert "secret-value" not in json.dumps(store.job("alice", PROJECT, job["run_id"]))


def test_stale_scripts_are_removed(tmp_path):
    (tmp_path / "code-quality-k6-abc.js").write_text("token")
    (tmp_path / "other.js").write_text("keep")
    assert performance.cleanup_stale_scripts(tmp_path) == 1
    assert (tmp_path / "other.js").exists()


# ----- comparison ----------------------------------------------------------------------------------

def test_comparison_reports_deltas_regressions_and_revision_changes():
    base = {"target_origin": "http://host.docker.internal:8080", "routes": ["/a"], "auth_mode": "public",
            "workload": {"executor": "constant_vus", "virtual_users": 1, "duration_seconds": 10}, "runner_version": "0.54.0",
            "revision_id": "rev_1", "route_results": [{"path": "/a", "throughput_rps": 10, "error_rate": 0,
                                                       "latency_ms": {"med": 10, "p(95)": 100, "p(99)": 150}}]}
    slower = {**base, "revision_id": "rev_2", "route_results": [{"path": "/a", "throughput_rps": 8, "error_rate": 0,
                                                                  "latency_ms": {"med": 12, "p(95)": 130, "p(99)": 160}}]}
    result = compare_runs(base, slower)
    assert result["equivalent"] is True and result["revisions"]["changed"] is True
    row = result["routes"][0]
    assert row["p95"]["change_pct"] == 30.0 and row["regression"] is True and result["regressions"] == 1
    different = compare_runs(base, {**slower, "workload": {**base["workload"], "virtual_users": 4}})
    assert different["differences"] == ["workload"] and different["routes"][0]["regression"] is False


def test_client_and_server_errors_make_a_run_partial_and_throughput_uses_the_measured_window():
    output = {"routes": [{"count": 400, "throughput_rps": 17.4, "statuses": {"2xx": 0, "3xx": 0, "4xx": 401, "5xx": 0}},
                         {"count": 100, "throughput_rps": 4.0, "statuses": {"2xx": 90, "3xx": 0, "4xx": 0, "5xx": 10}}]}
    results, complete, reasons = interpret_output(output, [{"path": "/pizzas/1", "template": "/pizzas/{pizza}"},
                                                          {"path": "/a", "template": "/a"}], measured_seconds=20)
    assert complete is False
    assert any("client errors" in reason for reason in reasons) and any("server errors" in reason for reason in reasons)
    assert results[0]["throughput_rps"] == 20.0
