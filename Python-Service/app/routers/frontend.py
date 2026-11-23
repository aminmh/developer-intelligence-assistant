"""HTTP API for front-end project understanding (UI map).

Called only by the web server, which checks the user's project access first.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException

from infra.settings import settings
from projects.frontend.service import UiMapUnavailable, checks_for_revision, ui_map_for_revision
from telemetry.snapshot import validate_project_id, validate_revision_id

router = APIRouter(prefix="/frontend", tags=["Front-end"])


@router.get("/projects/{project_id}/ui-map", summary="Routes, components, stores, and HTTP calls of a front-end project")
def read_ui_map(
    project_id: str,
    revision_id: str | None = None,
    app: str | None = None,
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
) -> dict[str, Any]:
    if not settings.internal_secret or x_internal_secret != settings.internal_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        validate_project_id(project_id)
        if revision_id:
            validate_revision_id(revision_id)
        return ui_map_for_revision(project_id, revision_id, app or None)
    except UiMapUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/projects/{project_id}/checks", summary="Accessibility, security, reliability, and performance checks of a front-end project")
def read_checks(
    project_id: str,
    revision_id: str | None = None,
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
) -> dict[str, Any]:
    if not settings.internal_secret or x_internal_secret != settings.internal_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        validate_project_id(project_id)
        if revision_id:
            validate_revision_id(revision_id)
        return checks_for_revision(project_id, revision_id)
    except UiMapUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
