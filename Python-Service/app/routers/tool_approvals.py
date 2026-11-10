"""Approve or deny a paused agent tool call and stream the continued answer.

Called only by the web server, which authenticates the user and checks project
permissions; the internal secret keeps other callers out, and ``X-User-Id``
identifies the user so only the person who started the chat can decide.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

from infra.settings import settings

router = APIRouter(prefix="/tool-approvals", tags=["LLM Tools"])

_STATUS = {"not_found": 404, "forbidden": 403, "already_decided": 409, "expired": 410, "invalid_decision": 400}


def _authorize(secret: str | None) -> None:
    if not settings.internal_secret or secret != settings.internal_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/{approval_id}", summary="Read a pending tool approval")
def read_approval(
    approval_id: str,
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
) -> dict[str, Any]:
    from agent_tools.approvals import ApprovalStore

    _authorize(x_internal_secret)
    approval = ApprovalStore().get(approval_id)
    if approval is None:
        raise HTTPException(404, "This approval does not exist.")
    return {"approval_id": approval.approval_id, "status": approval.status, "owner": approval.owner,
            "project_id": approval.project_id, "calls": approval.calls, "expires_at": approval.expires_at}


@router.post("/{approval_id}", summary="Approve or deny a paused tool call and continue the answer (SSE)")
def decide_approval(
    approval_id: str,
    body: dict,
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> StreamingResponse:
    from agent_tools.approvals import ApprovalError, ApprovalStore
    from rag.answer import resume_answer_stream

    _authorize(x_internal_secret)
    try:
        approval = ApprovalStore().decide(approval_id, str(body.get("decision") or ""), x_user_id)
    except ApprovalError as exc:
        raise HTTPException(_STATUS.get(exc.code, 400), exc.message) from exc

    def frames():
        for event in resume_answer_stream(approval):
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(frames(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
