"""sandbox_available() — truthful answer for whichever Docker daemon is active.

Probes the daemon ``DOCKER_HOST`` points at (the host daemon with a
host-installed runsc, or the nested sandbox-runner DinD daemon) so the same
call answers correctly for both backends. The pipeline must not branch on
backend type anywhere else — this module is the only place that knows.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.request
from typing import Any


def _http_get(base_url: str, path: str, timeout: float = 3.0) -> dict[str, Any]:
    url = base_url.replace("tcp://", "http://").rstrip("/") + path
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _unix_get(socket_path: str, path: str, timeout: float = 3.0) -> dict[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path)
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: docker\r\n\r\n".encode("ascii"))
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    _head, _sep, body = data.partition(b"\r\n\r\n")
    return json.loads(body.decode("utf-8"))


def _daemon_info(docker_host: str | None = None) -> dict[str, Any]:
    host = docker_host if docker_host else os.environ.get("DOCKER_HOST", "")
    if not host or host.startswith("unix://"):
        socket_path = host.replace("unix://", "") or "/var/run/docker.sock"
        return _unix_get(socket_path, "/info")
    if host.startswith(("tcp://", "http://")):
        return _http_get(host, "/info")
    raise ValueError(f"unsupported DOCKER_HOST: {host!r}")


def sandbox_available(docker_host: str | None = None) -> dict[str, Any]:
    """Report whether the active daemon can run jobs under the runsc runtime.

    ``docker_host`` overrides the daemon to probe (e.g. the UI-configured
    nested sandbox-runner endpoint); when None, ``DOCKER_HOST`` from the
    environment decides, matching the original behavior for existing callers.

    Returns:
        available: True only when runsc is registered on the probed daemon.
        backend:   "nested(DinD)" when a DOCKER_HOST-style endpoint is used,
                   else "host".
        runsc:     whether the runsc runtime is registered.
        detail:    one-line reason (server version + registered runtimes, or
                   the connection error) for log lines.
    """
    docker_host = docker_host if docker_host else os.environ.get("DOCKER_HOST", "")
    backend = "nested(DinD)" if docker_host else "host"
    try:
        info = _daemon_info(docker_host)
    except Exception as exc:  # noqa: BLE001 - every failure is reported to caller
        return {
            "available": False,
            "backend": backend,
            "runsc": False,
            "detail": f"daemon unreachable: {exc}",
        }
    runtimes = info.get("Runtimes") or {}
    runsc = "runsc" in runtimes
    return {
        "available": runsc,
        "backend": backend,
        "runsc": runsc,
        "detail": (
            f"docker {info.get('ServerVersion', '?')}; "
            f"runtimes: {', '.join(sorted(runtimes)) or 'none'}"
        ),
    }
