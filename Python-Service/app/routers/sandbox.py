"""Sandbox runner HTTP endpoints."""

from __future__ import annotations

import re

from fastapi import APIRouter

from infra.settings import settings
from runtime.sandbox_runtime import sandbox_available
from sandbox.executor import default_executor

router = APIRouter(prefix="/sandbox", tags=["Sandbox"])


def _project_stem(filename: str) -> str:
    """Convert an uploaded filename to the sandbox staging project id."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return re.sub(r"[^A-Za-z0-9_.-]", "_", stem).strip("._") or "project"


@router.get("/status", summary="Sandbox Runner Status")
def sandbox_status():
    host = settings.sandbox_docker_host or None
    result = sandbox_available(docker_host=host)
    result["dockerHost"] = host or "(default — host daemon socket)"
    result["jobsRoot"] = settings.sandbox_jobs_root or "/jobs"
    return {"success": True, **result}


@router.post("/command", summary="Run a Shell Command in the Project Sandbox")
def sandbox_command(body: dict):
    filename = body.get("filename", "")
    command = body.get("command", "")
    if not isinstance(command, str) or not command.strip():
        return {"success": False, "error": "Missing command"}
    if len(command) > 4000 or "\x00" in command:
        return {"success": False, "error": "Command too long or contains NUL"}
    if not filename:
        return {"success": False, "error": "Missing filename"}
    try:
        executor = default_executor()
    except ValueError as exc:
        return {"success": False, "error": f"sandbox backend unavailable: {exc}"}
    result = executor.run_command(
        _project_stem(filename),
        command,
        framework=body.get("framework") or "php",
        cwd=body.get("cwd"),
    )
    return {"success": result.get("status") == "done", **result}


@router.post("/clean", summary="Remove a Staged Project from the Sandbox Daemon")
def sandbox_clean(body: dict):
    filename = body.get("filename", "")
    if not isinstance(filename, str) or not filename:
        return {"success": False, "error": "Missing filename"}
    try:
        executor = default_executor()
    except ValueError as exc:
        return {"success": False, "error": f"sandbox backend unavailable: {exc}"}
    result = executor.cleanup_staging(_project_stem(filename))
    return {"success": result.get("status") == "done", **result}

