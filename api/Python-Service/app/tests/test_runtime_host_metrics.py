from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from telemetry import host_metrics, http as telemetry_http, runtime_provisioning, runtime_storage, ssh_tunnel
from telemetry.http import runtime_router


REMOTE_TARGET = {
    "mode": "remote",
    "host": "clickhouse.example.test",
    "ssh_port": 2222,
    "ssh_user": "telemetry",
    "host_fingerprint": "SHA256:verified-host-key",
}


def metrics_output(*, cpu_total=100, cpu_idle=80, clickhouse_path="/var/lib/clickhouse"):
    assert clickhouse_path == "/var/lib/clickhouse"
    return (
        f"cpu {cpu_total} {cpu_idle}\n"
        "memory 16000000000 6000000000\n"
        "load 0.10 0.20 0.30\n"
        "disk 100000000000 40000000000\n"
    )


class FakeResult:
    def __init__(self, stdout: str, exit_status: int = 0):
        self.stdout = stdout
        self.exit_status = exit_status


class FakeConnection:
    def __init__(self, stdout: str | None = None, error: Exception | None = None, wait_seconds: float = 0):
        self.stdout = stdout if stdout is not None else metrics_output()
        self.error = error
        self.wait_seconds = wait_seconds
        self.commands: list[str] = []
        self.closed = False

    async def run(self, command: str, *, check: bool = False):
        self.commands.append(command)
        if self.wait_seconds:
            await asyncio.sleep(self.wait_seconds)
        if self.error:
            raise self.error
        return FakeResult(self.stdout)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


@pytest.fixture(autouse=True)
def clear_monitor_cache(monkeypatch):
    monkeypatch.setattr(host_metrics, "_latest_sample", None)
    monkeypatch.setattr(host_metrics, "_cpu_counters", None)
    monkeypatch.setattr(host_metrics, "_target_identity", None)


def configure_remote(monkeypatch, connect, *, clickhouse_health="verified"):
    monkeypatch.setattr(runtime_storage, "get_storage_target", lambda _registry=None: dict(REMOTE_TARGET))
    monkeypatch.setattr(runtime_storage, "storage_status", lambda _registry=None: {"health": clickhouse_health})
    monkeypatch.setattr(ssh_tunnel, "agent_has_identity", lambda: True)
    monkeypatch.setattr(runtime_provisioning, "_connect", connect)


@pytest.mark.parametrize("mode", ["local", "disabled"])
def test_local_and_disabled_modes_do_not_open_ssh(monkeypatch, mode):
    monkeypatch.setattr(runtime_storage, "get_storage_target", lambda _registry=None: {"mode": mode})

    async def unexpected_connect(_target):
        raise AssertionError("SSH must not run outside Remote mode")

    monkeypatch.setattr(runtime_provisioning, "_connect", unexpected_connect)

    response = host_metrics.collect_host_metrics()

    assert response["mode"] == mode
    assert response["metrics"] is None
    assert response["ssh_health"] == "not-required"


def test_first_remote_sample_warms_cpu_and_next_sample_uses_counter_delta(monkeypatch):
    queued_connections = [
        FakeConnection(metrics_output(cpu_total=100, cpu_idle=80)),
        FakeConnection(metrics_output(cpu_total=200, cpu_idle=150)),
    ]
    connections_used = []
    received_targets = []

    async def connect(target):
        received_targets.append(target)
        connection = queued_connections.pop(0)
        connections_used.append(connection)
        return connection, REMOTE_TARGET["host_fingerprint"]

    configure_remote(monkeypatch, connect)

    first = host_metrics.collect_host_metrics()
    second = host_metrics.collect_host_metrics()

    assert first["available"] is True
    assert first["metrics"]["cpu_percent"] is None
    assert first["ssh_health"] == "connected"
    assert first["clickhouse_health"] == "available"
    assert second["metrics"]["cpu_percent"] == 30.0
    assert second["metrics"]["memory_used_bytes"] == 6_000_000_000
    assert second["metrics"]["disk_free_bytes"] == 40_000_000_000
    assert second["sampled_at"]
    assert received_targets == [REMOTE_TARGET, REMOTE_TARGET]
    commands = [command for connection in connections_used for command in connection.commands]
    assert len(commands) == 2
    assert all(command == host_metrics.METRICS_COMMAND for command in commands)
    assert all("sudo" not in command.lower() for command in commands)


def test_clickhouse_failure_keeps_host_metrics_and_reports_health_separately(monkeypatch):
    connection = FakeConnection()

    async def connect(_target):
        return connection, REMOTE_TARGET["host_fingerprint"]

    configure_remote(monkeypatch, connect, clickhouse_health="unavailable")

    response = host_metrics.collect_host_metrics()

    assert response["available"] is True
    assert response["ssh_health"] == "connected"
    assert response["clickhouse_health"] == "unavailable"
    assert response["clickhouse_message"]
    assert response["metrics"] is not None


