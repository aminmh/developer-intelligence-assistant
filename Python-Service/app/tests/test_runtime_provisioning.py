from __future__ import annotations

import asyncio

from telemetry import runtime_provisioning


class FakeHostKey:
    def get_fingerprint(self, algorithm: str) -> str:
        assert algorithm == "sha256"
        return "SHA256:test-fingerprint"

    def export_public_key(self, format_name: str) -> bytes:
        assert format_name == "openssh"
        return b"ssh-ed25519 AAAATESTKEY runtime-test"


def test_scan_host_key_uses_default_ssh_port_without_authentication(monkeypatch):
    calls = []

    async def get_server_host_key(host, **kwargs):
        calls.append((host, kwargs))
        return FakeHostKey()

    monkeypatch.setattr(runtime_provisioning.asyncssh, "get_server_host_key", get_server_host_key)

    result = asyncio.run(runtime_provisioning.scan_host_key({"host": "amin-system-product-name.local"}))

    assert result == {
        "fingerprint": "SHA256:test-fingerprint",
        "host": "amin-system-product-name.local",
        "port": 22,
    }
    assert calls == [("amin-system-product-name.local", {"port": 22, "config": None})]


def test_scan_host_key_uses_configured_non_default_port(monkeypatch):
    calls = []

    async def get_server_host_key(host, **kwargs):
        calls.append((host, kwargs))
        return FakeHostKey()

    monkeypatch.setattr(runtime_provisioning.asyncssh, "get_server_host_key", get_server_host_key)

    result = asyncio.run(runtime_provisioning.scan_host_key({"host": "ssh.example.test", "ssh_port": 2222}))

    assert result["port"] == 2222
    assert calls == [("ssh.example.test", {"port": 2222, "config": None})]


def test_connect_keeps_agent_authentication_enabled(monkeypatch):
    calls = []
    connection = object()

    async def get_server_host_key(host, port):
        return FakeHostKey()

    async def connect(host, **kwargs):
        calls.append((host, kwargs))
        return connection

    monkeypatch.setattr(runtime_provisioning, "_get_server_host_key", get_server_host_key)
    monkeypatch.setattr(runtime_provisioning.asyncssh, "import_known_hosts", lambda value: "trusted-host")
    monkeypatch.setattr(runtime_provisioning.asyncssh, "connect", connect)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/devassistant/runtime-host/ssh-agent.sock")

    result, fingerprint = asyncio.run(runtime_provisioning._connect({
        "host": "ssh.example.test",
        "ssh_port": 22,
        "ssh_user": "amin",
        "host_fingerprint": "SHA256:test-fingerprint",
    }))

    assert result is connection
    assert fingerprint == "SHA256:test-fingerprint"
    assert len(calls) == 1
    assert calls[0][1]["agent_path"] == "/run/devassistant/runtime-host/ssh-agent.sock"
    assert "client_keys" not in calls[0][1]
