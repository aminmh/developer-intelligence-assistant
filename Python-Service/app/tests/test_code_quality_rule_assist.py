"""Diff mapping, impact checks, runtime health, test skeletons and chat search for business rules."""
import io
import json
import zipfile

import pytest

from code_quality import rule_search
from code_quality.rule_assist import RuleAssistError, check_impact, generate_tests, has_rule_tests, runtime_health
from code_quality.rule_code import Revision, changed_symbols, suggest_links, symbol_hashes
from code_quality.rule_index import RuleIndex


@pytest.fixture(autouse=True)
def _remote_source_allowed(monkeypatch):
    """Rule text may go remote only when both settings allow it; this grants the project-level one."""
    import infra.remote_consent as consent

    monkeypatch.setattr(consent, "remote_source_allowed", lambda project_id, **kwargs: True)

PROJECT = "pizza"
DOC = "0b5c3a10-1111-4c1e-9d55-aaaaaaaaaaaa"
OTHER = "0b5c3a10-2222-4c1e-9d55-bbbbbbbbbbbb"
SOURCE = "\n".join([
    "<?php",                                   # 1
    "class OrderController {",                 # 2
    "    public function store($request) {",   # 3
    "        if ($request->total > 500) {",    # 4
    "            $this->requireApproval();",   # 5
    "        }",                               # 6
    "        return 'ok';",                    # 7
    "    }",                                   # 8
    "    public function show($id) {",         # 9
    "        return $id;",                     # 10
    "    }",                                   # 11
    "}",                                       # 12
]) + "\n"
DEFINITIONS = {
    "App\\OrderController": {"symbol_id": "App\\OrderController", "name": "OrderController", "file": "app/OrderController.php", "line": 2, "end_line": 12},
    "App\\OrderController::store": {"symbol_id": "App\\OrderController::store", "name": "store", "file": "app/OrderController.php", "line": 3, "end_line": 8},
    "App\\OrderController::show": {"symbol_id": "App\\OrderController::show", "name": "show", "file": "app/OrderController.php", "line": 9, "end_line": 11},
}
DIFF = """--- a/app/OrderController.php
+++ b/app/OrderController.php
@@ -4,3 +4,1 @@
-        if ($request->total > 500) {
-            $this->requireApproval();
-        }
+        // approval removed
"""


