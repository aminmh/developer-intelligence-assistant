import json

import pytest

import code_quality.performance as performance
from code_quality.performance import (
    PerformanceError,
    PerformanceStore,
    compare_runs,
    run_benchmark,
    validate_origin,
    validate_route,
    validate_target,
)


def test_target_validation_applies_workload_limits_and_normalizes_local_docker_origin():
    target = validate_target({"origin": "http://localhost:8080/", "virtual_users": 3,
                              "duration_seconds": 20, "auth_mode": "public"})
    assert target["origin"] == "http://host.docker.internal:8080"
    assert target["virtual_users"] == 3
    with pytest.raises(PerformanceError, match="Virtual users"):
        validate_target({"origin": "http://localhost", "virtual_users": 51, "duration_seconds": 10})
    with pytest.raises(PerformanceError, match="origin"):
        validate_origin("http://user:password@example.com")
    with pytest.raises(PerformanceError, match="Private and link-local"):
        validate_origin("http://169.254.169.254")


def test_public_target_resolution_is_validated_and_pinned_for_the_runner(monkeypatch):
    monkeypatch.setattr(performance.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (None, None, None, None, ("1.1.1.1", 0)),
    ])
    pinned = performance._validate_resolved_target("https://shop.example")
    script = performance._script("https://shop.example", ["/api/items"],
                                 {"virtual_users": 1, "duration_seconds": 5}, {"mode": "public"}, pinned)
    assert pinned == "1.1.1.1"
    assert '"hosts":{"shop.example":"1.1.1.1"}' in script

    monkeypatch.setattr(performance.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (None, None, None, None, ("10.0.0.2", 0)),
    ])
    with pytest.raises(PerformanceError, match="private or link-local"):
        performance._validate_resolved_target("https://shop.example")


@pytest.mark.parametrize("route", ["//outside.example/path", "/../private", "/a\\b", "/a\r\nHeader: value"])
def test_route_validation_rejects_non_relative_or_controlled_paths(route):
    with pytest.raises(PerformanceError):
        validate_route(route)


def test_target_profiles_are_isolated_by_user_and_do_not_store_credentials(tmp_path):
    store = PerformanceStore(tmp_path / "quality.sqlite")
    target = store.save_target("alice", {"origin": "https://shop.example", "virtual_users": 2,
                                          "duration_seconds": 10, "auth_mode": "bearer"})
    assert store.get_target("bob", target["target_id"]) is None
    assert "token" not in json.dumps(store.targets("alice"))
    assert store.save_target("alice", {"origin": "https://shop.example", "virtual_users": 4,
                                        "duration_seconds": 25, "auth_mode": "bearer"})["target_id"] == target["target_id"]


def test_run_saves_sanitized_measurements_and_keeps_prior_run(tmp_path):
    store = PerformanceStore(tmp_path / "quality.sqlite")
    target = store.save_target("alice", {"origin": "https://shop.example", "virtual_users": 1,
                                          "duration_seconds": 10, "auth_mode": "bearer"})
    measurements = [{"path": "/api/items", "count": 20, "throughput_rps": 2,
                     "errors": 0, "error_rate": 0, "latency_ms": {"med": 5.0}}]
    report = {"routes": ["/api/items"], "auth_mode": "bearer",
              "workload": {"virtual_users": 1, "duration_seconds": 10},
              "runner_version": "0.54.0", "complete": True, "route_results": measurements}
    first = store.save_run("alice", "pizza", target, report)
    second = store.save_run("alice", "pizza", target, report)
    stored = json.dumps(store.history("alice", "pizza"))
    assert len(store.history("alice", "pizza")) == 2
    assert first["run_id"] != second["run_id"]
    assert "token" not in stored and "password" not in stored and "cookie" not in stored


def test_comparison_marks_non_equivalent_inputs():
    base = {"target_origin": "http://host.docker.internal:8080", "routes": ["/api/items"],
            "auth_mode": "public", "workload": {"virtual_users": 1, "duration_seconds": 10},
            "runner_version": "0.54.0"}
    assert compare_runs(base, dict(base))["equivalent"] is True
    different = compare_runs(base, base | {"workload": {"virtual_users": 4, "duration_seconds": 10}})
    assert different == {"equivalent": False, "differences": ["workload"]}


