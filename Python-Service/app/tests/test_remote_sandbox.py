"""Remote SSH transport tests; all subprocess interaction is mocked."""

from types import SimpleNamespace

import pytest

from runtime.remote_sandbox import (
    RemoteSandboxManager,
    _bootstrap_script,
    validate_alias,
    validate_jobs_root,
)


def test_alias_and_root_validation_reject_shell_shaped_values():
    assert validate_alias("linuxbox.prod_1") == "linuxbox.prod_1"
    assert validate_jobs_root("/var/lib/dev-assistant-sandbox/jobs/") == "/var/lib/dev-assistant-sandbox/jobs"
    with pytest.raises(ValueError):
        validate_alias("user@host")
    with pytest.raises(ValueError):
        validate_alias("linuxbox; whoami")
    with pytest.raises(ValueError):
        validate_jobs_root("relative/jobs")
    with pytest.raises(ValueError):
        validate_jobs_root("/jobs/../etc")


def test_connection_success_marks_alias_ready(monkeypatch):
    manager = RemoteSandboxManager()
    monkeypatch.setattr(manager, "_run_ssh", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="CHECK:ok\n", stderr=""))
    result = manager.test_connection("linuxbox", "/var/lib/dev-assistant-sandbox/jobs")
    assert result["success"] is True
    assert manager.is_ready("linuxbox", "/var/lib/dev-assistant-sandbox/jobs") is True


def test_remote_monitor_parses_procfs_metrics(monkeypatch):
    manager = RemoteSandboxManager()
    monkeypatch.setattr(
        manager,
        "_run_ssh",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="MONITOR cpu=12.5 ram_total=34359738368 ram_free=8589934592 rx=2048 tx=512\n",
            stderr="",
        ),
    )
    result = manager.system_monitor("linuxbox")
    assert result == {
        "success": True,
        "cpuPercent": 12.5,
        "ramTotalBytes": 34359738368,
        "ramFreeBytes": 8589934592,
        "ramUsedBytes": 25769803776,
        "networkDownloadBytesPerSec": 2048,
        "networkUploadBytesPerSec": 512,
    }


def test_connection_reports_root_permission_without_leaking_paths(monkeypatch):
    manager = RemoteSandboxManager()
    monkeypatch.setattr(manager, "_run_ssh", lambda *args, **kwargs: SimpleNamespace(returncode=23, stdout="CHECK:jobs_root=not_writable\n", stderr="/root/.ssh/id_ed25519"))
    result = manager.test_connection("linuxbox", "/jobs")
    assert result["success"] is False
    assert result["reachable"] is True
    assert result["jobsRootWritable"] is False
    assert "/root/.ssh" not in result["detail"]


def test_connection_reports_missing_ssh_client_without_raising(monkeypatch):
    manager = RemoteSandboxManager()
    monkeypatch.setattr(manager, "_run_ssh", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("ssh")))
    result = manager.test_connection("linuxbox", "/jobs")
    assert result["success"] is False
    assert "does not include the SSH client" in result["detail"]


def test_tunnel_reused_and_closed(monkeypatch):
    manager = RemoteSandboxManager()
    process = SimpleNamespace(poll=lambda: None, terminate=lambda: None, wait=lambda timeout: None, stderr=None)
    monkeypatch.setattr("runtime.remote_sandbox._free_port", lambda: 40123)
    monkeypatch.setattr("runtime.remote_sandbox.subprocess.Popen", lambda *args, **kwargs: process)
    first = manager.ensure_tunnel("linuxbox")
    second = manager.ensure_tunnel("linuxbox")
    assert first == second == "tcp://127.0.0.1:40123"
    assert manager.tunnel_state("linuxbox") == "active"
    manager.close_tunnel()
    assert manager.tunnel_state("linuxbox") == "idle"


def test_initializer_reports_bootstrap_failure(monkeypatch):
    manager = RemoteSandboxManager()
    monkeypatch.setattr(manager, "_run_ssh", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="sudo: a password is required"))
    job = manager.start_initialization("linuxbox", "/jobs")
    import time
    for _ in range(20):
        state = manager.initialization_status(job["jobId"])
        if state and state["status"] != "running":
            break
        time.sleep(0.01)
    assert state["status"] == "failed"
    assert "password" in state["detail"]


def test_bootstrap_preserves_an_existing_docker_installation():
    script = _bootstrap_script("/var/lib/dev-assistant-sandbox/jobs")
    assert "if ! command -v docker" in script
    assert "apt-get install -y --no-install-recommends docker.io" in script
    assert script.index("if ! command -v docker") < script.index(
        "apt-get install -y --no-install-recommends docker.io"
    )
    assert "https://storage.googleapis.com/gvisor/releases release main" in script
    assert "apt-get install -y --no-install-recommends runsc" in script
    assert "gvisor.tar.zstd" not in script
    assert 'Dir::Etc::sourcelist="sources.list.d/gvisor.list"' in script
    assert "gpg --batch --yes --dearmor" in script
    assert "gvisor.tar.bz2" in script
    assert "sha512sum -c gvisor.tar.bz2.sha512" in script
    assert "install -d -m 0770 -o root -g docker" in script
