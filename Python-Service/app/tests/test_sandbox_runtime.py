"""sandbox_runtime.sandbox_available() — backend probing + host override tests.

The override parameter lets the UI-configured runner host drive the probe while
preserving the DOCKER_HOST env fallback for existing callers.
"""

import pytest

import sandbox_runtime


@pytest.fixture
def no_docker_host(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    return sandbox_runtime


def test_override_host_used(no_docker_host, monkeypatch):
    captured = {}

    def fake_info(docker_host):
        captured["host"] = docker_host
        return {"ServerVersion": "27.5.1", "Runtimes": {"runc": {}, "runsc": {}}}

    monkeypatch.setattr(sandbox_runtime, "_daemon_info", fake_info)
    result = sandbox_runtime.sandbox_available(docker_host="tcp://sandbox-runner:2375")
    assert captured["host"] == "tcp://sandbox-runner:2375"
    assert result["backend"] == "nested(DinD)"
    assert result["available"] is True
    assert result["runsc"] is True


def test_env_fallback_when_no_override(no_docker_host, monkeypatch):
    captured = {}
    monkeypatch.setenv("DOCKER_HOST", "tcp://sandbox-runner:2375")

    def fake_info(docker_host):
        captured["host"] = docker_host
        return {"ServerVersion": "27.5.1", "Runtimes": {"runsc": {}}}

    monkeypatch.setattr(sandbox_runtime, "_daemon_info", fake_info)
    result = sandbox_runtime.sandbox_available()
    assert captured["host"] == "tcp://sandbox-runner:2375"
    assert result["backend"] == "nested(DinD)"


def test_no_override_no_env_defaults_to_host_socket(no_docker_host, monkeypatch):
    captured = {}

    def fake_info(docker_host):
        captured["host"] = docker_host
        return {"ServerVersion": "27.5.1", "Runtimes": {"runc": {}}}

    monkeypatch.setattr(sandbox_runtime, "_daemon_info", fake_info)
    result = sandbox_runtime.sandbox_available()
    assert captured["host"] == ""
    assert result["backend"] == "host"
    assert result["available"] is False  # no runsc registered
    assert result["runsc"] is False


def test_daemon_unreachable_reported(no_docker_host, monkeypatch):
    def boom(docker_host):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(sandbox_runtime, "_daemon_info", boom)
    result = sandbox_runtime.sandbox_available(docker_host="tcp://127.0.0.1:1")
    assert result["available"] is False
    assert result["runsc"] is False
    assert result["backend"] == "nested(DinD)"
    assert "daemon unreachable" in result["detail"]


def test_unsupported_host_raises_but_available_reports_false(no_docker_host, monkeypatch):
    def boom(docker_host):
        raise ValueError(f"unsupported DOCKER_HOST: {docker_host!r}")

    monkeypatch.setattr(sandbox_runtime, "_daemon_info", boom)
    result = sandbox_runtime.sandbox_available(docker_host="ssh://weird")
    assert result["available"] is False
    assert "daemon unreachable" in result["detail"]


def test_sandbox_keys_in_config_defaults_and_round_trip(tmp_path):
    """sandboxDockerHost / sandboxJobsRoot are UI-editable, persisted keys."""
    from settings_store import RAG_CONFIG_DEFAULTS, SettingsStore, build_config

    assert "sandboxDockerHost" in RAG_CONFIG_DEFAULTS
    assert "sandboxJobsRoot" in RAG_CONFIG_DEFAULTS
    assert RAG_CONFIG_DEFAULTS["sandboxJobsRoot"] == "/jobs"

    store = SettingsStore(db_path=str(tmp_path / "settings.db"))
    store.set_many({"sandboxDockerHost": "tcp://sandbox-runner:2375"})
    config = build_config(store)
    assert config["sandboxDockerHost"] == "tcp://sandbox-runner:2375"
    assert config["sandboxJobsRoot"] == "/jobs"
