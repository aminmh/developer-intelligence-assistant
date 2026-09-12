"""Database evaluator MVP HTTP lifecycle."""

from fastapi import APIRouter, HTTPException

from evaluator.service import evaluator_service

router = APIRouter(prefix="/evaluator", tags=["Database Evaluator"])


@router.post("/runs", summary="Start a database evaluator run")
def start_evaluator_run(body: dict):
    return {"success": True, **evaluator_service.start(body)}


@router.get("/runs/{run_id}", summary="Poll evaluator status")
def evaluator_status(run_id: str):
    state = evaluator_service.status(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Evaluator run not found")
    return state


@router.get("/runs/{run_id}/reports", summary="List normalized evaluator reports")
def evaluator_reports(run_id: str):
    if evaluator_service.status(run_id) is None:
        raise HTTPException(status_code=404, detail="Evaluator run not found")
    return {"run_id": run_id, "reports": evaluator_service.reports(run_id)}


@router.get("/runs/{run_id}/context", summary="View linked source and schema context")
def evaluator_context(run_id: str):
    if evaluator_service.status(run_id) is None:
        raise HTTPException(status_code=404, detail="Evaluator run not found")
    return {"run_id": run_id, **evaluator_service.context(run_id)}


@router.post("/runs/{run_id}/recommendation", summary="Request a review-only evaluator recommendation")
def evaluator_recommendation(run_id: str, body: dict | None = None):
    if evaluator_service.status(run_id) is None:
        raise HTTPException(status_code=404, detail="Evaluator run not found")
    return {"run_id": run_id, "recommendation": evaluator_service.recommendation(run_id, body or {})}
