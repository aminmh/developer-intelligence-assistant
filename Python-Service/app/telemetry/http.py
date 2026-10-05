"""Project-scoped CI API. Disabled unless dedicated credentials are configured."""
from __future__ import annotations

import hmac
import json
import os
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from telemetry.registry import SnapshotConflict, SnapshotRegistry, default_registry
from telemetry.snapshot import validate_project_id, validate_revision_id, validate_scope
from telemetry.source import MAX_ARCHIVE_BYTES, SourceService

router = APIRouter(prefix="/telemetry/projects", tags=["Telemetry snapshots"])
host_router = APIRouter(prefix="/telemetry/host", tags=["Telemetry deployments"])
runtime_router = APIRouter(prefix="/telemetry/runtime", tags=["Runtime telemetry"])
MAX_HOST_REPORT_BYTES = 2 * 1024 * 1024


def authorize(request: Request, project_id: str) -> None:
    try:
        credentials = json.loads(os.environ.get("TELEMETRY_CI_PROJECT_TOKENS", "{}"))
        expected = credentials.get(project_id) if isinstance(credentials, dict) else None
    except (ValueError, TypeError):
        expected = None
    header = request.headers.get("authorization", "")
    provided = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
    if (not isinstance(expected, str) or len(expected) < 32
            or not hmac.compare_digest(provided.encode(), expected.encode())):
        raise HTTPException(401, "Valid project-scoped CI credentials required")


def get_registry() -> SnapshotRegistry:
    return default_registry()


def get_storage():
    from index.storage import storage
    return storage


def authorize_internal(request: Request) -> None:
    expected = os.environ.get("RAG_CALLBACK_SECRET", "")
    provided = request.headers.get("x-internal-secret", "")
    if (not expected or len(expected) < 32
            or not hmac.compare_digest(provided.encode(), expected.encode())):
        raise HTTPException(401, "Internal service credentials required")


@runtime_router.get("/projects")
def runtime_projects(request: Request) -> list[dict]:
    authorize_internal(request)
    return get_registry().runtime_projects()


@runtime_router.get("/projects/{project_id}/snapshots/{commit_sha}")
def runtime_snapshot(project_id: str, commit_sha: str, request: Request) -> dict:
    authorize_internal(request)
    try:
        validate_scope(project_id, commit_sha)
        from telemetry.runtime import project_runtime
        return project_runtime(project_id, commit_sha, get_registry())
    except ValueError as exc:
        raise HTTPException(422, "Invalid project snapshot identity") from exc


@runtime_router.get("/projects/{project_id}/revisions/{revision_id}")
def runtime_revision(project_id: str, revision_id: str, request: Request) -> dict:
    authorize_internal(request)
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
        from telemetry.runtime import project_runtime_by_revision
        return project_runtime_by_revision(project_id, revision_id, get_registry())
    except ValueError as exc:
        raise HTTPException(422, "Invalid indexed revision identity") from exc
    except KeyError as exc:
        raise HTTPException(404, "Indexed revision was not found") from exc


@runtime_router.get("/settings")
def runtime_settings(request: Request) -> dict:
    authorize_internal(request)
    from telemetry.runtime import telemetry_settings
    return telemetry_settings(get_registry())


@runtime_router.put("/settings")
async def update_runtime_settings(request: Request) -> dict:
    authorize_internal(request)
    try:
        payload = await request.json()
        from telemetry.runtime import telemetry_settings, update_runtime_sampling_policy
        update_runtime_sampling_policy(payload, get_registry())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        from telemetry.policy import refresh_sampling_policy
        policy = await run_in_threadpool(refresh_sampling_policy, get_registry())
    except Exception:
        policy = {"pending": True}
    return {**telemetry_settings(get_registry()), "policy_refresh": policy}


