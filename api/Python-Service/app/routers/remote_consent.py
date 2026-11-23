"""Per-project settings: external-model source consent and revision retention.

Called only by the web server, which checks project-administrator access for
changes; the shared internal secret keeps other callers out.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException

from infra.remote_consent import InvalidProject, get_consent, set_consent
from infra.settings import settings
from telemetry.retention import InvalidRetention, get_policy, set_policy

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


@router.get("/{project_id}/retention", summary="Read the project's revision retention")
def read_retention(
    project_id: str,
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
) -> dict[str, Any]:
    _authorize(x_internal_secret)
    try:
        return get_policy(project_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/{project_id}/retention", summary="Change the project's revision retention")
def update_retention(
    project_id: str,
    body: dict,
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
) -> dict[str, Any]:
    _authorize(x_internal_secret)
    try:
        return set_policy(project_id, body)
    except (InvalidRetention, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{project_id}/remote-docs", summary="Read whether framework docs may come from public hosts")
def read_remote_docs(
    project_id: str,
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
) -> dict[str, Any]:
    """@.archcore/knowledge-layer/framework-knowledge-layer.spec.md: on by default."""
    from knowledge.settings import get_remote_docs

    _authorize(x_internal_secret)
    try:
        return get_remote_docs(project_id)
    except InvalidProject as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/{project_id}/remote-docs", summary="Turn remote framework documentation on or off")
def update_remote_docs(
    project_id: str,
    body: dict,
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
) -> dict[str, Any]:
    from knowledge.settings import set_remote_docs

    _authorize(x_internal_secret)
    try:
        return set_remote_docs(project_id, body.get("allowed"))
    except InvalidProject as exc:
        raise HTTPException(400, str(exc)) from exc
