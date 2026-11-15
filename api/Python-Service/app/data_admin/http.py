"""Internal data-management API. Express authorizes the user; this layer requires the internal secret.

Express narrows every read to the caller's projects and allows writes for global administrators only.
"""
from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from data_admin import deletion, inventory
from data_admin.journal import Journal
from data_admin.preview import preview
from telemetry.http import authorize_internal

router = APIRouter(prefix="/data-admin", tags=["data-admin"], dependencies=[Depends(authorize_internal)])
_journal: Journal | None = None
_journal_lock = threading.Lock()


def journal() -> Journal:
    global _journal
    with _journal_lock:
        if _journal is None:
            _journal = Journal()
            _journal.fail_interrupted()
        return _journal


def _actor(request: Request) -> dict[str, str | None]:
    return {"sub": request.headers.get("x-user-id") or None, "email": request.headers.get("x-user-email") or None}


def _inventory(refresh: bool = False) -> dict[str, Any]:
    return inventory.snapshot(refresh=refresh)


@router.get("/summary")
def summary(refresh: bool = False) -> dict[str, Any]:
    return inventory.summarize(_inventory(refresh))


@router.get("/projects")
def projects(refresh: bool = False) -> dict[str, Any]:
    return {"projects": inventory.project_totals(_inventory(refresh))}


@router.get("/stores/{store}/items")
def store_items(store: str, project_id: str | None = None, owner: str | None = None, refresh: bool = False) -> dict[str, Any]:
    if store not in inventory.STORES:
        raise HTTPException(404, "Unknown store.")
    data = _inventory(refresh)["stores"][store]
    items = data["items"]
    if project_id:
        items = [item for item in items if item["project_id"] == project_id
                 or project_id in (item["detail"].get("projects") or {})]
    if owner:
        items = [item for item in items if item["owner"] == owner]
    return {"store": store, "status": data["status"], "error": data["error"], "items": items}


@router.get("/stores/{store}/preview")
def item_preview(store: str, id: str = Query(..., max_length=1024)) -> dict[str, Any]:  # noqa: A002 — public query name
    item = inventory.find_item(_inventory(), store, id)
    if item is None:
        raise HTTPException(404, "Item not found. Refresh the inventory.")
    try:
        return {"item": item, **preview(item)}
    except Exception as exc:  # noqa: BLE001 — a preview is best effort
        raise HTTPException(503, f"Preview unavailable: {str(exc)[:200]}") from exc


@router.get("/orphans")
def orphans(refresh: bool = False) -> dict[str, Any]:
    inv = _inventory(refresh)
    items = [item for store in inv["stores"].values() for item in store["items"]
             if item["owner"] in ("orphan", "legacy")]
    return {"generated_at": inv["generated_at"], "items": items}


@router.post("/deletions/plan")
def plan_deletion(request: Request, target: dict[str, Any] = Body(..., embed=True)) -> dict[str, Any]:
    try:
        result = deletion.plan(target)
    except deletion.PlanError as exc:
        raise HTTPException(400, str(exc)) from exc
    journal().audit(actor=_actor(request), action="plan", target=target, plan=result, result="planned")
    return result


@router.post("/deletions/execute")
def execute_deletion(request: Request, target: dict[str, Any] = Body(..., embed=True),
                     plan_hash: str = Body(..., embed=True, alias="hash")) -> dict[str, Any]:
    try:
        return deletion.start(target, plan_hash, _actor(request), journal=journal())
    except deletion.PlanError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (deletion.PlanChanged, deletion.JobRunning) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/jobs")
def jobs() -> dict[str, Any]:
    return {"jobs": journal().recent_jobs()}


@router.get("/jobs/{job_id}")
def job(job_id: str) -> dict[str, Any]:
    found = journal().job(job_id)
    if not found:
        raise HTTPException(404, "Job not found.")
    return found


@router.get("/audit")
def audit(project_ids: str | None = None, limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    scoped = None if project_ids is None else [p for p in project_ids.split(",") if p]
    return {"entries": journal().audit_entries(project_ids=scoped, limit=limit)}