@runtime_router.get("/events")
async def runtime_update_events(request: Request):
    authorize_internal(request)
    from telemetry.events import runtime_events
    return StreamingResponse(
        runtime_events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def check_request(request: Request, project_id: str, commit_sha: str) -> None:
    authorize(request, project_id)
    try:
        validate_scope(project_id, commit_sha)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{project_id}/snapshots/{commit_sha}")
def status(project_id: str, commit_sha: str, request: Request) -> dict:
    check_request(request, project_id, commit_sha)
    return get_registry().get(project_id, commit_sha)


@router.put("/{project_id}/snapshots/{commit_sha}/source")
async def upload(project_id: str, commit_sha: str, request: Request) -> dict:
    check_request(request, project_id, commit_sha)
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/zip":
        raise HTTPException(415, "Send repository-root ZIP bytes as application/zip")
    data = bytearray()
    async for block in request.stream():
        if len(data) + len(block) > MAX_ARCHIVE_BYTES:
            raise HTTPException(413, "Source archive exceeds limit")
        data.extend(block)
    service = SourceService(get_registry(), get_storage())
    try:
        result = await run_in_threadpool(service.upload, project_id, commit_sha, bytes(data))
    except SnapshotConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return result


def _host_scopes() -> dict:
    try:
        value = json.loads(os.environ.get("TELEMETRY_HOST_SCOPES", "{}"))
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _host_identity(request: Request) -> tuple[str, dict]:
    # Ingress must remove this header from client requests and set it only from
    # the verified mTLS peer certificate.
    fingerprint = request.headers.get("x-verified-client-cert-sha256", "").lower().replace(":", "")
    scopes = _host_scopes()
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint) or fingerprint not in scopes:
        raise HTTPException(401, "Verified host certificate required")
    configured = scopes[fingerprint]
    if not isinstance(configured, dict):
        raise HTTPException(403, "Host has no deployment scope")
    return fingerprint, configured


@host_router.post("/deployments")
async def report_deployments(request: Request) -> dict:
    host_id, scope = _host_identity(request)
    payload = bytearray()
    async for block in request.stream():
        if len(payload) + len(block) > MAX_HOST_REPORT_BYTES:
            raise HTTPException(413, "Deployment report exceeds limit")
        payload.extend(block)
    try:
        body = json.loads(payload)
        containers = body["containers"]
        inventory = body.get("inventory", False)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, "Invalid deployment report")
    if not isinstance(containers, list) or len(containers) > 2000 or not isinstance(inventory, bool):
        raise HTTPException(422, "Invalid deployment report")
    allowed = scope.get("projects", {})
    if not isinstance(allowed, dict):
        raise HTTPException(403, "Host has no deployment scope")
    clean = []
    for item in containers:
        if not isinstance(item, dict):
            raise HTTPException(422, "Invalid deployment record")
        # Deliberately project only the approved fields; never persist raw Docker labels.
        record = {name: item.get(name) for name in
                  ("container_id", "project_id", "environment", "service", "commit_sha", "image_id", "state")}
        try:
            if (not isinstance(record["container_id"], str) or not re.fullmatch(r"[0-9a-f]{12,64}", record["container_id"])
                    or not isinstance(record["image_id"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", record["image_id"])
                    or record["state"] not in {"running", "stopped"}):
                raise ValueError
            validate_scope(record["project_id"], record["commit_sha"])
            if not isinstance(record["environment"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", record["environment"]):
                raise ValueError
            if not isinstance(record["service"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", record["service"]):
                raise ValueError
        except (ValueError, TypeError):
            raise HTTPException(422, "Invalid deployment record")
        envs = allowed.get(record["project_id"], [])
        if not isinstance(envs, list) or not all(isinstance(value, str) for value in envs):
            raise HTTPException(403, "Host has no deployment scope")
        if record["environment"] not in envs:
            raise HTTPException(403, "Deployment is outside host scope")
        clean.append(record)
    event_id = body.get("event_id")
    registry = get_registry()
    if event_id is not None:
        if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", event_id):
            raise HTTPException(422, "Invalid event ID")
        if registry.has_event(host_id, event_id):
            return {"accepted": True, "duplicate": True}
    pairs = registry.report_deployments(host_id, clean, inventory=inventory)
    from telemetry.events import publish_runtime_update
    publish_runtime_update()
    queued = sum(registry.queue_if_deployable(project, commit) for project, commit in pairs)
    if event_id is not None:
        registry.mark_event(host_id, event_id)
    return {"accepted": True, "containers": len(clean), "queued": queued}
