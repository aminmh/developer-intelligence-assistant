"""Tests for the agent around answer generation, using a fake tool-calling model."""

import json

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

import agent_tools.runtime as runtime
import rag.answer as answer
from agent_tools.limits import ToolLimits
from agent_tools.registry import ToolRegistry
from agent_tools.types import EFFECT_READ_ONLY, EFFECT_SIDE_EFFECTING, USER_WORKFLOWS, ToolContext, ToolDefinition


class _ToolCallingFake(BaseChatModel):
    """Fake chat model that accepts ``bind_tools`` and replays scripted messages whole."""

    responses: list

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])


def _tool(name="lookup", effect=EFFECT_READ_ONLY, handler=None):
    return ToolDefinition(
        name=name, description=f"{name} tool.",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"],
                      "additionalProperties": False},
        effect=effect, source="builtin", workflows=USER_WORKFLOWS,
        handler=handler or (lambda args, ctx: {"found": args["q"], "file": "app/Models/User.php"}),
    )


def _model(*messages):
    return _ToolCallingFake(responses=list(messages))


def _call(name, args, call_id="call_1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


CTX = ToolContext(workflow="search")


def _run(model, tools, limits=None):
    registry = ToolRegistry(builtins=tools, managed_loader=lambda: [])
    return list(runtime.stream_agent_answer("Where is User?", "system", CTX, registry=registry,
                                            limits=limits or ToolLimits(), model=model))


def test_agent_calls_a_tool_then_answers():
    seen = []
    tool = _tool(handler=lambda args, ctx: seen.append((args, ctx.workflow)) or {"file": "app/Models/User.php"})
    events = _run(_model(_call("lookup", {"q": "User"}), AIMessage(content="User is in app/Models/User.php.")), [tool])

    types = [e["type"] for e in events]
    assert types[0] == "tool_started" and "tool_completed" in types and types[-1] == "agent_done"
    assert seen == [({"q": "User"}, "search")]
    done = events[-1]
    assert "app/Models/User.php" in done["answer"]
    assert done["tool_calls"][0]["name"] == "lookup" and done["tool_calls"][0]["status"] == "success"
    assert done["truncated"] is False


def test_invalid_arguments_never_reach_the_tool():
    seen = []
    tool = _tool(handler=lambda args, ctx: seen.append(args))
    events = _run(_model(_call("lookup", {"wrong": 1}), AIMessage(content="Could not look it up.")), [tool])

    completed = next(e for e in events if e["type"] == "tool_completed")
    assert completed["status"] == "error" and completed["error"] == "invalid_arguments"
    assert seen == []


def test_unknown_tool_calls_are_rejected():
    events = _run(_model(_call("drop_database", {"q": "x"}), AIMessage(content="ok")), [_tool()])

    completed = [e for e in events if e["type"] == "tool_completed"]
    assert len(completed) == 1 and completed[0]["status"] == "error"
    assert events[-1]["type"] == "agent_done"


def test_tool_call_limit_stops_tool_execution():
    calls = [_call("lookup", {"q": str(i)}, call_id=f"c{i}") for i in range(4)]
    events = _run(_model(*calls, AIMessage(content="partial answer")), [_tool()], limits=ToolLimits(max_calls_per_run=2))

    statuses = [e["error"] for e in events if e["type"] == "tool_completed"]
    assert statuses[:2] == [None, None]
    assert "tool_call_limit" in statuses


def test_agent_failure_before_output_is_reported_as_unavailable():
    class Broken(_ToolCallingFake):
        def _generate(self, *args, **kwargs):
            raise RuntimeError("upstream 500")

    with pytest.raises(runtime.AgentUnavailable):
        _run(Broken(responses=[]), [_tool()])


def test_agent_workflow_requires_remote_probe(monkeypatch):
    import runtime.llm as llm

    monkeypatch.setattr(llm, "remote_configured", lambda: True)
    monkeypatch.setattr(llm, "remote_tool_calling_status", lambda: {"supported": True})
    assert runtime.agent_workflow("search", use_remote=True) == "search"
    assert runtime.agent_workflow("search", use_remote=False) is None
    assert runtime.agent_workflow("unknown_route", use_remote=True) is None

    monkeypatch.setattr(llm, "remote_tool_calling_status", lambda: {"supported": False})
    assert runtime.agent_workflow("search", use_remote=True) is None
    monkeypatch.setattr(llm, "remote_tool_calling_status", lambda: None)
    assert runtime.agent_workflow("search", use_remote=True) is None


def test_stream_falls_back_to_plain_generation_when_agent_cannot_start(monkeypatch):
    def unavailable(*args, **kwargs):
        raise runtime.AgentUnavailable("no tools")
        yield  # pragma: no cover

    monkeypatch.setattr(runtime, "stream_agent_answer", unavailable)
    monkeypatch.setattr(answer, "stream_complete_with_model",
                        lambda *a, **k: iter([("plain", "m", "remote")]))
    monkeypatch.setattr(answer, "validate_answer", lambda text, q, r: (text, []))

    events = list(answer.generate_answer_stream("q", use_remote=True, tool_context=CTX))

    assert events[-1]["type"] == "done" and events[-1]["answer"] == "plain"


def test_stream_emits_tool_events_and_done(monkeypatch):
    def fake_agent(*args, **kwargs):
        yield {"type": "tool_started", "id": "c1", "name": "lookup", "arguments": {"q": "x"}}
        yield {"type": "tool_completed", "id": "c1", "name": "lookup", "status": "success"}
        yield {"type": "delta", "text": "answer"}
        yield {"type": "agent_done", "answer": "answer", "model": "m", "provider": "remote",
               "tool_calls": [{"name": "lookup", "status": "success"}], "usage": None, "truncated": False}

    monkeypatch.setattr(runtime, "stream_agent_answer", fake_agent)
    monkeypatch.setattr(answer, "validate_answer", lambda text, q, r: (text, []))
    import runtime.llm as llm
    monkeypatch.setattr(llm, "remote_endpoint", lambda: ("u", "k", "remote-model"))

    events = list(answer.generate_answer_stream("q", use_remote=True, tool_context=CTX))

    assert [e["type"] for e in events] == ["start", "tool_started", "tool_completed", "delta", "validation", "done"]
    assert events[0]["model"] == "remote-model"
    assert events[-1]["tool_calls"] == [{"name": "lookup", "status": "success"}]
    json.dumps(events)  # every event is SSE-serializable


def test_non_stream_answer_returns_tool_calls(monkeypatch):
    def fake_agent(*args, **kwargs):
        yield {"type": "tool_started", "id": "c1", "name": "lookup", "arguments": {}}
        yield {"type": "agent_done", "answer": "done", "model": "m", "provider": "remote",
               "tool_calls": [{"name": "lookup", "status": "success"}], "usage": None, "truncated": False}

    monkeypatch.setattr(runtime, "stream_agent_answer", fake_agent)
    monkeypatch.setattr(answer, "validate_answer", lambda text, q, r: (text, []))

    result = answer.generate_answer("q", use_remote=True, tool_context=CTX)

    assert result["answer"] == "done" and result["tool_calls"][0]["name"] == "lookup"
    assert result["usage"]["source"] == "estimate"


def test_tool_status_explains_why_tools_are_off(monkeypatch):
    import runtime.llm as llm

    monkeypatch.setattr(llm, "remote_configured", lambda: True)
    monkeypatch.setattr(llm, "remote_tool_calling_status", lambda: None)
    assert runtime.tool_status("search", True)["code"] == "untested"

    monkeypatch.setattr(llm, "remote_tool_calling_status", lambda: {"supported": False, "detail": "text reply"})
    status = runtime.tool_status("search", True)
    assert status["available"] is False and "text reply" in status["reason"]

    monkeypatch.setattr(llm, "remote_tool_calling_status", lambda: {"supported": True})
    assert runtime.tool_status("search", True) == {"available": True, "code": "available", "reason": None}
    assert runtime.tool_status("search", False)["code"] == "no_remote"
    assert runtime.tool_status("structure_index", True)["code"] == "route"


def test_stream_done_event_carries_tool_status(monkeypatch):
    from rag.answer_service import AnswerService
    import rag.answer as answer_module

    monkeypatch.setattr(runtime, "tool_status", lambda route, use_remote: {"available": False, "code": "untested",
                                                                           "reason": "Run Test connection."})
    monkeypatch.setattr(answer_module, "generate_answer_stream", lambda *a, **k: iter([{"type": "done", "answer": "ok"}]))
    service = AnswerService(
        answer_context=lambda body, query, trace: ([], [], None), resolve_profile=lambda b, q: None,
        inspect_context=lambda r, b: None, domain_context=lambda r, b: None,
        resolve_preview_mode=lambda b: "off", report_stream_error=lambda exc: None,
    )

    events = [json.loads(chunk[len("data: "):]) for chunk in service.stream({"query": "q", "route": "search"})]

    assert [e["type"] for e in events] == ["done"]
    assert events[0]["tools"] == {"available": False, "code": "untested", "reason": "Run Test connection."}


def test_no_project_source_means_no_tools(monkeypatch):
    from rag.answer_service import AnswerService
    import rag.answer as answer_module

    captured = {}
    monkeypatch.setattr(runtime, "tool_status", lambda route, use_remote: {"available": True, "code": "available",
                                                                           "reason": None})
    monkeypatch.setattr(runtime, "latest_workspace", lambda filename: None)

    def fake_stream(*args, **kwargs):
        captured["tool_context"] = kwargs.get("tool_context")
        return iter([{"type": "done", "answer": "ok"}])

    monkeypatch.setattr(answer_module, "generate_answer_stream", fake_stream)
    service = AnswerService(
        answer_context=lambda body, query, trace: ([], [], None), resolve_profile=lambda b, q: None,
        inspect_context=lambda r, b: None, domain_context=lambda r, b: None,
        resolve_preview_mode=lambda b: "off", report_stream_error=lambda exc: None,
    )

    done = json.loads(list(service.stream({"query": "q", "route": "search"}))[-1][len("data: "):])

    assert captured["tool_context"] is None
    assert done["tools"]["code"] == "no_source"


def test_empty_answer_after_tools_gets_one_tool_free_retry():
    model = _model(_call("lookup", {"q": "User"}), AIMessage(content=""), AIMessage(content="User lives in app/Models/User.php."))
    events = _run(model, [_tool()])

    done = events[-1]
    assert done["type"] == "agent_done" and "app/Models/User.php" in done["answer"]
    assert done["tool_calls"][0]["status"] == "success"


def test_empty_retry_yields_an_explicit_message_not_a_blank_answer():
    events = _run(_model(_call("lookup", {"q": "User"}), AIMessage(content=""), AIMessage(content="")), [_tool()])

    assert events[-1]["answer"] == runtime._NO_ANSWER_TEXT


def test_request_budget_caps_tool_output_and_zero_means_unlimited():
    assert ToolLimits(max_request_tokens=0).request_budget().max_output_bytes == 65_536
    assert ToolLimits(max_request_tokens=6500).request_budget().max_output_bytes == 7_800
    assert ToolLimits(max_request_tokens=1000).request_budget().max_output_bytes == 4_096


def test_limits_read_the_request_budget(tmp_path):
    from agent_tools.limits import load_limits

    path = tmp_path / "o.yml"
    path.write_text("agent_tools:\n  max_request_tokens: 6500\n")
    assert load_limits(path).max_request_tokens == 6500
    assert load_limits(tmp_path / "missing.yml").max_request_tokens == 0


class _RecordingFake(_ToolCallingFake):
    seen: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append([getattr(m, "content", "") for m in messages if getattr(m, "type", "") == "tool"])
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def test_older_tool_results_are_cleared_under_a_request_budget():
    big = _tool(handler=lambda args, ctx: {"text": "x" * 6000, "q": args["q"]})
    calls = [_call("lookup", {"q": str(i)}, call_id=f"c{i}") for i in range(3)]
    model = _RecordingFake(responses=[*calls, AIMessage(content="done")], seen=[])

    events = _run(model, [big], limits=ToolLimits(max_request_tokens=2000))

    assert events[-1]["answer"] == "done"
    last_request = model.seen[-1]
    assert len(last_request) == 3
    assert all("x" * 100 in content for content in last_request[-2:])   # two newest kept
    assert "x" * 100 not in last_request[0] and "already used" in last_request[0]  # oldest cleared


def test_without_a_budget_tool_results_stay_intact():
    big = _tool(handler=lambda args, ctx: {"text": "x" * 6000, "q": args["q"]})
    calls = [_call("lookup", {"q": str(i)}, call_id=f"c{i}") for i in range(2)]
    model = _RecordingFake(responses=[*calls, AIMessage(content="done")], seen=[])

    _run(model, [big], limits=ToolLimits())

    assert all("x" * 100 in content for content in model.seen[-1])


def test_final_answer_retry_respects_the_budget():
    captured = {}

    class Capture(_ToolCallingFake):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            captured["chars"] = sum(len(str(m.content)) for m in messages)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    big = _tool(handler=lambda args, ctx: {"text": "y" * 6000})
    model = Capture(responses=[_call("lookup", {"q": "a"}, "c1"), _call("lookup", {"q": "b"}, "c2"),
                               AIMessage(content=""), AIMessage(content="final")])

    events = _run(model, [big], limits=ToolLimits(max_request_tokens=2000))

    assert events[-1]["answer"] == "final"
    assert captured["chars"] < 2000 * 4
