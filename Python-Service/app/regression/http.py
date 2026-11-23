"""HTTP routes for regression checks (@.archcore/regression/regression-check.spec.md, Surface).

The web server authorizes the project before proxying here; these routes also require the
internal secret and user identity, like the other Code Quality routes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from regression.model import verdict_counts
from regression.service import RegressionBusy, RegressionRequestError, RegressionUnavailable, start_static
from regression.store import CheckNotFound, RegressionStore

router = APIRouter(tags=["Regression Check"])
_cleaned = False


def _store() -> RegressionStore:
    global _cleaned
    store = RegressionStore()
    if not _cleaned:  # once per process: a restart leaves no live thread behind a running check
        store.fail_interrupted()
        _cleaned = True
    return store


def _authorize(request: Request, project_id: str) -> str:
    from code_quality.http import _authorize as authorize_internal
    from code_quality.http import _performance_project

    user_id = authorize_internal(request)
    _performance_project(project_id)
    return user_id


def _summary(check: dict[str, Any]) -> dict[str, Any]:
    report = check["report"]
    return {key: check[key] for key in ("check_id", "project_id", "base_revision_id", "candidate_revision_id",
                                        "created_at", "updated_at", "state", "error")} | {
        "tier": report.get("tier"), "counts": verdict_counts(report), "uncovered": len(report.get("uncovered", []))}


@router.post("/projects/{project_id}/regression-checks", status_code=202,
             summary="Start a static regression check between a base and a candidate revision")
async def create_regression_check(project_id: str, request: Request) -> dict[str, Any]:
    user_id = _authorize(request, project_id)
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(422, "The request must be JSON.") from exc
    if not isinstance(body, dict):
        raise HTTPException(422, "The request must be a JSON object.")
    try:
        check = await run_in_threadpool(start_static, project_id, body.get("base_revision_id"),
                                        body.get("candidate_revision_id"), created_by=user_id, store=_store())
    except RegressionRequestError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RegressionUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    except RegressionBusy as exc:
        raise HTTPException(409, {"detail": str(exc), "active_check_id": exc.check_id}) from exc
    return _summary(check)


@router.get("/projects/{project_id}/regression-checks", summary="List regression checks of a project")
def list_regression_checks(project_id: str, request: Request) -> dict[str, Any]:
    _authorize(request, project_id)
    return {"checks": [_summary(check) for check in _store().list(project_id)]}


@router.get("/projects/{project_id}/regression-checks/{check_id}", summary="Read one regression check and its report")
def get_regression_check(project_id: str, check_id: str, request: Request) -> dict[str, Any]:
    _authorize(request, project_id)
    try:
        check = _store().get(project_id, check_id)
    except CheckNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return {**_summary(check), "report": check["report"]}


@router.delete("/projects/{project_id}/regression-checks/{check_id}", status_code=204, response_class=Response,
               summary="Delete one regression check")
def delete_regression_check(project_id: str, check_id: str, request: Request) -> Response:
    _authorize(request, project_id)
    try:
        _store().delete(project_id, check_id)
    except CheckNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(status_code=204)
