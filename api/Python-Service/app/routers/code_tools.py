"""Source preview for `path:line` citations in chat answers.

Reads through the same source resolution and checks as the agent's
``code_read`` tool, so the chat can show exactly what the model could see:
project-relative paths only, no dependency or build paths, no credential files.
The web server authorizes the upload before proxying here.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()

_CONTEXT_LINES = 20
_STATUS = {"invalid_path": 400, "excluded_path": 403, "not_found": 404, "no_source": 404,
           "binary_file": 415, "too_large": 413}


@router.post("/code-tools/read", tags=["Code Intelligence"], summary="Read lines around a cited source line")
def read_citation(body: dict) -> dict[str, Any]:
    from agent_tools.navigation import read_lines
    from agent_tools.runtime import latest_workspace
    from agent_tools.types import ToolCallRejected, ToolContext

    path = body.get("path")
    try:
        line = int(body.get("line") or 1)
    except (TypeError, ValueError):
        raise HTTPException(400, "line must be a number") from None
    if not isinstance(path, str) or not path:
        raise HTTPException(400, "path is required")
    from rag.revision_source import RevisionUnavailable, resolve_revision

    try:
        body = resolve_revision(body)
    except RevisionUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    filename = str(body.get("filename") or "")
    context = ToolContext(
        workflow="chat",
        project_id=body.get("project_id") or None,
        commit_sha=body.get("commit_sha") or None,
        revision_id=body.get("revision_id") or None,
        workspace_path=latest_workspace(filename),
        filename=filename,
    )
    start = max(1, line - _CONTEXT_LINES)
    try:
        result = read_lines(context, path, start, line + _CONTEXT_LINES)
    except ToolCallRejected as exc:
        raise HTTPException(_STATUS.get(exc.code, 400), exc.message) from exc
    return {"success": True, "highlight_line": line, **result}


@router.post("/code-tools/impact", tags=["Code Intelligence"],
             summary="Report what depends on changed symbols, a diff, or the change since another revision")
def read_impact(body: dict) -> dict[str, Any]:
    """Change impact over the revision's symbol graph (@.archcore/agent-tools/change-impact-analysis.spec.md).

    Body: ``project_id``, ``revision_id`` (or ``revision``: an id or ``latest``; default ``latest``) and
    exactly one of ``symbols``, ``diff``, ``base_revision_id``; optional ``depth`` and ``max_nodes``.
    Returns the same report as the ``code_impact`` agent tool.
    """
    from inspection.impact_service import INPUT_KINDS, ImpactRequestError, ImpactUnavailable, run_impact

    revision_id = body.get("revision_id") or body.get("revision") or "latest"
    args = {key: body[key] for key in (*INPUT_KINDS, "depth", "max_nodes") if key in body}
    try:
        return run_impact(body.get("project_id"), str(revision_id), args)
    except ImpactRequestError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ImpactUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
