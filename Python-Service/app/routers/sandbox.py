"""Sandbox runner HTTP endpoints."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from infra.settings import settings
from runtime.sandbox_runtime import sandbox_available
from runtime.remote_sandbox import remote_sandbox_manager, validate_alias, validate_jobs_root
from sandbox.executor import default_executor
from evaluator.sandbox_hook import EphemeralSqlTagger
from evaluator.service import evaluator_service

router = APIRouter(prefix="/sandbox", tags=["Sandbox"])


def _project_stem(filename: str) -> str:
    """Convert an uploaded filename to the sandbox staging project id."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return re.sub(r"[^A-Za-z0-9_.-]", "_", stem).strip("._") or "project"


@router.get("/status", summary="Sandbox Runner Status")
def sandbox_status():
    mode = str(settings.sandbox_execution_mode or "local").lower()
    root = settings.sandbox_jobs_root or "/jobs"
    if mode == "remote":
        alias = settings.sandbox_remote_ssh_alias or ""
        try:
            if not remote_sandbox_manager.is_ready(alias, root):
                raise RuntimeError("connection test required before remote mode can execute jobs")
            host = remote_sandbox_manager.ensure_tunnel(alias)
            result = sandbox_available(docker_host=host)
        except Exception as exc:  # never fall back to local in remote mode
            result = {"available": False, "backend": "remote", "runsc": False,
                      "detail": f"remote sandbox unavailable: {str(exc)[:300]}"}
            host = "(SSH tunnel unavailable)"
        result.update({"runner": "remote", "remoteAlias": alias, "tunnelState": remote_sandbox_manager.tunnel_state(alias),
                       "dockerHost": host, "jobsRoot": root})
    else:
        host = settings.sandbox_docker_host or None
        result = sandbox_available(docker_host=host)
        result.update({"runner": "local", "remoteAlias": "", "tunnelState": "not used",
                       "dockerHost": host or "(default — host daemon socket)", "jobsRoot": root})
    return {"success": True, **result}


@router.post("/remote/test", summary="Test Remote Sandbox SSH Connection")
def remote_test(body: dict):
    alias = body.get("alias", settings.sandbox_remote_ssh_alias)
    root = body.get("jobsRoot", settings.sandbox_jobs_root)
    return remote_sandbox_manager.test_connection(alias, root)


@router.get("/remote/monitor", summary="Read Remote Sandbox Host Metrics")
def remote_monitor():
    return remote_sandbox_manager.system_monitor(settings.sandbox_remote_ssh_alias)


@router.post("/remote/initialize", summary="Start Remote Sandbox Initialization")
def remote_initialize(body: dict):
    alias = body.get("alias", settings.sandbox_remote_ssh_alias)
    root = body.get("jobsRoot", settings.sandbox_jobs_root)
    try:
        return {"success": True, "job": remote_sandbox_manager.start_initialization(alias, root)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/remote/initialize/{job_id}", summary="Get Remote Sandbox Initialization Status")
def remote_initialize_status(job_id: str):
    job = remote_sandbox_manager.initialization_status(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Remote initialization job not found")
    return {"success": True, "job": job}


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


@router.post("/request", summary="Run a route request in the staged project sandbox")
def sandbox_request(body: dict):
    filename = body.get("filename", "")
    path = body.get("path", "")
    method = body.get("method", "GET")
    headers = body.get("headers") or {}
    request_body = body.get("body")
    if not isinstance(filename, str) or not filename:
        return {"success": False, "error": "Missing filename"}
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or len(path) > 2000:
        return {"success": False, "error": "Route path must start with / and remain bounded"}
    if (
        not isinstance(headers, dict)
        or len(headers) > 50
        or any(
            len(str(k)) > 120
            or len(str(v)) > 4000
            or any(char in str(k) or char in str(v) for char in ("\r", "\n"))
            for k, v in headers.items()
        )
    ):
        return {"success": False, "error": "Headers are malformed or exceed limits"}
    if request_body is not None and (not isinstance(request_body, str) or len(request_body.encode("utf-8")) > 64 * 1024):
        return {"success": False, "error": "Request body must be a string under 64 KiB"}
    try:
        executor = default_executor()
    except ValueError as exc:
        return {"success": False, "error": f"sandbox backend unavailable: {exc}"}

    evaluate = bool(body.get("evaluate_database"))
    hook = EphemeralSqlTagger() if evaluate else None
    hook_result = hook.inject(executor, _project_stem(filename)) if hook else None
    try:
        result = executor.run_api_request(
            _project_stem(filename), str(method), path,
            headers={str(k): str(v) for k, v in headers.items()},
            body=request_body,
            framework=body.get("framework") or "php",
            # Route-only contract: the app endpoint is fixed to the staged
            # sandbox loopback; callers cannot turn this into an open proxy.
            base_url="http://127.0.0.1:8000",
        )
    finally:
        cleanup_result = hook.cleanup(executor) if hook else None

    response = {"success": result.get("status") == "done", **result}
    if hook_result:
        response["database_evaluation"] = {"enabled": True, "hook": hook_result.__dict__}
        if cleanup_result:
            response["database_evaluation"]["cleanup"] = cleanup_result.__dict__
        run = evaluator_service.start({
            "filename": filename,
            "database": body.get("database") or "mysql",
            "slow_log": body.get("slow_log") or "",
            "explain": body.get("explain"),
            "query": body.get("query") or "",
            "schema": body.get("schema") or {},
        })
        response["database_evaluation"]["run_id"] = run.get("run_id")
    return response


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
