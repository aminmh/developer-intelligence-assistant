"""Budgeted per-category review (gap-closing plan, Phase 4; budgeted-exploratory-review spec)."""

import json

import pytest
from langchain_core.messages import AIMessage

from agent_tools.types import ToolContext
from code_quality import category_review
from code_quality.category_review import ReviewConfig, _TokenPool, returned_ranges, review_category, run_reviews

SOURCE = "\n".join(f"line {n}" for n in range(1, 41)) + "\n"


class ScriptedModel:
    """Replies with a fixed list of messages; records what it was sent and whether tools were bound."""

    def __init__(self, replies, tokens=100):
        self.replies, self.tokens, self.calls = list(replies), tokens, []

    def bind_tools(self, tools):
        self.tools = tools
        return self

    def invoke(self, messages):
        self.calls.append(list(messages))
        reply = self.replies.pop(0) if self.replies else AIMessage(content='{"findings": [], "summary": "done"}')
        if isinstance(reply, Exception):
            raise reply
        reply.usage_metadata = {"input_tokens": self.tokens, "output_tokens": 10, "total_tokens": self.tokens + 10}
        return reply


def _call(name, args, call_id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def _answer(findings, summary="Checked the service."):
    return AIMessage(content=json.dumps({"findings": findings, "summary": summary}))


@pytest.fixture
def context(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/Service.php").write_text(SOURCE)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/setup.md").write_text("# Setup\nRun make up.\n")
    return ToolContext(workflow="search", workspace_path=str(tmp_path))


def _review(model, context, config=ReviewConfig(enabled=True), pool=None, leads=()):
    return review_category("reliability", list(leads), {"status": "covered"}, context, model, config,
                           pool or _TokenPool(1_000_000))


def test_findings_are_kept_only_when_a_tool_showed_the_cited_lines(context):
    model = ScriptedModel([
        _call("code_read", {"path": "src/Service.php", "start_line": 1, "end_line": 20}, "c1"),
        _call("evidence_read", {"path": "docs/setup.md"}, "c2"),
        _answer([
            {"title": "Swallowed error", "message": "The catch returns success.", "severity": "error",
             "citations": [{"path": "src/Service.php", "start_line": 3, "end_line": 5}]},
            {"title": "Unseen", "message": "Lines the model never read.", "severity": "warning",
             "citations": [{"path": "src/Service.php", "start_line": 30, "end_line": 31}]},
            {"title": "No citation", "message": "Claims without evidence.", "citations": []},
            {"title": "Docs", "message": "Setup names a target.", "severity": "info",
             "citations": [{"path": "docs/setup.md", "start_line": 2, "end_line": 2}]},
        ]),
    ])
    result = _review(model, context, leads=[{"rule": "empty-catch", "file": "src/Service.php", "line": 4,
                                             "severity": "warning", "message": "Empty catch."}])
    assert result["status"] == "complete" and result["reason"] is None
    assert result["accepted_findings"] == 2 and result["rejected_citations"] == 2
    kept = {(f["file"], f["line"], f["end_line"], f["severity"]) for f in result["findings"]}
    assert kept == {("src/Service.php", 3, 5, "error"), ("docs/setup.md", 2, 2, "info")}
    assert all(f["category"] == "reliability" and f["tool"] == "ai-category-review" for f in result["findings"])
    assert [(t["tool"], t["ok"], t["ranges_returned"]) for t in result["trace"]] == [("code_read", True, 1), ("evidence_read", True, 1)]
    assert result["usage"]["tool_calls"] == 2 and result["usage"]["total_tokens"] == 330
    first_prompt = model.calls[0][1].content
    assert "empty-catch" in first_prompt and "checklist" in first_prompt
    assert {spec["function"]["name"] for spec in model.tools} == set(category_review.TOOLS)


def test_search_hits_count_only_for_their_own_line():
    output = {"items": [{"path": "a.php", "line": 7, "text": "x"}, {"path": "b.php", "line": 2}]}
    assert returned_ranges("code_text_search", output) == [("a.php", 7, 7)]
    assert returned_ranges("code_outline", {"path": "a.php", "items": [{"line": 1, "end_line": 9}]}) == []


def test_the_tool_call_limit_stops_the_review_and_marks_it_partial(context):
    reads = [_call("code_read", {"path": "src/Service.php", "start_line": n, "end_line": n + 1}, f"c{n}") for n in (1, 3, 5)]
    model = ScriptedModel([*reads, _answer([])])
    result = _review(model, context, ReviewConfig(enabled=True, max_tool_calls=2))
    assert result["status"] == "partial" and "tool calls limit" in result["reason"]
    assert result["usage"]["tool_calls"] == 2
    assert "budget is used up" in model.calls[-1][-1].content


def test_token_and_global_limits(context):
    model = ScriptedModel([_call("code_read", {"path": "src/Service.php"}, "c1"), _answer([])], tokens=5_000)
    assert "tokens limit" in _review(model, context, ReviewConfig(enabled=True, max_tokens=1_000))["reason"]
    pool = _TokenPool(10)
    pool.add(10)
    result = _review(ScriptedModel([_answer([])]), context, pool=pool)
    assert result["status"] == "partial" and "global tokens" in result["reason"]


def test_model_failure_or_invalid_reply_is_unavailable(context):
    assert _review(ScriptedModel([RuntimeError("down")]), context)["status"] == "unavailable"
    result = _review(ScriptedModel([AIMessage(content="I think it is fine.")]), context)
    assert result["status"] == "unavailable" and "JSON" in result["reason"]


def test_all_findings_failing_citations_is_partial_not_clean(context):
    model = ScriptedModel([_answer([{"message": "x", "citations": [{"path": "src/Service.php", "start_line": 1}]}])])
    result = _review(model, context)
    assert result["status"] == "partial" and result["rejected_citations"] == 1 and result["findings"] == []


def test_tools_not_offered_are_refused(context):
    model = ScriptedModel([_call("project_structure", {}, "c1"), _answer([])])
    result = _review(model, context)
    assert result["trace"][0]["ok"] is False and result["trace"][0]["error"] == "not_offered"


def test_run_reviews_shares_one_token_pool(context):
    model = ScriptedModel([_answer([]), _answer([])])
    result = run_reviews(["security", "testing"], {}, {}, context, model, ReviewConfig(enabled=True, parallel=2, per_question=False))
    assert set(result["categories"]) == {"security", "testing"} and result["total_tokens"] == 220
    assert result["config"]["max_tool_calls"] == 20 and result["status"] == "complete"


def test_each_checklist_question_gets_its_own_loop_and_results_merge_per_category(context):
    from code_quality.category_review import CHECKLISTS

    model = ScriptedModel([])  # every loop answers at once with no findings
    config = ReviewConfig(enabled=True, parallel=2, question_tool_calls=4)
    result = run_reviews(["security", "testing"], {}, {}, context, model, config)
    expected = len(CHECKLISTS["security"]) + len(CHECKLISTS["testing"])
    assert result["loops"] == expected and len(model.calls) == expected
    security = result["categories"]["security"]
    assert [q["question"] for q in security["questions"]] == list(CHECKLISTS["security"])
    assert security["budget"]["max_tool_calls"] == 4 * len(CHECKLISTS["security"])
    prompts = {call[0].content for call in model.calls}
    assert all(any(question in prompt for prompt in prompts) for question in CHECKLISTS["security"])


def test_config_is_off_by_default_and_the_environment_can_enable_it(tmp_path, monkeypatch):
    path = tmp_path / "orchestrator.yml"
    path.write_text("code_quality_review:\n  max_tool_calls: 99\n  parallel: 2\n")
    monkeypatch.delenv("CODE_QUALITY_REVIEW_ENABLED", raising=False)
    config = category_review.load_config(path)
    assert config.enabled is False and config.max_tool_calls == 40 and config.parallel == 2
    monkeypatch.setenv("CODE_QUALITY_REVIEW_ENABLED", "true")
    assert category_review.load_config(path).enabled is True


def test_assessment_records_why_reviews_did_not_run(monkeypatch):
    from code_quality import service

    monkeypatch.delenv("CODE_QUALITY_REVIEW_ENABLED", raising=False)
    result = service._run_category_reviews("p", "r", {}, {"php": 1}, [], {}, False)
    assert result["status"] == "disabled" and "turned off" in result["reason"]
    monkeypatch.setenv("CODE_QUALITY_REVIEW_ENABLED", "true")
    import agent_tools.runtime as runtime

    monkeypatch.setattr(runtime, "tool_status", lambda *_: {"available": False, "reason": "Code tools need the external model."})
    assert service._run_category_reviews("p", "r", {}, {"php": 1}, [], {}, False)["reason"] == "Code tools need the external model."
