"""Developer log viewer HTTP endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query

from infra.log_viewer import clear_logs, list_logs
from infra.settings import settings
from infra.settings_store import settings_store

router = APIRouter(prefix="/logs", tags=["Developer Logs"])


def _authorize(secret: str | None) -> None:
    if secret != settings.internal_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("", summary="List developer logs")
def developer_logs(
    search: str = "",
    types: str = "",
    severity: str = "",
    start: str = "",
    end: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
):
    _authorize(x_internal_secret)
    selected = [item.strip() for item in types.split(",") if item.strip()] or None
    try:
        return list_logs(
            search=search,
            types=selected,
            severity=severity,
            start=start,
            end=end,
            offset=offset,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{log_type}", summary="Clear one developer log")
def clear_developer_log(
    log_type: str,
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
):
    _authorize(x_internal_secret)
    try:
        return clear_logs(log_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("", summary="Clear all developer logs")
def clear_all_developer_logs(
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
):
    _authorize(x_internal_secret)
    return clear_logs()


@router.get("/config", summary="Get log viewer configuration")
def get_log_config(
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
):
    _authorize(x_internal_secret)
    return {"deletion_mode": settings.log_deletion_mode}


@router.put("/config", summary="Update log viewer configuration")
def update_log_config(
    body: dict,
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
):
    _authorize(x_internal_secret)
    mode = body.get("deletion_mode") if isinstance(body, dict) else None
    if mode not in {"soft", "hard"}:
        raise HTTPException(status_code=400, detail="deletion_mode must be soft or hard")
    settings_store.set("logDeletionMode", mode)
    return {"deletion_mode": mode}