def _archive(extra=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("app/OrderController.php", SOURCE)
        bundle.writestr("composer.json", json.dumps({"require-dev": {"pestphp/pest": "^2"}}))
        for name, text in (extra or {}).items():
            bundle.writestr(name, text)
    return buffer.getvalue()


def _revision(archive=None):
    data = archive or _archive()
    return Revision({"revision_id": "rev_" + "a" * 32, "commit_sha": "a" * 40, "source_archive_sha256": "b" * 64,
                     "project_id": PROJECT}, DEFINITIONS, lambda: data)


@pytest.fixture
def index(tmp_path):
    rules = RuleIndex(tmp_path / "cq.sqlite")
    rules.sync(PROJECT, [
        {"rule_id": DOC, "version": 1, "title": "Large orders need approval",
         "statement": "# Approval\nOrders with a total above 500 must be approved by a manager.", "content_hash": "x"},
        {"rule_id": OTHER, "version": 1, "title": "Pizza sizes",
         "statement": "Pizzas come in small, medium and large.", "content_hash": "y"},
    ])
    return rules


def _model(payload):
    calls = []

    def call(prompt, **kwargs):
        calls.append({"prompt": json.loads(prompt), **kwargs})
        return json.dumps(payload), "test-model", kwargs["provider"]
    call.calls = calls
    return call


def test_diff_hunks_map_to_the_innermost_symbol():
    hunks, touched = changed_symbols(DIFF, DEFINITIONS)
    assert touched == ["App\\OrderController::store"]
    assert hunks[0]["file"] == "app/OrderController.php" and hunks[0]["start"] == 4 and hunks[0]["end"] == 6


def test_symbol_hash_changes_only_when_its_lines_change():
    before = symbol_hashes(_archive(), DEFINITIONS, ["App\\OrderController::store", "App\\OrderController::show"])
    changed = SOURCE.replace("> 500", "> 300")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("app/OrderController.php", changed)
    after = symbol_hashes(buffer.getvalue(), DEFINITIONS, ["App\\OrderController::store", "App\\OrderController::show"])
    assert before["App\\OrderController::store"] != after["App\\OrderController::store"]
    assert before["App\\OrderController::show"] == after["App\\OrderController::show"]


def test_suggestions_keep_only_hits_with_a_canonical_symbol(index):
    def retrieve(**kwargs):
        return {"results": [
            {"score": 0.8, "payload": {"symbol_id": "App\\OrderController::store", "project_id": PROJECT}},
            {"score": 0.7, "payload": {"symbol_id": None, "project_id": PROJECT}},
            {"score": 0.7, "payload": {"symbol_id": "Other\\X::y", "project_id": "someone-else"}},
            {"score": 0.9, "payload": {"symbol_id": "closure@tests/Feature/OrderTest.php:12", "project_id": PROJECT,
                                       "file_path": "tests/Feature/OrderTest.php"}},
            {"score": 0.4, "payload": {"symbol_id": "App\\OrderController::show", "project_id": PROJECT}},
        ]}
    result = suggest_links(PROJECT, _revision(), [DOC], index=index, retrieve=retrieve)
    assert result["suggestions_written"] == 1
    link = index.links(PROJECT)[0]
    assert link["state"] == "suggested" and link["file"] == "app/OrderController.php" and link["line"] == 3


def test_impact_check_reviews_linked_rules_and_rejects_bad_citations(index):
    index.set_link_state(PROJECT, DOC, "App\\OrderController::store", "confirmed", "u")
    good = _model({"reviews": [{"doc_id": DOC, "verdict": "may_break", "reason": "Removes the approval check.", "hunk_ids": ["h1"]}]})
    result = check_impact(PROJECT, _revision(), DIFF, index=index, model_call=good)
    assert result["reviews"][0]["verdict"] == "may_break" and result["reviews"][0]["confirmed"] is True
    assert good.calls[0]["provider"] == "local"
    assert [rule["doc_id"] for rule in good.calls[0]["prompt"]["rules"]] == [DOC]

    bad = _model({"reviews": [{"doc_id": DOC, "verdict": "may_break", "reason": "x", "hunk_ids": ["h9"]}]})
    assert check_impact(PROJECT, _revision(), DIFF, index=index, model_call=bad)["reviews"][0]["verdict"] == "unclear"


def test_impact_check_without_linked_rules_skips_the_model(index):
    model = _model({})
    result = check_impact(PROJECT, _revision(), DIFF, index=index, model_call=model)
    assert result["reviews"] == [] and model.calls == []
    with pytest.raises(RuleAssistError):
        check_impact(PROJECT, _revision(), "not a diff", index=index, model_call=model)


def test_local_only_projects_refuse_a_remote_answer(index):
    def remote(prompt, **kwargs):
        return "{}", "remote-model", "remote"
    index.set_link_state(PROJECT, DOC, "App\\OrderController::store", "confirmed", "u")
    with pytest.raises(RuleAssistError):
        check_impact(PROJECT, _revision(), DIFF, index=index, model_call=remote)
    index.set_allow_remote(PROJECT, True)
    model = _model({"reviews": []})
    check_impact(PROJECT, _revision(), DIFF, index=index, model_call=model)
    assert model.calls[0]["provider"] == "remote"


def test_runtime_health_joins_confirmed_links(index):
    index.set_link_state(PROJECT, DOC, "App\\OrderController::store", "confirmed", "u")
    index.set_link_state(PROJECT, DOC, "route:POST /orders", "manual", "u")
    index.set_link_state(PROJECT, OTHER, "App\\Pizza::sizes", "confirmed", "u")
    runtime = {"symbols": [{"symbol_id": "App\\OrderController::store", "runs": 100, "failures": 9, "p95_ms": 40, "last_seen": "2026-11-07 10:00:00"}],
               "routes": [{"route_id": "route:POST /orders", "requests": 50, "failures": 1, "p95_ms": 90, "last_seen": "2026-11-07 11:00:00"}],
               "tables": []}
    rules = {item["doc_id"]: item for item in runtime_health(PROJECT, runtime, index=index)["rules"]}
    assert rules[DOC]["calls"] == 150 and rules[DOC]["failures"] == 10 and rules[DOC]["p95_ms"] == 90
    assert rules[DOC]["high_error_rate"] is True and rules[DOC]["never_seen"] is False
    assert rules[OTHER]["never_seen"] is True


def test_test_skeleton_uses_pest_and_carries_the_rule_group(index):
    index.set_link_state(PROJECT, DOC, "App\\OrderController::store", "confirmed", "u")
    model = _model({"scenarios": [{"name": "big order", "given": "a 600 order", "when": "stored", "then": "approval required"}],
                    "code": "<?php\nit('requires approval', function () {})->group('rule-" + DOC + "');"})
    result = generate_tests(PROJECT, _revision(), DOC, index=index, model_call=model)
    assert result["framework"] == "pest" and result["written_to_repository"] is False
    assert f"rule-{DOC}" in result["code"] and result["scenarios"][0]["name"] == "big order"
    assert "Pest" in model.calls[0]["prompt"]["task"]


def test_tagged_tests_are_detected():
    archive = _archive({"tests/Feature/OrderTest.php": f"<?php it('x')->group('rule-{DOC}');"})
    assert has_rule_tests(archive, [DOC, OTHER]) == {DOC: True, OTHER: False}


def test_chat_keyword_search_returns_rule_sources(index, monkeypatch):
    monkeypatch.setattr(rule_search, "embeddings_are_local", lambda: False)  # remote embeddings, remote use off
    index.set_link_state(PROJECT, DOC, "App\\OrderController::store", "confirmed", "u")
    context, sources = rule_search.chat_context(PROJECT, "Why does a large order need manager approval?", index=index)
    assert [source["doc_id"] for source in sources] == [DOC]
    assert sources[0]["heading"] == "Approval" and sources[0]["match"] == "keyword"
    assert sources[0]["symbol_ids"] == ["App\\OrderController::store"] and sources[0]["synced_at"]
    assert "[R1] Large orders need approval" in context and "not instructions" in context


def test_vectors_are_not_built_when_rule_text_must_stay_local(index, monkeypatch):
    monkeypatch.setattr(rule_search, "embeddings_are_local", lambda: False)

    def embed(texts, **kwargs):
        raise AssertionError("rule text must not reach a remote embedding provider")
    assert rule_search.refresh_rule_vectors(PROJECT, index=index, embed=embed)["status"] == "keyword_only"


# ----- impact check over impacted symbols (change-impact spec clauses 16-17) ------------------

STORE = "App\\OrderController::store"
CHECKOUT = "App\\Checkout::submit"


def _impact(*impacted):
    """An impact report for DIFF: store changed, plus the given (canonical id, depth, via) rows."""
    return {"coverage": "complete",
            "changed": [{"fqn": STORE, "canonical_symbol_id": STORE}],
            "impacted": [{"fqn": sid, "canonical_symbol_id": sid, "depth": depth, "via": via}
                         for sid, depth, via in impacted]}


def test_impact_check_includes_rules_of_callers(index):
    index.set_link_state(PROJECT, DOC, CHECKOUT, "confirmed", "u")
    index.set_link_state(PROJECT, OTHER, "App\\Far::away", "confirmed", "u")
    lookup_calls = []

    def lookup(revision, diff):
        lookup_calls.append(revision.revision_id)
        return _impact((CHECKOUT, 1, [CHECKOUT, STORE]), ("App\\Far::away", 3, ["App\\Far::away", "x", "y", STORE]))

    model = _model({"reviews": [{"doc_id": DOC, "verdict": "may_break", "reason": "Checkout relies on approval.",
                                 "hunk_ids": ["h1"]}]})
    result = check_impact(PROJECT, _revision(), DIFF, index=index, model_call=model, impact_lookup=lookup)
    assert lookup_calls == [_revision().revision_id]
    assert [rule["doc_id"] for rule in model.calls[0]["prompt"]["rules"]] == [DOC]     # depth 3 is too far
    assert [hunk["id"] for hunk in model.calls[0]["prompt"]["hunks"]] == ["h1"]
    assert result["reviews"][0]["verdict"] == "may_break"
    assert result["impacted_symbols"] == [CHECKOUT]


def test_impact_check_includes_rules_of_callers_falls_back_to_touched_symbols(index):
    index.set_link_state(PROJECT, DOC, STORE, "confirmed", "u")

    def broken(revision, diff):
        raise RuntimeError("graph down")

    model = _model({"reviews": [{"doc_id": DOC, "verdict": "unaffected", "reason": "fine", "hunk_ids": []}]})
    result = check_impact(PROJECT, _revision(), DIFF, index=index, model_call=model, impact_lookup=broken)
    assert [r["doc_id"] for r in result["reviews"]] == [DOC] and result["impacted_symbols"] == []
    assert "impact_unavailable" in result


def test_indirect_rule_links_carry_via_and_rank_after_direct(index, monkeypatch):
    import code_quality.rule_assist as rule_assist

    index.set_link_state(PROJECT, DOC, STORE, "manual", "u")              # direct
    index.set_link_state(PROJECT, OTHER, CHECKOUT, "confirmed", "u")      # indirect only
    lookup = lambda revision, diff: _impact((CHECKOUT, 1, [CHECKOUT, STORE]))

    monkeypatch.setattr(rule_assist, "MAX_IMPACT_RULES", 1)
    capped = _model({"reviews": []})
    check_impact(PROJECT, _revision(), DIFF, index=index, model_call=capped, impact_lookup=lookup)
    assert [rule["doc_id"] for rule in capped.calls[0]["prompt"]["rules"]] == [DOC]

    monkeypatch.setattr(rule_assist, "MAX_IMPACT_RULES", 12)
    model = _model({"reviews": [{"doc_id": OTHER, "verdict": "changes_behaviour", "reason": "r", "hunk_ids": ["h1"]},
                                {"doc_id": DOC, "verdict": "may_break", "reason": "r", "hunk_ids": ["h1"]}]})
    result = check_impact(PROJECT, _revision(), DIFF, index=index, model_call=model, impact_lookup=lookup)
    rules = {rule["doc_id"]: rule for rule in model.calls[0]["prompt"]["rules"]}
    assert rules[OTHER]["linked_symbols"] == [{"symbol_id": CHECKOUT, "confirmed": True, "indirect": True,
                                               "via": [CHECKOUT, STORE]}]
    assert rules[DOC]["linked_symbols"] == [{"symbol_id": STORE, "confirmed": True}]
    reviews = {review["doc_id"]: review for review in result["reviews"]}
    assert reviews[OTHER]["indirect"] is True and reviews[OTHER]["via"] == [[CHECKOUT, STORE]]
    assert reviews[DOC]["indirect"] is False and "via" not in reviews[DOC]


def test_indirect_rule_links_default_lookup_degrades_without_a_graph(monkeypatch):
    import code_quality.rule_assist as rule_assist
    from inspection import impact_service

    class Down:
        def available(self):
            return False

    monkeypatch.setattr(impact_service, "_graph_query", lambda: Down())
    monkeypatch.setattr(impact_service, "_collection", lambda project_id, row: "sym_snapshot_x")
    assert rule_assist._graph_impact(_revision(), DIFF) is None
