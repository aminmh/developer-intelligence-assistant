"""Remote Docker/gVisor sandbox transport over an SSH-config alias.

The service never reads keys or SSH config content. OpenSSH resolves the alias
from the read-only ``/root/.ssh`` mount and exposes the remote Docker socket
only on an ephemeral loopback port in this container.
"""

from __future__ import annotations

import re
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any


_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_JOBS_ROOT = re.compile(r"^/[A-Za-z0-9_./-]*$")
_IDLE_SECONDS = 90
_SSH_OPTIONS = [
    "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
    "-o", "ConnectTimeout=12", "-o", "NumberOfPasswordPrompts=0",
    "-o", "ExitOnForwardFailure=yes",
]


def validate_alias(alias: object) -> str:
    value = str(alias or "").strip()
    if not _ALIAS.fullmatch(value):
        raise ValueError("Remote SSH alias must be an SSH-config host alias (letters, digits, dot, dash, underscore).")
    return value


def validate_jobs_root(path: object) -> str:
    value = str(path or "").strip()
    if not _JOBS_ROOT.fullmatch(value) or ".." in value.split("/"):
        raise ValueError("Remote jobs root must be an absolute, normalized Linux path.")
    return value.rstrip("/") or "/"


def _safe_detail(value: object) -> str:
    """Keep diagnostics useful without returning local paths or SSH output noise."""
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"/(?:Users|home|root)/[^\s:]+", "[local path hidden]", text)
    text = re.sub(r"\s+", " ", text)
    return text[:500] or "No diagnostic was returned."


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _remote_check_script(jobs_root: str) -> str:
    # ``jobs_root`` is validated before interpolation and only printed as a
    # remote path. The leading markers make remote diagnostics easy to parse.
    return f'''set -eu
command -v docker >/dev/null || {{ echo "CHECK:docker=missing"; exit 20; }}
docker info --format '{{{{json .Runtimes}}}}' >/tmp/sandbox-runtimes.json 2>/dev/null || {{ echo "CHECK:docker=unavailable"; exit 21; }}
grep -q '"runsc"' /tmp/sandbox-runtimes.json || {{ echo "CHECK:runsc=missing"; exit 22; }}
test -d {jobs_root!r} && test -w {jobs_root!r} || {{ echo "CHECK:jobs_root=not_writable"; exit 23; }}
echo "CHECK:ok"
'''


def _bootstrap_script(jobs_root: str) -> str:
    """Versioned, idempotent Debian/Ubuntu bootstrap run through ``sudo -n``."""
    return f'''#!/bin/sh
set -eu
BOOTSTRAP_VERSION=1
if [ ! -r /etc/os-release ]; then echo "Unsupported distribution: no os-release"; exit 40; fi
. /etc/os-release
case "$ID:$VERSION_ID" in
  debian:12|ubuntu:22.04|ubuntu:24.04) ;;
  *) echo "Unsupported distribution: $ID $VERSION_ID (need Debian 12 or Ubuntu 22.04/24.04)"; exit 40 ;;
esac
export DEBIAN_FRONTEND=noninteractive
# Avoid failing an otherwise healthy setup because an unrelated third-party
# APT source is down. Refresh all configured sources only when a prerequisite
# is actually absent; existing Docker hosts normally already have these tools.
missing_prerequisite=0
for bin in curl gpg python3; do command -v "$bin" >/dev/null 2>&1 || missing_prerequisite=1; done
if [ "$missing_prerequisite" -eq 1 ]; then
  apt-get update
  apt-get install -y --no-install-recommends ca-certificates curl gnupg python3
fi
# Do not replace an existing Docker CE installation with Ubuntu/Debian's
# docker.io package: the packages conflict and an existing daemon is valid.
if ! command -v docker >/dev/null 2>&1; then
  apt-get install -y --no-install-recommends docker.io
fi
systemctl enable --now docker
arch="$(dpkg --print-architecture)"
case "$arch" in amd64|arm64) ;; *) echo "Unsupported CPU architecture: $arch"; exit 41 ;; esac
# gVisor's package repository is the supported installation path. It carries
# the complete current runtime layout instead of assuming legacy standalone
# runsc/containerd-shim binaries exist at a download URL.
curl -fsSL https://gvisor.dev/archive.key | gpg --batch --yes --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$arch signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" > /etc/apt/sources.list.d/gvisor.list
# Only update the repository just added. Existing distribution package lists
# remain available for dependency resolution without contacting unrelated
# repositories such as optional developer tools.
apt-get update -o Dir::Etc::sourcelist="sources.list.d/gvisor.list" -o Dir::Etc::sourceparts="-" -o APT::Get::List-Cleanup="0"
if ! apt-get install -y --no-install-recommends runsc; then
  # Some networks can reach repository metadata but reject the package object.
  # Fall back to gVisor's documented, checksum-verified complete release
  # archive; it includes runsc, the shim, and the required gvisor-bin sidecars.
  case "$arch" in amd64) gvisor_arch=x86_64 ;; arm64) gvisor_arch=aarch64 ;; esac
  gvisor_url="https://storage.googleapis.com/gvisor/releases/release/latest/$gvisor_arch"
  gvisor_tmp="$(mktemp -d)"
  curl -fsSL "$gvisor_url/gvisor.tar.bz2" -o "$gvisor_tmp/gvisor.tar.bz2"
  curl -fsSL "$gvisor_url/gvisor.tar.bz2.sha512" -o "$gvisor_tmp/gvisor.tar.bz2.sha512"
  (cd "$gvisor_tmp" && sha512sum -c gvisor.tar.bz2.sha512)
  tar -xjf "$gvisor_tmp/gvisor.tar.bz2" -C /usr/local/bin
  rm -rf "$gvisor_tmp"
fi
export RUNSC_PATH="$(command -v runsc)"
python3 - <<'PY'
import json
import os
from pathlib import Path
p = Path('/etc/docker/daemon.json')
try:
    data = json.loads(p.read_text()) if p.exists() else {{}}
except json.JSONDecodeError as e:
    raise SystemExit(f'Existing /etc/docker/daemon.json is invalid; preserved: {{e}}')
runtimes = data.setdefault('runtimes', {{}})
runtimes.setdefault('runsc', {{'path': os.environ['RUNSC_PATH']}})
p.write_text(json.dumps(data, indent=2) + '\\n')
PY
systemctl restart docker
install -d -m 0750 -o root -g docker {jobs_root!r}
if [ -n "${{SUDO_USER:-}}" ]; then usermod -aG docker "$SUDO_USER"; fi
docker info --format '{{{{json .Runtimes}}}}' | grep -q '"runsc"'
runsc --version >/dev/null
echo "Remote sandbox bootstrap v$BOOTSTRAP_VERSION complete"
'''


