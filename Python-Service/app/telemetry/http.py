"""Project-scoped CI API. Disabled unless dedicated credentials are configured."""
from __future__ import annotations

import hmac
import json
import os

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from telemetry.registry import SnapshotConflict, SnapshotRegistry, default_registry
from telemetry.snapshot import validate_scope
from telemetry.source import MAX_ARCHIVE_BYTES, SourceService

router = APIRouter(prefix="/telemetry/projects", tags=["Telemetry snapshots"])


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
    from index.storage import storage
    service = SourceService(get_registry(), storage)
    try:
        result = await run_in_threadpool(service.upload, project_id, commit_sha, bytes(data))
    except SnapshotConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if result["state"] == "failed":
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content=result)
    return result
