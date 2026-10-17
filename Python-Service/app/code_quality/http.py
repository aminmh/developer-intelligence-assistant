"""Authenticated API surface for saved code quality assessments."""
from __future__ import annotations

import hmac
import json
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
from code_quality.performance import PerformanceError, PerformanceStore, compare_runs, run_benchmark
from telemetry.snapshot import validate_project_id, validate_revision_id

router = APIRouter(prefix="/code-quality", tags=["Code quality assessment"])
MAX_PERFORMANCE_REQUEST_BYTES = 100_000
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


@router.get("/performance/targets")
def list_performance_targets(request: Request) -> dict:
    user_id = _authorize(request)
    return {"targets": PerformanceStore().targets(user_id)}


@router.post("/performance/targets")
async def save_performance_target(request: Request) -> dict:
    user_id = _authorize(request)
    try:
        payload = await request.json()
        return {"target": PerformanceStore().save_target(user_id, payload)}
    except PerformanceError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "Performance target settings are invalid.") from exc


@router.get("/performance/targets/{target_id}")
def get_performance_target(target_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    try:
        target_id = str(__import__("uuid").UUID(target_id))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(422, "Invalid performance target identity.") from exc
    target = PerformanceStore().get_target(user_id, target_id)
    if target is None:
        raise HTTPException(404, "Performance target was not found.")
    return {"target": target}


@router.delete("/performance/targets/{target_id}")
def delete_performance_target(target_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    try:
        target_id = str(__import__("uuid").UUID(target_id))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(422, "Invalid performance target identity.") from exc
    if not PerformanceStore().delete_target(user_id, target_id):
        raise HTTPException(404, "Performance target was not found.")
    return {"deleted": True}


@router.get("/projects/{project_id}/revisions/{revision_id}/performance/routes")
def performance_routes(project_id: str, revision_id: str, request: Request) -> dict:
    _authorize(request)
    try:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
        from telemetry.registry import default_registry
        if default_registry().get_by_revision_id(project_id, revision_id) is None:
            raise HTTPException(404, "Indexed revision was not found.")
        from telemetry.runtime import ClickHouseRuntime
        sampled = ClickHouseRuntime().requests(project_id, revision_id)
        routes: dict[str, dict] = {}
        for item in sampled.get("requests", []):
            if item.get("method", "").upper() != "GET":
                continue
            route = item.get("route")
            if not isinstance(route, str) or not route.startswith("/") or route.startswith("//") or "?" in route:
                continue
            routes.setdefault(route, {"method": "GET", "route": route, "samples": 0})["samples"] += 1
        return {"available": bool(sampled.get("available")), "reason": sampled.get("reason"),
                "sampled": True, "routes": sorted(routes.values(), key=lambda item: (-item["samples"], item["route"]))}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(422, "Invalid project revision identity.") from exc
    except Exception as exc:
        raise HTTPException(503, "Sampled Runtime routes are unavailable.") from exc


@router.post("/projects/{project_id}/performance/runs")
async def create_performance_run(project_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    try:
        validate_project_id(project_id)
        raw = await request.body()
        if len(raw) > MAX_PERFORMANCE_REQUEST_BYTES:
            raise HTTPException(413, "Performance benchmark request exceeds the size limit.")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise PerformanceError("Performance benchmark settings are invalid.")
        target_id = str(__import__("uuid").UUID(payload.get("target_id", "")))
        store = PerformanceStore()
        target = store.get_target(user_id, target_id)
        if target is None:
            raise HTTPException(404, "Performance target was not found.")
        bearer_token = payload.get("bearer_token")
        credentials = payload.get("login_credentials")
        report = await run_in_threadpool(run_benchmark, target, payload.get("routes"), bearer_token, credentials)
        return store.save_run(user_id, project_id, target, report)
    except HTTPException:
        raise
    except PerformanceError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "Performance benchmark request is invalid.") from exc
    except Exception as exc:
        logger.error("code_quality_performance_failed project=%s error_type=%s", project_id, type(exc).__name__)
        raise HTTPException(503, "Performance benchmark could not be completed. Check Docker, the target, and authentication, then retry.") from exc


@router.get("/projects/{project_id}/performance/runs")
def list_performance_runs(project_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    try:
        validate_project_id(project_id)
    except ValueError as exc:
        raise HTTPException(422, "Invalid project identity.") from exc
    return {"runs": PerformanceStore().history(user_id, project_id)}


@router.get("/projects/{project_id}/performance/runs/{run_id}")
def get_performance_run(project_id: str, run_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    try:
        validate_project_id(project_id)
        run_id = str(__import__("uuid").UUID(run_id))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(422, "Invalid performance run identity.") from exc
    result = PerformanceStore().get_run(user_id, project_id, run_id)
    if result is None:
        raise HTTPException(404, "Performance run was not found.")
    return result


@router.post("/projects/{project_id}/performance/compare")
async def compare_performance_runs(project_id: str, request: Request) -> dict:
    user_id = _authorize(request)
    try:
        validate_project_id(project_id)
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError
        store = PerformanceStore()
        left = store.get_run(user_id, project_id, str(__import__("uuid").UUID(payload.get("left_run_id", ""))))
        right = store.get_run(user_id, project_id, str(__import__("uuid").UUID(payload.get("right_run_id", ""))))
        if not left or not right:
            raise HTTPException(404, "One or both performance runs were not found.")
        return {"comparison": compare_runs(left, right), "runs": [left, right]}
    except HTTPException:
        raise
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, "Select two valid performance runs.") from exc


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