@dataclass
class _InitJob:
    id: str
    alias: str
    jobs_root: str
    status: str = "running"
    detail: str = "Starting remote bootstrap…"
    log: list[str] | None = None

    def view(self) -> dict[str, Any]:
        return {"jobId": self.id, "alias": self.alias, "jobsRoot": self.jobs_root,
                "status": self.status, "detail": self.detail, "log": list(self.log or [])}


class RemoteSandboxManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tunnel: subprocess.Popen[str] | None = None
        self._tunnel_alias = ""
        self._tunnel_port: int | None = None
        self._last_used = 0.0
        self._jobs: dict[str, _InitJob] = {}
        self._ready: set[tuple[str, str]] = set()
        self._idle_timer: threading.Timer | None = None

    def _run_ssh(
        self, alias: str, script: str, timeout: int = 30, remote_argv: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*_SSH_OPTIONS, alias, *(remote_argv or ["sh", "-s"])], input=script, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False,
        )

    def test_connection(self, alias: object, jobs_root: object) -> dict[str, Any]:
        try:
            target, root = validate_alias(alias), validate_jobs_root(jobs_root)
        except ValueError as exc:
            return {"success": False, "reachable": False, "detail": str(exc)}
        try:
            result = self._run_ssh(target, _remote_check_script(root))
        except subprocess.TimeoutExpired:
            return {"success": False, "reachable": False, "detail": "SSH connection timed out."}
        except FileNotFoundError:
            return {
                "success": False,
                "reachable": False,
                "detail": "The sandbox service image does not include the SSH client. Rebuild and restart python-api.",
            }
        except OSError as exc:
            return {"success": False, "reachable": False, "detail": f"SSH client could not start: {_safe_detail(exc)}"}
        output = _safe_detail(result.stdout or result.stderr)
        if result.returncode == 0:
            with self._lock:
                self._ready.add((target, root))
            return {"success": True, "reachable": True, "docker": True, "runsc": True,
                    "jobsRootWritable": True, "detail": "SSH, Docker, gVisor, and jobs directory are ready."}
        if "CHECK:jobs_root=not_writable" in result.stdout:
            return {"success": False, "reachable": True, "docker": True, "runsc": True,
                    "jobsRootWritable": False, "detail": "Docker and gVisor are ready, but the configured jobs directory is not writable. Initialize it or correct its permissions."}
        if "CHECK:runsc=missing" in result.stdout:
            return {"success": False, "reachable": True, "docker": True, "runsc": False,
                    "jobsRootWritable": False, "detail": "Docker is reachable but the runsc runtime is not registered."}
        if "CHECK:docker=" in result.stdout:
            return {"success": False, "reachable": True, "docker": False, "runsc": False,
                    "jobsRootWritable": False, "detail": "SSH authentication succeeded, but Docker is unavailable for this account."}
        return {"success": False, "reachable": False, "docker": False, "runsc": False,
                "jobsRootWritable": False, "detail": f"SSH connection failed: {output}"}

    def ensure_tunnel(self, alias: object) -> str:
        target = validate_alias(alias)
        with self._lock:
            if self._tunnel and self._tunnel.poll() is None and self._tunnel_alias == target:
                self._last_used = time.monotonic()
                self._schedule_idle_cleanup()
                return f"tcp://127.0.0.1:{self._tunnel_port}"
            self.close_tunnel()
            port = _free_port()
            proc = subprocess.Popen(
                [*_SSH_OPTIONS, "-N", "-L", f"127.0.0.1:{port}:/var/run/docker.sock", target],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            time.sleep(0.2)
            if proc.poll() is not None:
                error = _safe_detail(proc.stderr.read() if proc.stderr else "")
                raise RuntimeError(f"SSH tunnel could not start: {error}")
            self._tunnel, self._tunnel_alias, self._tunnel_port = proc, target, port
            self._last_used = time.monotonic()
            self._schedule_idle_cleanup()
            return f"tcp://127.0.0.1:{port}"

    def _schedule_idle_cleanup(self) -> None:
        if self._idle_timer:
            self._idle_timer.cancel()
        self._idle_timer = threading.Timer(_IDLE_SECONDS, self.close_idle_tunnel)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def is_ready(self, alias: object, jobs_root: object) -> bool:
        try:
            key = (validate_alias(alias), validate_jobs_root(jobs_root))
        except ValueError:
            return False
        with self._lock:
            return key in self._ready

    def tunnel_state(self, alias: object) -> str:
        try:
            target = validate_alias(alias)
        except ValueError:
            return "not configured"
        with self._lock:
            if not self._tunnel or self._tunnel_alias != target:
                return "idle"
            return "active" if self._tunnel.poll() is None else "lost"

    def close_idle_tunnel(self) -> None:
        with self._lock:
            if self._tunnel and time.monotonic() - self._last_used > _IDLE_SECONDS:
                self.close_tunnel()
            elif self._tunnel:
                self._schedule_idle_cleanup()

    def close_tunnel(self) -> None:
        with self._lock:
            if self._idle_timer:
                self._idle_timer.cancel()
                self._idle_timer = None
            if self._tunnel and self._tunnel.poll() is None:
                self._tunnel.terminate()
                try: self._tunnel.wait(timeout=3)
                except subprocess.TimeoutExpired: self._tunnel.kill()
            self._tunnel = None
            self._tunnel_alias = ""
            self._tunnel_port = None

    def start_initialization(self, alias: object, jobs_root: object) -> dict[str, Any]:
        target, root = validate_alias(alias), validate_jobs_root(jobs_root)
        job = _InitJob(str(uuid.uuid4()), target, root, log=[])
        with self._lock: self._jobs[job.id] = job
        threading.Thread(target=self._initialize, args=(job,), daemon=True).start()
        return job.view()

    def _initialize(self, job: _InitJob) -> None:
        job.log.append("Checking passwordless sudo and installing Docker/gVisor prerequisites.")
        try:
            result = self._run_ssh(
                job.alias, _bootstrap_script(job.jobs_root), timeout=900,
                remote_argv=["sudo", "-n", "sh", "-s"],
            )
            if result.returncode:
                job.status = "failed"; job.detail = _safe_detail(result.stderr or result.stdout)
                job.log.append("Remote bootstrap failed."); return
            job.log.append("Bootstrap finished; opening a fresh SSH session to verify Docker and runsc.")
            verified = self.test_connection(job.alias, job.jobs_root)
            if not verified.get("success"):
                job.status = "failed"; job.detail = str(verified.get("detail")); job.log.append("Fresh-session verification failed."); return
            job.status = "done"; job.detail = "Remote Docker, runsc, and jobs directory are ready."; job.log.append("Fresh-session verification passed.")
        except subprocess.TimeoutExpired:
            job.status = "failed"; job.detail = "Remote initialization timed out. Package installation may still be running on the host."
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"; job.detail = _safe_detail(exc)

    def initialization_status(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.view() if job else None


remote_sandbox_manager = RemoteSandboxManager()
