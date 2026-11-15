"""A scripted tool-calling model drives the real navigation tools on a fixture revision (plan task 29).

Unlike the unit tests, nothing here is stubbed below the model: ripgrep, Tree-sitter,
the repo map, path checks, and secret exclusion all run on a small Go project.
Symbol lookup needs Neo4j and is covered by the live check instead.
"""

import json
import shutil
from pathlib import Path

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

import agent_tools.runtime as runtime
from agent_tools.limits import ToolLimits
from agent_tools.registry import ToolRegistry
from agent_tools.types import ToolContext

FIXTURE = Path(__file__).parent / "fixtures" / "agent_revision"


class _Recorder(BaseChatModel):
    """Replays scripted turns and keeps every tool result the agent hands back."""

    responses: list
    seen: dict

    @property
    def _llm_type(self) -> str:
        return "scripted-recorder"

    def bind_tools(self, tools, **kwargs):
        self.seen["tools"] = sorted(t["function"]["name"] if isinstance(t, dict) else t.name for t in tools)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        for message in messages:
            if isinstance(message, ToolMessage):
                self.seen.setdefault("results", {})[message.tool_call_id] = message.content
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])


def _call(call_id, name, args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


@pytest.fixture
def revision(tmp_path, monkeypatch):
    root = tmp_path / "shop"
    shutil.copytree(FIXTURE, root)
    (root / ".env").write_text("SECRET_TOKEN=fixture-value-must-never-be-read\n")
    monkeypatch.setenv("REPO_MAP_DIR", str(tmp_path / "maps"))
    monkeypatch.setattr(runtime, "_overview_for", lambda context: "")
    if not shutil.which("rg"):
        pytest.skip("ripgrep is installed only in the API image")
    return root


def test_agent_answers_from_real_tool_output_on_a_fixture_revision(revision):
    model = _Recorder(seen={}, responses=[
        _call("map", "code_repo_map", {"max_files": 5}),
        _call("search", "code_text_search", {"pattern": "refuse(", "fixed_string": True}),
        _call("outline", "code_outline", {"path": "internal/auth/login.go"}),
        _call("read", "code_read", {"path": "internal/auth/login.go", "start_line": 9, "end_line": 17}),
        _call("secret", "code_read", {"path": ".env"}),
        AIMessage(content="`Server::Login` refuses locked accounts and empty passwords "
                          "(`internal/auth/login.go:10`, `internal/auth/login.go:13`)."),
    ])
    context = ToolContext(workflow="search", workspace_path=str(revision))
    registry = ToolRegistry(managed_loader=lambda: [])

    events = list(runtime.stream_agent_answer("Why does login get refused?", "system", context,
                                              registry=registry, limits=ToolLimits(), model=model))
    seen = model.seen  # the model holds its own copy of the dict it was given

    assert events[-1]["type"] == "agent_done" and "Server::Login" in events[-1]["answer"]
    assert {"code_repo_map", "code_text_search", "code_outline", "code_read", "code_symbol_lookup"} <= set(seen["tools"])
    statuses = {c["id"]: (c["status"], c["error"]) for c in events[-1]["tool_calls"]}
    assert statuses == {"map": ("success", None), "search": ("success", None), "outline": ("success", None),
                        "read": ("success", None), "secret": ("error", "excluded_path")}

    def output(value):
        payload = json.loads(value)["output"]
        return json.loads(payload) if isinstance(payload, str) else payload

    results = {key: output(value) for key, value in seen["results"].items() if key != "secret"}
    ranked = results["map"]["files"]
    # The Tree-sitter producer ran on the Go source: Hash is called from another file, refuse from its own.
    assert ranked[0]["path"] == "internal/util/hash.go"
    assert ranked[0]["symbols"][0]["name"] == "Hash" and ranked[0]["symbols"][0]["callers_elsewhere"] == 1
    matches = results["search"]["items"]
    assert {(m["path"], m["line"]) for m in matches} == {("internal/auth/login.go", 11), ("internal/auth/login.go", 14),
                                                         ("internal/auth/login.go", 19)}
    outline = json.dumps(results["outline"])
    assert "Login" in outline and "refuse" in outline and "Refused" in outline
    read = results["read"]
    assert "s.locked[user]" in json.dumps(read) and "fixture-value" not in json.dumps(seen["results"])
