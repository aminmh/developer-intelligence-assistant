from pathlib import Path

import pytest
import yaml

from telemetry import runtime_storage

BASE_CONFIG = Path(__file__).resolve().parents[3] / "telemetry-dev" / "collector-runtime.yaml"


def _generated_config(tmp_path, monkeypatch, mode: str) -> dict:
    if not BASE_CONFIG.is_file():
        pytest.skip("Collector base configuration is not available in this checkout")
    monkeypatch.setattr(runtime_storage, "_BASE_CONFIG", BASE_CONFIG)
    monkeypatch.setattr(runtime_storage, "_STATE_DIR", tmp_path)
    runtime_storage.write_collector_config({"mode": mode, "database": "telemetry"})
    return yaml.safe_load((tmp_path / "collector-runtime.yaml").read_text(encoding="utf-8"))


def test_local_collector_keeps_only_the_full_span_pipeline(tmp_path, monkeypatch):
    config = _generated_config(tmp_path, monkeypatch, "local")

    pipelines = config["service"]["pipelines"]
    assert list(pipelines) == ["traces/full"]
    assert pipelines["traces/full"]["exporters"] == ["clickhouse/full"]
    assert "tail_sampling/runtime" not in config["processors"]
    assert "p95policy/runtime" not in config["processors"]
    assert set(config["exporters"]) == {"clickhouse/full"}
    assert "connectors" not in config
    # Privacy processing stays on the remaining pipeline.
    assert {"filter/runtime_privacy", "transform/runtime_privacy"} <= set(pipelines["traces/full"]["processors"])


def test_remote_collector_keeps_sampled_traces_and_span_metrics(tmp_path, monkeypatch):
    config = _generated_config(tmp_path, monkeypatch, "remote")

    assert {"traces/full", "traces/sampled", "metrics/runtime"} <= set(config["service"]["pipelines"])
    assert "spanmetrics/runtime" in config["connectors"]


def test_disabled_collector_has_no_pipelines(tmp_path, monkeypatch):
    config = _generated_config(tmp_path, monkeypatch, "disabled")

    assert config["service"]["pipelines"] == {}


def test_switching_between_local_and_remote_restarts_the_collector(monkeypatch):
    saved = {"mode": "remote", "database": "telemetry", "host": "db.example.test", "ssh_port": 22,
             "ssh_user": "runtime", "host_fingerprint": "SHA256:abc"}
    restarts = []

    class Store:
        def set_runtime_setting(self, _key, _value):
            pass

    monkeypatch.setattr(runtime_storage, "get_storage_target", lambda _registry=None: dict(saved))
    monkeypatch.setattr(runtime_storage, "public_storage_target", lambda _registry=None: {})
    monkeypatch.setattr(runtime_storage, "_registry", lambda _registry=None: Store())
    monkeypatch.setattr(runtime_storage, "verify_local_clickhouse_connection", lambda: None)
    for name in ("write_router_target", "write_ssh_target", "write_collector_config"):
        monkeypatch.setattr(runtime_storage, name, lambda _target: None)
    monkeypatch.setattr(runtime_storage, "request_collector_restart", lambda: restarts.append(True) or True)

    result = runtime_storage.set_storage_mode("local")

    assert restarts == [True]
    assert result["collector_restart_requested"] is True


def test_local_storage_uses_configured_clickhouse_endpoint(monkeypatch):
    monkeypatch.setenv("TELEMETRY_CLICKHOUSE_USER", "telemetry")
    monkeypatch.setenv("TELEMETRY_CLICKHOUSE_PASSWORD", "local-test-password")
    monkeypatch.setenv("TELEMETRY_CLICKHOUSE_URL", "http://telemetry-clickhouse:8123/")
    monkeypatch.setattr(
        runtime_storage,
        "get_storage_target",
        lambda _registry=None: {"mode": "local", "database": "telemetry"},
    )

    connection = runtime_storage.clickhouse_connection(object())

    assert connection["mode"] == "local"
    assert connection["url"] == "http://telemetry-clickhouse:8123"


def test_remote_storage_uses_clickhouse_router(monkeypatch):
    monkeypatch.setenv("TELEMETRY_CLICKHOUSE_USER", "telemetry")
    monkeypatch.setenv("TELEMETRY_CLICKHOUSE_PASSWORD", "remote-test-password")
    monkeypatch.setenv("TELEMETRY_CLICKHOUSE_URL", "http://telemetry-clickhouse:8123")
    monkeypatch.setattr(
        runtime_storage,
        "get_storage_target",
        lambda _registry=None: {"mode": "remote", "database": "telemetry"},
    )

    connection = runtime_storage.clickhouse_connection(object())

    assert connection["mode"] == "remote"
    assert connection["url"] == "http://telemetry-clickhouse-router:8123"
