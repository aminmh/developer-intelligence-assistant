"""HTTP endpoints for starting and polling embedding rebuilds."""

from __future__ import annotations

import re
import threading
import uuid

from fastapi import APIRouter, HTTPException

from infra.settings import settings

router = APIRouter(tags=["Embeddings"])
_feedback_lock = threading.Lock()


def _run_route_feedback_rebuild(job_id: str) -> None:
    from index.embedding_rebuild import write_status
    from rag.router import rebuild_feedback_collection

    try:
        def report(stage: str, indexed: int, total: int) -> None:
            write_status(job_id, status="running", stage=stage, total=total, indexed=indexed)

        result = rebuild_feedback_collection(progress=report)
        write_status(
            job_id, status="done", stage=result.get("stage", "complete"), total=result["total"],
            indexed=result["indexed"], collection_name=result["collection_name"],
        )
    except Exception as exc:  # noqa: BLE001 — status carries a retryable failure
        write_status(job_id, status="error", stage="error", error=str(exc)[:300])
    finally:
        _feedback_lock.release()


@router.post("/api/embeddings/rebuild")
def rebuild_project_embeddings(body: dict) -> dict:
    if settings.embedding_provider != "cohere":
        raise HTTPException(409, "Select Cohere as the embedding provider before rebuilding")
    filename = body.get("filename")
    session_id = body.get("upload_session_id")
    project_id = body.get("project_id")
    commit_sha = body.get("commit_sha")
    if not all(isinstance(value, str) and value for value in (filename, session_id, project_id)):
        raise HTTPException(400, "filename, upload_session_id, and project_id are required")

    from index.storage import storage
    from telemetry.registry import default_registry
    from telemetry.snapshot import Snapshot, validate_project_id, validate_scope

    try:
        validate_project_id(project_id)
        if commit_sha is not None:
            validate_scope(project_id, commit_sha)
    except ValueError as exc:
        raise HTTPException(422, "Invalid project revision") from exc
    match = storage.find_rag_file(filename, session_id)
    if (not match or match.get("project_id") != project_id or match.get("commit_sha") != commit_sha
            or (body.get("revision_id") and body["revision_id"] != match.get("revision_id"))
            or (body.get("source_ref") and body["source_ref"] != match.get("source_ref"))):
        raise HTTPException(403, "Selected upload does not match the requested project revision")
    checksum = match.get("archive_sha256")
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise HTTPException(409, "Project archive checksum is unavailable")

    snapshot = Snapshot(project_id, commit_sha, checksum)
    registry = default_registry()
    current = registry.get(project_id, snapshot.reference)
    if current.get("state") != "ready" or not registry.is_ready_collection(snapshot.collection):
        raise HTTPException(409, "Index the selected project revision with the default provider first")
    archive = storage.get_rag_file_bytes(filename, session_id)
    if archive is None:
        raise HTTPException(404, "Uploaded source archive is no longer available")

    job_id = str(uuid.uuid4())
    from index.embedding_rebuild import start_rebuild, write_status
    write_status(job_id, status="queued", stage="queued", collection_name=snapshot.collection)
    try:
        start_rebuild(
            job_id, filename, archive,
            {"project_id": project_id, "commit_sha": commit_sha,
             "source_archive_sha256": checksum, "revision_id": current.get("revision_id")},
            snapshot.collection,
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"success": True, "job_id": job_id, "status": "queued"}


@router.get("/api/embeddings/rebuild/{job_id}")
def embedding_rebuild_status(job_id: str) -> dict:
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(404, "Embedding job not found") from exc
    from index.embedding_rebuild import read_status
    status = read_status(job_id)
    if status is None:
        raise HTTPException(404, "Embedding job not found")
    return status


@router.post("/api/embeddings/rebuild-route-feedback")
def rebuild_route_feedback() -> dict:
    if settings.embedding_provider != "cohere":
        raise HTTPException(409, "Select Cohere as the embedding provider before rebuilding")
    if not _feedback_lock.acquire(blocking=False):
        raise HTTPException(409, "A route-feedback rebuild is already running")
    from index.embedding_rebuild import write_status

    job_id = str(uuid.uuid4())
    write_status(job_id, status="queued", stage="queued")
    threading.Thread(target=_run_route_feedback_rebuild, args=(job_id,), daemon=True).start()
    return {"success": True, "job_id": job_id, "status": "queued"}
