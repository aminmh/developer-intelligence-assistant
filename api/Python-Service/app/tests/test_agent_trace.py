"""Tool calls are recorded in the answer's retrieval trace (navigation plan task 19)."""

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

import agent_tools.approvals as approvals
import agent_tools.runtime as runtime
import agent_tools.trace as trace
import index.llamaindex_layer as layer
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


def _tool(name="lookup", effect=EFFECT_READ_ONLY, source="builtin", version=None):
    return ToolDefinition(
        name=name, description=f"{name} tool.",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"],
                      "additionalProperties": False},
        effect=effect, source=source, workflows=USER_WORKFLOWS, version=version,
        handler=lambda args, ctx: {"file": "app/Models/User.php"},
    )


def _call(name, args, call_id="call_1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(layer.settings, "log_dir", str(tmp_path / "logs"))
    monkeypatch.setattr(layer, "_TRACE_CACHE", {})
    monkeypatch.setenv("AGENT_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(approvals, "_checkpoint_conn", None)
    monkeypatch.setattr(runtime, "_overview_for", lambda context: "")


def _run(messages, tools, run_meta, context=ToolContext(workflow="search")):
    registry = ToolRegistry(builtins=tools, managed_loader=lambda: [])
    return list(runtime.stream_agent_answer("Where is User?", "system", context, registry=registry,
                                            limits=ToolLimits(), model=_Fake(responses=list(messages)),
                                            run_meta=run_meta))


def _stored(trace_id):
    layer._TRACE_CACHE.clear()  # read back from the JSONL file, as after a restart
    return layer.get_retrieval_trace(trace_id)


def test_tool_calls_join_the_existing_retrieval_trace():
    layer.record_retrieval_trace("t-1", {"mode": "hybrid", "sources": [{"file": "a.php"}]})

    _run([_call("lookup", {"q": "User"}), AIMessage(content="In app/Models/User.php.")], [_tool()],
         {"trace_id": "t-1"})

    stored = _stored("t-1")
    assert stored["mode"] == "hybrid" and stored["sources"] == [{"file": "a.php"}]
    agent = stored["agent"]
    assert agent["status"] == "done"
    call = agent["tool_calls"][0]
    assert call["name"] == "lookup" and call["arguments"] == {"q": "User"} and call["status"] == "success"
    assert call["version"] == "builtin" and call["source"] == "builtin" and call["effect"] == EFFECT_READ_ONLY
    # Tools ran, so the source is resolved; this context has none.
    assert agent["revision"]["workflow"] == "search" and agent["revision"]["source_error"] == "no_source"


def test_runs_without_a_trace_id_write_nothing(tmp_path):
    _run([_call("lookup", {"q": "User"}), AIMessage(content="ok")], [_tool()], {})

    assert not (tmp_path / "logs" / "retrieval-traces.jsonl").exists()


def test_answers_without_tool_calls_record_the_revision_without_resolving_it(monkeypatch):
    monkeypatch.setattr("agent_tools.source.resolve_source",
                        lambda context: pytest.fail("source resolved without tool calls"))
    context = ToolContext(workflow="search", project_id="pizza", commit_sha="abc123")

    _run([AIMessage(content="Retrieval was enough.")], [_tool()], {"trace_id": "t-2"}, context=context)

    agent = _stored("t-2")["agent"]
    assert agent["tool_calls"] == []
    assert agent["revision"] == {"project_id": "pizza", "revision_id": None, "commit_sha": "abc123",
                                 "workflow": "search"}


def test_paused_runs_record_the_approval():
    events = _run([_call("open_ticket", {"q": "bug"})], [_tool("open_ticket", effect=EFFECT_SIDE_EFFECTING)],
                  {"trace_id": "t-3", "owner": "u1"})

    approval_id = next(e for e in events if e["type"] == "approval_required")["approval_id"]
    agent = _stored("t-3")["agent"]
    assert agent["status"] == "paused" and agent["approval_id"] == approval_id


def test_versions_name_the_backing_binary_or_managed_revision(monkeypatch):
    monkeypatch.setattr(trace, "_backend_version", lambda name: "ripgrep 13.0.0" if name == "code_text_search" else None)
    definitions = {
        "code_text_search": _tool("code_text_search"),
        "code_read": _tool("code_read"),
        "jira": _tool("jira", source="managed", version="managed 2026-11-10T10:00:00Z"),
    }
    calls = [{"name": n, "arguments": {"q": "x"}, "status": "success"} for n in (*definitions, "gone")]

    versions = [row["version"] for row in trace.trace_calls(calls, definitions)]
    assert versions == ["builtin; ripgrep 13.0.0", "builtin", "managed 2026-11-10T10:00:00Z", "unknown"]


def test_long_arguments_are_cut():
    rows = trace.trace_calls([{"name": "lookup", "arguments": {"q": "x" * 2000}}], {"lookup": _tool()})

    assert isinstance(rows[0]["arguments"], str) and len(rows[0]["arguments"]) <= 501


def test_workspace_source_is_named_by_folder(tmp_path):
    workspace = tmp_path / "upload_42"
    workspace.mkdir()
    (workspace / "composer.json").write_text("{}")

    identity = trace.revision_identity(ToolContext(workflow="debug", workspace_path=str(workspace)), resolve=True)

    assert identity["source"] == "workspace" and identity["workspace"] == "upload_42"
    assert identity["archive_sha256"] is None


def test_trace_write_failures_never_break_the_answer(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(layer, "get_retrieval_trace", broken)

    events = _run([_call("lookup", {"q": "User"}), AIMessage(content="ok")], [_tool()], {"trace_id": "t-4"})
    assert events[-1]["type"] == "agent_done" and events[-1]["answer"] == "ok"
