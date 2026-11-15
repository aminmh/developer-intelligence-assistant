"""Execution adapters for user-managed tools."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import subprocess
import urllib.parse
from typing import Any

import requests

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


def _address_blocked(address: str) -> bool:
    """True for loopback, private, link-local, reserved, multicast, or unspecified."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


def _blocked_host(host: str) -> bool:
    """Resolve ``host`` and block it when any resolved address is not public.

    Checking every resolved address (not the host text) also catches public
    names that point at private addresses. Resolution failure blocks the call.
    """
    h = host.strip().lower().rstrip(".")
    if not h or h == "localhost" or h.endswith(".localhost") or h.endswith(".internal"):
        return True
    try:
        infos = socket.getaddrinfo(h, None)
    except (socket.gaierror, UnicodeError):
        return True
    addresses = {info[4][0] for info in infos}
    return not addresses or any(_address_blocked(a) for a in addresses)


def _allowed_hosts(cfg: dict[str, Any]) -> set[str]:
    """Configured allowlist, or the template's own host when that is fixed."""
    raw = cfg.get("allowedHosts") or []
    hosts = {x.strip().lower() for x in (raw.split(",") if isinstance(raw, str) else raw) if str(x).strip()}
    if hosts:
        return hosts
    template_host = urllib.parse.urlparse(str(cfg.get("url") or "")).hostname or ""
    return set() if (not template_host or "{" in template_host) else {template_host.lower()}


def execute_tool(tool: dict[str, Any], *, filename: str = "", workspace_path: str | None = None, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = tool.get("config") or {}; args = arguments or {}
    kind = tool.get("type")
    if kind == "rest":
        method = str(cfg.get("method") or "GET").upper(); url = str(cfg.get("url") or "")
        # Arguments fill path or query values only: "/" and "?" are escaped, so
        # an argument cannot add path segments or change the host.
        url = _PLACEHOLDER.sub(lambda m: urllib.parse.quote(str(args.get(m.group(1), "")), safe=""), url)
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host or host not in _allowed_hosts(cfg) or _blocked_host(host):
            return {"success": False, "error": "REST host is not allowed"}
        body = cfg.get("body") if isinstance(cfg.get("body"), (dict, list)) else None
        try:
            response = requests.request(method, url, json=body, timeout=min(float(cfg.get("timeoutSeconds", 30)), 120),
                                        headers={"Accept": "application/json"}, allow_redirects=False)
        except requests.RequestException as exc:
            return {"success": False, "error": f"REST request failed: {str(exc)[:300]}"}
        if 300 <= response.status_code < 400:
            return {"success": False, "status_code": response.status_code, "error": "REST redirects are not followed"}
        text = response.text[: int(cfg.get("maxResponseBytes", 200000))]
        try: payload = response.json()
        except Exception: payload = text
        return {"success": response.ok, "status_code": response.status_code, "result": payload}
    if kind == "mcp":
        command = str(cfg.get("command") or "").strip(); image = str(cfg.get("workerImage") or "node:22-alpine")
        if not command: return {"success": False, "error": "MCP command is empty"}
        try:
            argv = ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL", "--pids-limit", "128", "--memory", "256m", image, command, *[str(x) for x in (cfg.get("args") or [])]]
            proc = subprocess.run(argv, input=json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/list"}) + "\n", text=True, capture_output=True, timeout=min(float(cfg.get("timeoutSeconds", 30)), 120), cwd=None, env={"PATH": os.environ.get("PATH", "")})
            return {"success": proc.returncode == 0, "exit_code": proc.returncode, "stdout": proc.stdout[-200000:], "stderr": proc.stderr[-5000:]}
        except Exception as exc: return {"success": False, "error": str(exc)}
    return {"success": False, "error": "Unsupported tool type"}
