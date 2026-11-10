"""Per-project setting: may project source go to the external model?

Called only by the web server, which checks project-administrator access for
changes; the shared internal secret keeps other callers out.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException

from infra.remote_consent import InvalidProject, get_consent, set_consent
from infra.settings import settings

router = APIRouter(prefix="/projects", tags=["Project settings"])


def _authorize(secret: str | None) -> None:
    if not settings.internal_secret or secret != settings.internal_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/{project_id}/remote-source", summary="Read external-model source consent")
def read_remote_source(
    project_id: str,
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
) -> dict[str, Any]:
    _authorize(x_internal_secret)
    try:
        return get_consent(project_id)
    except InvalidProject as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/{project_id}/remote-source", summary="Change external-model source consent")
def update_remote_source(
    project_id: str,
    body: dict,
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
) -> dict[str, Any]:
    _authorize(x_internal_secret)
    try:
        return set_consent(project_id, body.get("allowed"))
    except InvalidProject as exc:
        raise HTTPException(400, str(exc)) from exc
