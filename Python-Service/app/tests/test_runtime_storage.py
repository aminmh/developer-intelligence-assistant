from telemetry import runtime_storage


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
