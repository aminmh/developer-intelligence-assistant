"""Tests for pausing agent runs on side-effecting tools and resuming after a decision."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

import agent_tools.approvals as approvals
import agent_tools.runtime as runtime
from agent_tools.approvals import ApprovalError, ApprovalStore
from agent_tools.limits import ToolLimits
from agent_tools.registry import ToolRegistry
from agent_tools.types import EFFECT_READ_ONLY, EFFECT_SIDE_EFFECTING, USER_WORKFLOWS, ToolContext, ToolDefinition


class _Fake(BaseChatModel):
    responses: list

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])


@pytest.fixture(autouse=True)
def runs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "_checkpoint_conn", None)
    monkeypatch.setattr(runtime, "_overview_for", lambda context: "")
    yield tmp_path
    if approvals._checkpoint_conn is not None:
        approvals._checkpoint_conn.close()


executed: list = []

SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"],
          "additionalProperties": False}


def _ticket_tool():
    return ToolDefinition(name="create_ticket", description="Create a ticket.", input_schema=SCHEMA,
                          effect=EFFECT_SIDE_EFFECTING, source="managed", workflows=USER_WORKFLOWS,
                          handler=lambda args, ctx: executed.append(args) or {"ticket": 7})


def _search_tool():
    return ToolDefinition(name="lookup", description="Look up.", input_schema={"type": "object"},
                          effect=EFFECT_READ_ONLY, source="builtin", workflows=USER_WORKFLOWS,
                          handler=lambda args, ctx: {"found": True})


def _call(name="create_ticket", args=None, call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {"title": "Fix login"},
                                              "id": call_id, "type": "tool_call"}])


CTX = ToolContext(workflow="search", project_id="pizza")


def _registry(tools=None):
    return ToolRegistry(builtins=tools if tools is not None else [_ticket_tool(), _search_tool()], managed_loader=lambda: [])


def _start(model, owner="alice", tools=None):
    executed.clear()
    return list(runtime.stream_agent_answer("Open a ticket", "system", CTX, registry=_registry(tools),
                                            limits=ToolLimits(), model=model,
                                            run_meta={"owner": owner, "question": "q", "route": "search",
                                                      "extras": {"matches": [1]}}))


def test_side_effecting_call_pauses_before_running():
    events = _start(_Fake(responses=[_call()]))

    types = [e["type"] for e in events]
    assert types == ["tool_started", "approval_required", "agent_paused"]
    assert executed == []
    approval = ApprovalStore().get(events[1]["approval_id"])
    assert approval.status == "pending" and approval.owner == "alice" and approval.project_id == "pizza"
    assert approval.calls == [{"id": "c1", "name": "create_ticket", "arguments": {"title": "Fix login"}}]


def test_read_only_calls_never_pause():
    events = _start(_Fake(responses=[_call("lookup", {}), AIMessage(content="done")]))

    assert events[-1]["type"] == "agent_done" and events[-1]["answer"] == "done"
    assert not any(e["type"] in {"approval_required", "agent_paused"} for e in events)
    assert ApprovalStore().get("0" * 32) is None


def test_approve_runs_the_exact_call_once_after_a_restart():
    approval_id = _start(_Fake(responses=[_call()]))[1]["approval_id"]
    # "Restart": drop the process-wide checkpoint connection and rebuild everything.
    approvals._checkpoint_conn.close()
    approvals._checkpoint_conn = None

    decided = ApprovalStore().decide(approval_id, "approve", "alice")
    events = list(runtime.resume_agent_answer(decided, registry=_registry(), limits=ToolLimits(),
                                              model=_Fake(responses=[AIMessage(content="Ticket 7 created.")])))

    assert executed == [{"title": "Fix login"}]
    assert events[0] == {"type": "approval_decided", "approval_id": approval_id, "decision": "approved"}
    completed = next(e for e in events if e["type"] == "tool_completed")
    assert completed["status"] == "success"
    assert events[-1]["type"] == "agent_done" and "Ticket 7" in events[-1]["answer"]
    with pytest.raises(ApprovalError) as exc:
        ApprovalStore().decide(approval_id, "approve", "alice")
    assert exc.value.code == "already_decided"


def test_deny_never_runs_the_call():
    approval_id = _start(_Fake(responses=[_call()]))[1]["approval_id"]

    decided = ApprovalStore().decide(approval_id, "deny", "alice")
    events = list(runtime.resume_agent_answer(decided, registry=_registry(), limits=ToolLimits(),
                                              model=_Fake(responses=[AIMessage(content="Not created.")])))

    assert executed == []
    completed = next(e for e in events if e["type"] == "tool_completed")
    assert completed["status"] == "error" and completed["error"] == "denied"
    assert events[-1]["type"] == "agent_done"


def test_tool_disabled_after_the_pause_is_not_run_even_when_approved():
    approval_id = _start(_Fake(responses=[_call()]))[1]["approval_id"]

    decided = ApprovalStore().decide(approval_id, "approve", "alice")
    list(runtime.resume_agent_answer(decided, registry=_registry([_search_tool()]), limits=ToolLimits(),
                                     model=_Fake(responses=[AIMessage(content="Could not run it.")])))

    assert executed == []


def test_invalid_arguments_are_flagged_at_the_pause():
    events = _start(_Fake(responses=[_call(args={"wrong": 1})]))

    assert "invalid" in events[1]["calls"][0]


def test_only_the_owner_can_decide_and_only_while_pending(runs_dir):
    store = ApprovalStore()
    approval = store.create(run_id="run_x", owner="alice", project_id="pizza", calls=[], run={})

    for user, code in (("mallory", "forbidden"),):
        with pytest.raises(ApprovalError) as exc:
            store.decide(approval.approval_id, "approve", user)
        assert exc.value.code == code
    with pytest.raises(ApprovalError) as exc:
        store.decide(approval.approval_id, "maybe", "alice")
    assert exc.value.code == "invalid_decision"
    with pytest.raises(ApprovalError) as exc:
        store.decide("0" * 32, "approve", "alice")
    assert exc.value.code == "not_found"
    assert store.decide(approval.approval_id, "deny", "alice").status == "denied"


def test_expired_approvals_are_rejected(monkeypatch):
    store = ApprovalStore()
    monkeypatch.setattr(approvals, "APPROVAL_TTL", timedelta(seconds=-1))
    approval = store.create(run_id="run_y", owner=None, project_id=None, calls=[], run={})

    with pytest.raises(ApprovalError) as exc:
        store.decide(approval.approval_id, "approve", None)
    assert exc.value.code == "expired"


def test_answer_stream_turns_a_pause_into_a_done_event_with_the_approval(monkeypatch):
    import rag.answer as answer

    monkeypatch.setattr(answer, "validate_answer", lambda text, q, r: (text, []))
    import runtime.llm as llm
    monkeypatch.setattr(llm, "remote_endpoint", lambda: ("u", "k", "remote-model"))
    original = runtime.stream_agent_answer

    def scripted(prompt, system, context, **kwargs):
        return original(prompt, system, context, registry=_registry(), limits=ToolLimits(),
                        model=_Fake(responses=[_call()]), run_meta=kwargs.get("run_meta"))

    monkeypatch.setattr(runtime, "stream_agent_answer", scripted)
    events = list(answer.generate_answer_stream("Open a ticket", use_remote=True, tool_context=CTX,
                                                run_meta={"owner": "alice", "extras": {"matches": ["m"]}}))

    done = events[-1]
    assert done["type"] == "done" and done["paused"] is True
    assert done["approval"]["calls"][0]["name"] == "create_ticket"
    assert any(e["type"] == "approval_required" for e in events)

    assert ApprovalStore().get(done["approval"]["approval_id"]).owner == "alice"


def test_resume_stream_finishes_with_the_original_extras(monkeypatch):
    import rag.answer as answer

    monkeypatch.setattr(answer, "validate_answer", lambda text, q, r: (text, []))
    approval_id = _start(_Fake(responses=[_call()]))[1]["approval_id"]
    decided = ApprovalStore().decide(approval_id, "approve", "alice")
    original = runtime.resume_agent_answer
    monkeypatch.setattr(runtime, "resume_agent_answer",
                        lambda a, **k: original(a, registry=_registry(), limits=ToolLimits(),
                                                model=_Fake(responses=[AIMessage(content="Ticket created.")])))

    events = list(answer.resume_answer_stream(decided))

    assert events[0]["type"] == "start"
    done = events[-1]
    assert done["type"] == "done" and done["answer"] == "Ticket created." and done["matches"] == [1]
    assert done["tool_calls"][-1]["status"] == "success"


def test_decision_endpoint(monkeypatch):
    import routers.tool_approvals as route_module

    monkeypatch.setattr(route_module.settings._env, "internal_secret", "s3cret")
    app = FastAPI()
    app.include_router(route_module.router)
    client = TestClient(app)
    store = ApprovalStore()
    approval = store.create(run_id="run_z", owner="alice", project_id="pizza",
                            calls=[{"id": "c1", "name": "create_ticket", "arguments": {}}], run={})
    path = f"/tool-approvals/{approval.approval_id}"
    import rag.answer as answer
    monkeypatch.setattr(answer, "resume_answer_stream", lambda a: iter([{"type": "done", "answer": a.status}]))

    assert client.post(path, json={"decision": "approve"}).status_code == 401
    headers = {"X-Internal-Secret": "s3cret", "X-User-Id": "mallory"}
    assert client.post(path, json={"decision": "approve"}, headers=headers).status_code == 403
    info = client.get(path, headers={"X-Internal-Secret": "s3cret"}).json()
    assert info["project_id"] == "pizza" and info["status"] == "pending"

    headers["X-User-Id"] = "alice"
    response = client.post(path, json={"decision": "approve"}, headers=headers)
    assert response.status_code == 200
    assert json.loads(response.text.strip()[len("data: "):]) == {"type": "done", "answer": "approved"}
    assert client.post(path, json={"decision": "deny"}, headers=headers).status_code == 409
    assert client.get("/tool-approvals/" + "0" * 32, headers={"X-Internal-Secret": "s3cret"}).status_code == 404
