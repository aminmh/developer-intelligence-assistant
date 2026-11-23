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
        _recordings().purge()
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
    _recordings().delete_check(project_id, check_id)
    return Response(status_code=204)


# --- replay (spec clauses 10-18) -----------------------------------------------------------------------

def _recordings() -> Any:
    from regression.recordings import Recordings

    return Recordings()


def _replay_call(action: Any) -> Any:
    from regression.replay_service import ProbeError, ReplayError

    try:
        return action()
    except CheckNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ProbeError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ReplayError as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


async def _body(request: Request, limit: int = 1024 * 1024) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > limit:
        raise HTTPException(413, "The request is too large.")
    try:
        import json

        body = json.loads(raw or b"{}")
    except ValueError as exc:
        raise HTTPException(422, "The request must be JSON.") from exc
    if not isinstance(body, dict):
        raise HTTPException(422, "The request must be a JSON object.")
    return body


@router.get("/projects/{project_id}/regression-checks/{check_id}/replay",
            summary="Probes, replay settings and replay runs of a check")
def get_replay(project_id: str, check_id: str, request: Request) -> dict[str, Any]:
    from regression import replay_service

    _authorize(request, project_id)
    store = _store()
    return _replay_call(lambda: (store.get(project_id, check_id), replay_service.state(store, project_id, check_id))[1])


@router.put("/projects/{project_id}/regression-checks/{check_id}/probes", summary="Save the edited probe set")
async def put_probes(project_id: str, check_id: str, request: Request) -> dict[str, Any]:
    from regression.probes import validate_probes

    _authorize(request, project_id)
    body = await _body(request)
    store = _store()
    return {"probes": _replay_call(lambda: store.set_probes(project_id, check_id, validate_probes(body.get("probes"))))}


@router.post("/projects/{project_id}/regression-checks/{check_id}/probes/generate",
             summary="Propose probes for the report's routes")
async def generate_probes(project_id: str, check_id: str, request: Request) -> dict[str, Any]:
    from regression import replay_service

    _authorize(request, project_id)
    store = _store()
    return await run_in_threadpool(_replay_call, lambda: replay_service.generate(store, project_id, check_id))


@router.post("/projects/{project_id}/regression-checks/{check_id}/probes/import",
             summary="Import a HAR file or request list into the probe set")
async def import_probes(project_id: str, check_id: str, request: Request) -> dict[str, Any]:
    from regression import replay_service

    user_id = _authorize(request, project_id)
    body = await _body(request, limit=21 * 1024 * 1024)
    store = _store()

    def action() -> Any:
        store.get(project_id, check_id)
        target = replay_service.target_for(user_id, project_id, body.get("target_id"))
        return replay_service.import_into(store, project_id, check_id, body.get("text"), target,
                                          "replace" if body.get("mode") == "replace" else "append")

    return await run_in_threadpool(_replay_call, action)


@router.put("/projects/{project_id}/regression-checks/{check_id}/replay-settings",
            summary="Save replay targets, the disposable flag, reset path and ignore rules")
async def put_replay_settings(project_id: str, check_id: str, request: Request) -> dict[str, Any]:
    from regression import replay_service

    _authorize(request, project_id)
    body = await _body(request)
    store = _store()
    return {"settings": _replay_call(lambda: replay_service.save_settings(store, project_id, check_id, body))}


@router.post("/projects/{project_id}/regression-checks/{check_id}/replay-runs", status_code=202,
             summary="Record a baseline or replay the candidate in the background")
async def start_replay_run(project_id: str, check_id: str, request: Request) -> dict[str, Any]:
    from regression import replay_service

    user_id = _authorize(request, project_id)
    body = await _body(request)
    store = _store()
    return await run_in_threadpool(_replay_call, lambda: replay_service.run(store, user_id, project_id, check_id, body))


@router.get("/projects/{project_id}/regression-checks/{check_id}/replay-runs/{run_id}", summary="Read one replay run")
def get_replay_run(project_id: str, check_id: str, run_id: str, request: Request) -> dict[str, Any]:
    _authorize(request, project_id)
    run = _replay_call(lambda: _store().get_run(project_id, run_id))
    if run["check_id"] != check_id:
        raise HTTPException(404, "Replay run was not found for this check.")
    return run


# --- characterization tests (spec clauses 19-21) -------------------------------------------------------

@router.post("/projects/{project_id}/regression-checks/{check_id}/tests",
             summary="Characterization tests and a replayable request file for the check")
async def build_tests(project_id: str, check_id: str, request: Request) -> Any:
    from regression import replay_service
    from regression.characterize import bundle_zip

    _authorize(request, project_id)
    store = _store()
    bundle = await run_in_threadpool(_replay_call, lambda: replay_service.test_bundle(store, project_id, check_id))
    if request.query_params.get("format") == "zip":
        return Response(bundle_zip(bundle), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="regression-tests-{check_id}.zip"'})
    return bundle