@pytest.mark.parametrize("failure", ["malformed", "timeout", "host-key"])
def test_failed_remote_sample_returns_latest_sample_as_stale(monkeypatch, failure):
    valid = FakeConnection()

    async def first_connect(_target):
        return valid, REMOTE_TARGET["host_fingerprint"]

    configure_remote(monkeypatch, first_connect)
    previous = host_metrics.collect_host_metrics()

    if failure == "malformed":
        failed_connection = FakeConnection("unexpected output")

        async def connect(_target):
            return failed_connection, REMOTE_TARGET["host_fingerprint"]

        monkeypatch.setattr(runtime_provisioning, "_connect", connect)
    elif failure == "timeout":
        failed_connection = FakeConnection(error=asyncio.TimeoutError())

        async def connect(_target):
            return failed_connection, REMOTE_TARGET["host_fingerprint"]

        monkeypatch.setattr(runtime_provisioning, "_connect", connect)
    else:
        async def connect(_target):
            raise runtime_provisioning.HostKeyMismatch("private detail must not be returned")

        monkeypatch.setattr(runtime_provisioning, "_connect", connect)

    response = host_metrics.collect_host_metrics()

    assert response["available"] is False
    assert response["stale"] is True
    assert response["sampled_at"] == previous["sampled_at"]
    assert response["metrics"] == previous["metrics"]
    assert response["ssh_health"] == ("unavailable" if failure == "host-key" else "connected")
    assert "private detail" not in response["ssh_message"]


def test_unavailable_keyring_returns_actionable_state_without_connecting(monkeypatch):
    monkeypatch.setattr(runtime_storage, "get_storage_target", lambda _registry=None: dict(REMOTE_TARGET))
    monkeypatch.setattr(ssh_tunnel, "agent_has_identity", lambda: False)

    async def unexpected_connect(_target):
        raise AssertionError("SSH must not run without the saved identity")

    monkeypatch.setattr(runtime_provisioning, "_connect", unexpected_connect)

    response = host_metrics.collect_host_metrics()

    assert response["available"] is False
    assert response["stale"] is False
    assert response["metrics"] is None
    assert "keyring" in response["ssh_message"]


def test_ssh_connection_failure_is_actionable(monkeypatch):
    monkeypatch.setattr(runtime_storage, "get_storage_target", lambda _registry=None: dict(REMOTE_TARGET))
    monkeypatch.setattr(ssh_tunnel, "agent_has_identity", lambda: True)

    async def fail_to_connect(_target):
        raise RuntimeError("SSH connection failed. Check the host, port, and network access.")

    monkeypatch.setattr(runtime_provisioning, "_connect", fail_to_connect)

    response = host_metrics.collect_host_metrics()

    assert response["available"] is False
    assert response["metrics"] is None
    assert "address, port, and network" in response["ssh_message"]


def test_switching_remote_host_discards_the_previous_sample(monkeypatch):
    target = dict(REMOTE_TARGET)
    monkeypatch.setattr(runtime_storage, "get_storage_target", lambda _registry=None: dict(target))
    monkeypatch.setattr(runtime_storage, "storage_status", lambda _registry=None: {"health": "verified"})
    monkeypatch.setattr(ssh_tunnel, "agent_has_identity", lambda: True)
    connection = FakeConnection()

    async def connect(_target):
        return connection, REMOTE_TARGET["host_fingerprint"]

    monkeypatch.setattr(runtime_provisioning, "_connect", connect)
    assert host_metrics.collect_host_metrics()["metrics"] is not None

    target["host"] = "another-clickhouse.example.test"
    monkeypatch.setattr(ssh_tunnel, "agent_has_identity", lambda: False)
    response = host_metrics.collect_host_metrics()

    assert response["stale"] is False
    assert response["sampled_at"] is None
    assert response["metrics"] is None


def test_concurrent_calls_serialize_remote_queries(monkeypatch):
    state_lock = Lock()
    active = 0
    max_active = 0
    samples = 0

    async def connect(_target):
        nonlocal samples
        with state_lock:
            samples += 1
            current = samples
        connection = FakeConnection(metrics_output(cpu_total=100 + current * 100, cpu_idle=80 + current * 80), wait_seconds=0.05)
        original_run = connection.run

        async def tracked_run(command, *, check=False):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                return await original_run(command, check=check)
            finally:
                with state_lock:
                    active -= 1

        connection.run = tracked_run
        return connection, REMOTE_TARGET["host_fingerprint"]

    configure_remote(monkeypatch, connect)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _index: host_metrics.collect_host_metrics(), range(2)))

    assert all(response["available"] for response in responses)
    assert max_active == 1


def test_host_metrics_endpoint_requires_internal_service_credential(monkeypatch):
    app = FastAPI()
    app.include_router(runtime_router)
    monkeypatch.setenv("RAG_CALLBACK_SECRET", "runtime-test-secret-with-at-least-32-characters")

    response = TestClient(app).get("/telemetry/runtime/host-metrics")

    assert response.status_code == 401


def test_host_metrics_endpoint_returns_nonremote_mode_without_ssh(monkeypatch):
    app = FastAPI()
    app.include_router(runtime_router)
    secret = "runtime-test-secret-with-at-least-32-characters"
    monkeypatch.setenv("RAG_CALLBACK_SECRET", secret)
    monkeypatch.setattr(runtime_storage, "get_storage_target", lambda _registry=None: {"mode": "disabled"})
    monkeypatch.setattr(telemetry_http, "get_registry", lambda: None)

    async def unexpected_connect(_target):
        raise AssertionError("SSH must not run outside Remote mode")

    monkeypatch.setattr(runtime_provisioning, "_connect", unexpected_connect)

    response = TestClient(app).get("/telemetry/runtime/host-metrics", headers={"x-internal-secret": secret})

    assert response.status_code == 200
    assert response.json()["mode"] == "disabled"
    assert response.json()["metrics"] is None
