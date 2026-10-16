"""Authenticated API surface for saved code quality assessments."""
from __future__ import annotations

import hmac
import logging
import os

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from code_quality.service import (
    AssessmentError,
    AssessmentModelUnavailable,
    AssessmentReportStoreUnavailable,
    ReportStore,
    assess_revision,
)
from telemetry.snapshot import validate_project_id, validate_revision_id

router = APIRouter(prefix="/code-quality", tags=["Code quality assessment"])
logger = logging.getLogger(__name__)


def _authorize(request: Request) -> str:
    expected = os.environ.get("RAG_CALLBACK_SECRET", "")
    supplied = request.headers.get("x-internal-secret", "")
    if (not expected or len(expected) < 32
            or not hmac.compare_digest(supplied.encode(), expected.encode())):
        raise HTTPException(401, "Internal service credentials required")
    user_id = request.headers.get("x-user-id", "")
    if not user_id or len(user_id) > 512:
        raise HTTPException(401, "Authenticated user identity required")
    return user_id


@router.get("/preferences")
def get_preferences(request: Request) -> dict:
    user_id = _authorize(request)
    return {"weights": ReportStore().weights(user_id)}


@router.put("/preferences")
async def put_preferences(request: Request) -> dict:
    user_id = _authorize(request)
    try:
        payload = await request.json()
        weights = ReportStore().save_weights(user_id, payload.get("weights") if isinstance(payload, dict) else None)
        return {"weights": weights}
    except AssessmentError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "Invalid assessment preferences.") from exc


@router.get("/projects/{project_id}/reports")
def list_reports(project_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc
    return {"reports": ReportStore().history(project_id)}


@router.get("/projects/{project_id}/reports/{report_id}")
def get_report(project_id: str, report_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc
    report = ReportStore().get_report(project_id, report_id)
    if report is None:
        raise HTTPException(404, "Assessment report was not found.")
    return report


@router.post("/projects/{project_id}/revisions/{revision_id}/assess")
async def assess(project_id: str, revision_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
        from telemetry.registry import default_registry
        registry = default_registry()
        snapshot = registry.get_by_revision_id(project_id, revision_id)
        if snapshot is None or snapshot.get("state") != "ready":
            raise HTTPException(404, "The selected indexed revision is unavailable.")
        archive_key = registry.archive_key(project_id, snapshot["commit_sha"])
        if not archive_key:
            raise HTTPException(409, "The source archive for this revision is no longer available for assessment.")
        from index.storage import storage
        try:
            archive = await run_in_threadpool(storage.get_snapshot_archive, archive_key)
        except Exception as exc:
            logger.warning(
                "code_quality_source_archive_unavailable project=%s revision=%s error_type=%s",
                project_id,
                revision_id,
                type(exc).__name__,
            )
            raise HTTPException(
                503,
                "The selected revision's source archive could not be retrieved. Retry or re-index the project.",
            ) from exc
        return await run_in_threadpool(assess_revision, project_id, revision_id, snapshot, archive, user_id)
    except HTTPException:
        raise
    except AssessmentError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, "Invalid indexed revision identity.") from exc
    except AssessmentModelUnavailable as exc:
        logger.warning(
            "code_quality_model_unavailable project=%s revision=%s error_type=%s",
            project_id,
            revision_id,
            type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__,
        )
        raise HTTPException(
            503,
            "The configured model could not complete scoring. Check its endpoint, model name, and availability, then retry.",
        ) from exc
    except AssessmentReportStoreUnavailable as exc:
        logger.error(
            "code_quality_report_store_unavailable project=%s revision=%s error_type=%s",
            project_id,
            revision_id,
            type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__,
        )
        raise HTTPException(
            503,
            "The assessment completed but its report could not be saved. Check API storage and retry.",
        ) from exc
    except Exception as exc:
        logger.error(
            "code_quality_unexpected_failure project=%s revision=%s error_type=%s",
            project_id,
            revision_id,
            type(exc).__name__,
        )
        raise HTTPException(
            503,
            "The assessment failed during internal processing. Check API logs and retry.",
        ) from exc
