"""Execution adapters for user-managed tools."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import urllib.parse
from typing import Any

import requests


def _blocked_host(host: str) -> bool:
    h = host.lower()
    return h in {"localhost", "127.0.0.1", "::1"} or h.startswith(("10.", "192.168.", "169.254.")) or h.startswith("172.")


def execute_tool(tool: dict[str, Any], *, filename: str = "", workspace_path: str | None = None, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = tool.get("config") or {}; args = arguments or {}
    kind = tool.get("type")
    if kind == "shell":
        from sandbox.executor import default_executor
        command = str(cfg.get("command") or "").strip()
        if not command: return {"success": False, "error": "Shell command is empty"}
        for key, value in args.items(): command = command.replace("{{" + key + "}}", shlex.quote(str(value)))
        result = default_executor().run_command(str(filename).rsplit(".", 1)[0] or "project", command, framework=cfg.get("framework") or "php", cwd=cfg.get("cwd"))
        return {"success": result.get("status") == "done", **result}
    if kind == "rest":
        method = str(cfg.get("method") or "GET").upper(); url = str(cfg.get("url") or "")
        for key, value in args.items(): url = url.replace("{{" + key + "}}", urllib.parse.quote(str(value)))
        parsed = urllib.parse.urlparse(url)
        raw_allowed = cfg.get("allowedHosts") or []
        allowed = set(x.strip() for x in raw_allowed.split(",") if x.strip()) if isinstance(raw_allowed, str) else set(raw_allowed)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or _blocked_host(parsed.hostname) or (allowed and parsed.hostname not in allowed):
            return {"success": False, "error": "REST host is not allowed"}
        body = cfg.get("body") if isinstance(cfg.get("body"), (dict, list)) else None
        response = requests.request(method, url, json=body, timeout=min(float(cfg.get("timeoutSeconds", 30)), 120), headers={"Accept": "application/json"})
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