def test_k6_receives_secrets_only_in_temporary_script_and_returns_per_route_summary(monkeypatch, tmp_path):
    monkeypatch.setattr("code_quality.performance._validate_resolved_target", lambda _origin: None)
    monkeypatch.setattr("code_quality.performance.K6_SCRIPT_TMPFS_DIR", tmp_path)
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["script"] = open(command[-1], encoding="utf-8").read()
        return type("Result", (), {"returncode": 0, "stdout": json.dumps({
            "runner_version": "0.54.0", "routes": [{"path": "/api/items", "count": 12,
                "throughput_rps": 1.2, "errors": 0, "error_rate": 0,
                "latency_ms": {"med": 3, "p(90)": 5, "p(95)": 7, "p(99)": 10}}],
        }).encode(), "stderr": b""})()

    monkeypatch.setattr("code_quality.performance.subprocess.run", run)
    target = {"origin": "http://host.docker.internal:8080", "auth_mode": "bearer",
              "virtual_users": 2, "duration_seconds": 10}
    report = run_benchmark(target, ["/api/items"], bearer_token="secret-value")
    assert report["route_results"][0]["count"] == 12
    assert "secret-value" not in " ".join(captured["command"])
    assert '"token":"secret-value"' in captured["script"]
    assert not __import__("pathlib").Path(captured["command"][-1]).exists()
    assert "bearer_token" not in json.dumps(report)


def test_k6_failure_message_does_not_include_command_output_or_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr("code_quality.performance._validate_resolved_target", lambda _origin: None)
    monkeypatch.setattr("code_quality.performance.K6_SCRIPT_TMPFS_DIR", tmp_path)
    def run(_command, **_kwargs):
        return type("Result", (), {"returncode": 1, "stdout": b"secret-value", "stderr": b"password=secret-value"})()

    monkeypatch.setattr("code_quality.performance.subprocess.run", run)
    with pytest.raises(PerformanceError) as failure:
        run_benchmark({"origin": "https://shop.example", "auth_mode": "public", "virtual_users": 1,
                       "duration_seconds": 5}, ["/" ])
    assert "secret-value" not in str(failure.value)


def test_http_login_failure_aborts_without_saving_a_report(monkeypatch, tmp_path):
    monkeypatch.setattr("code_quality.performance._validate_resolved_target", lambda _origin: None)
    monkeypatch.setattr("code_quality.performance.K6_SCRIPT_TMPFS_DIR", tmp_path)
    monkeypatch.setattr("code_quality.performance.subprocess.run", lambda *_args, **_kwargs:
        type("Result", (), {"returncode": 0, "stdout": json.dumps({"login_failures": 1, "routes": []}).encode(), "stderr": b""})())
    target = {"origin": "https://shop.example", "auth_mode": "http_login", "virtual_users": 1,
              "duration_seconds": 5, "login": {"login_path": "/login", "username_field": "email",
              "password_field": "password", "csrf_path": None, "csrf_cookie_name": "", "csrf_field_name": ""}}
    with pytest.raises(PerformanceError, match="HTTP login or CSRF bootstrap failed"):
        run_benchmark(target, ["/api/items"], login_credentials={"username": "name", "password": "secret"})


def test_network_failures_mark_partial_runs_and_unreachable_targets_fail(monkeypatch, tmp_path):
    monkeypatch.setattr("code_quality.performance._validate_resolved_target", lambda _origin: None)
    monkeypatch.setattr("code_quality.performance.K6_SCRIPT_TMPFS_DIR", tmp_path)
    target = {"origin": "https://shop.example", "auth_mode": "public", "virtual_users": 1,
              "duration_seconds": 5}
    partial = {"routes": [{"path": "/api/items", "count": 4, "network_errors": 1,
                           "throughput_rps": 1, "errors": 1, "error_rate": 0.25,
                           "latency_ms": {"med": 4}}]}
    monkeypatch.setattr("code_quality.performance.subprocess.run", lambda *_args, **_kwargs:
        type("Result", (), {"returncode": 0, "stdout": json.dumps(partial).encode(), "stderr": b""})())
    assert run_benchmark(target, ["/api/items"])["complete"] is False

    unreachable = {"routes": [{"path": "/api/items", "count": 4, "network_errors": 4,
                               "throughput_rps": 1, "errors": 4, "error_rate": 1,
                               "latency_ms": {}}]}
    monkeypatch.setattr("code_quality.performance.subprocess.run", lambda *_args, **_kwargs:
        type("Result", (), {"returncode": 0, "stdout": json.dumps(unreachable).encode(), "stderr": b""})())
    with pytest.raises(PerformanceError, match="could not be reached"):
        run_benchmark(target, ["/api/items"])
