from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_quality.design_metrics import build_units, load_rubric, score_design, select_candidates
from code_quality.design_judge import judge_candidates
from code_quality.service import ReportStore


def fixture_chunks():
    cls = {"node_type": "class", "fq_symbol_name": "App\\Example", "file_path": "Example.php",
           "start_line": 1, "loc": 50, "metric_schema": "code-metrics-1", "content_hash": "class",
           "signature": "class App\\Example", "constructor_dependencies": []}
    methods = [{"node_type": "method", "parent_symbol": "App\\Example", "method_name": f"m{i}",
                "fq_symbol_name": f"App\\Example::m{i}", "start_line": i + 2, "loc": 3,
                "metric_schema": "code-metrics-1", "content_hash": str(i), "signature": f"m{i}()",
                "reads_properties": ["shared" if i < 2 else f"p{i}"], "writes_properties": [],
                "cyclomatic": 1, "max_nesting": 0, "param_count": 0, "type_checks": 0}
               for i in range(4)]
    return [cls, *methods]


def test_candidates_and_cached_verdict_are_stable(tmp_path):
    rubric, rubric_hash = load_rubric()
    units = build_units(fixture_chunks(), rubric)
    assert units[0]["metrics"]["lcom4"] == 3
    candidates, counts = select_candidates("pizza", "rev_1", units, rubric)
    assert [c["rule_id"] for c in candidates] == ["srp-lcom4"]
    store = ReportStore(tmp_path / "reports.sqlite")
    calls = []
    def model(prompt, **kwargs):
        calls.append(prompt)
        return (json.dumps({"verdicts": [{"evidence_id": candidates[0]["evidence_id"], "principle": "SRP", "verdict": "violation", "reason": "Unrelated methods."}]}), "model", "local")
    first = judge_candidates("pizza", candidates, rubric, store, "model", "local", model, 12000)
    category, report, findings = score_design(units, candidates, first, counts, rubric, rubric_hash, False)
    assert category["score"] == 85
    assert findings[0]["evidence_id"] == candidates[0]["evidence_id"]
    next_candidates, next_counts = select_candidates("pizza", "rev_1", units, rubric)
    second = judge_candidates("pizza", next_candidates, rubric, store, "model", "local", model, 12000)
    _, next_report, _ = score_design(units, next_candidates, second, next_counts, rubric, rubric_hash, False)
    assert len(calls) == 1
    assert next_report["clean_code_hash"] == report["clean_code_hash"]
    assert next_report["candidates"][0]["cache_hit"] is True
    assert "card" not in next_report["candidates"][0]


def test_invalid_rubric_fails(tmp_path):
    path = tmp_path / "invalid.yml"
    path.write_text("version: broken\n")
    with pytest.raises(ValueError, match="rubric"):
        load_rubric(path)


def test_failed_model_is_uncertain_and_not_cached(tmp_path):
    rubric, _ = load_rubric()
    candidates, _ = select_candidates("pizza", "rev_1", build_units(fixture_chunks(), rubric), rubric)
    store = ReportStore(tmp_path / "reports.sqlite")
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("provider unavailable")
    for _ in range(2):
        result = judge_candidates("pizza", candidates, rubric, store, "model", "local", fail, 12000)
        assert result[candidates[0]["evidence_id"]]["verdict"] == "uncertain"
    assert len(calls) == 2


def test_design_mode_evaluator_does_not_score_clean_code(monkeypatch):
    import runtime.llm as llm
    from code_quality.service import CATEGORIES, _score
    categories = [category for category in CATEGORIES if category != "clean_code"]
    evidence = [{"evidence_id": f"ev_{category}", "category": category, "kind": "finding", "file": "a.php", "line": 1, "message": "Evidence."} for category in CATEGORIES]
    captured = []
    def complete(prompt, **kwargs):
        captured.append(prompt)
        result = {category: {"assessed": True, "score": 70, "reason": "Evidence", "evidence_ids": [f"ev_{category}"]} for category in categories}
        return json.dumps({"categories": result}), "model", "local"
    monkeypatch.setattr(llm, "complete_with_model", complete)
    monkeypatch.setattr(llm, "remote_configured", lambda: False)
    result, *_ = _score(evidence, {category: {"supported": True} for category in CATEGORIES}, {"php": 1},
                        {"scanned_files": 1, "unsupported_extensions": {}}, [], design_mode=True)
    assert set(result) == set(categories)
    assert "ev_clean_code" not in captured[0]
    assert '"clean_code":{' not in captured[0]


def test_revision_delta_reuses_unchanged_cards(tmp_path):
    rubric, _ = load_rubric()
    first_units = build_units(fixture_chunks(), rubric)
    first, _ = select_candidates("pizza", "rev_1", first_units, rubric)
    store = ReportStore(tmp_path / "reports.sqlite")
    calls = []
    def model(prompt, **kwargs):
        candidates = json.loads(prompt.split("Candidates: ", 1)[1])
        calls.append(candidates)
        return (json.dumps({"verdicts": [{"evidence_id": c["evidence_id"], "principle": c["principle"], "verdict": "acceptable", "reason": "Cohesive."} for c in candidates]}), "model", "local")
    judge_candidates("pizza", first, rubric, store, "model", "local", model, 12000)
    second, _ = select_candidates("pizza", "rev_2", first_units, rubric)
    result = judge_candidates("pizza", second, rubric, store, "model", "local", model, 12000)
    assert result[second[0]["evidence_id"]]["cache_hit"]
    assert first[0]["evidence_id"] != second[0]["evidence_id"]
    changed = fixture_chunks()
    changed[0]["content_hash"] = "changed"
    third, _ = select_candidates("pizza", "rev_3", build_units(changed, rubric), rubric)
    judge_candidates("pizza", third, rubric, store, "model", "local", model, 12000)
    assert len(calls) == 2


def test_oversized_candidate_becomes_uncertain_without_call(tmp_path):
    rubric, _ = load_rubric()
    candidates, _ = select_candidates("pizza", "rev_1", build_units(fixture_chunks(), rubric), rubric)
    candidates[0]["card"]["methods"] = ["x" * 20000]
    store = ReportStore(tmp_path / "reports.sqlite")
    def fail(*args, **kwargs):
        raise AssertionError("model must not be called")
    result = judge_candidates("pizza", candidates, rubric, store, "model", "local", fail, 3000)
    assert result[candidates[0]["evidence_id"]]["reason"] == "context_limit"


def test_wrong_principle_does_not_cache_verdict(tmp_path):
    rubric, _ = load_rubric()
    candidates, _ = select_candidates("pizza", "rev_1", build_units(fixture_chunks(), rubric), rubric)
    store = ReportStore(tmp_path / "reports.sqlite")
    calls = []
    def model(*args, **kwargs):
        calls.append(True)
        return json.dumps({"verdicts": [{"evidence_id": candidates[0]["evidence_id"], "principle": "DIP", "verdict": "violation", "reason": "Wrong principle."}]}), "model", "local"
    for _ in range(2):
        result = judge_candidates("pizza", candidates, rubric, store, "model", "local", model, 12000)
        assert result[candidates[0]["evidence_id"]]["verdict"] == "uncertain"
    assert len(calls) == 2


def test_assessment_uses_metric_score_and_persists_no_card_source(monkeypatch, tmp_path):
    import code_quality.service as service
    import code_quality.design_metrics as metrics
    import code_quality.semantic_context as semantic
    import code_quality.flow_tracing as flow
    import code_quality.external_analyzers as external
    import code_quality.outline as outline
    import code_quality.rules as rules
    import runtime.llm as llm
    revision = "rev_" + "b" * 32
    snapshot = {"commit_sha": "a" * 40, "source_archive_sha256": "c" * 64}
    stats = {"detected_files": 1, "supported_files": 1, "scanned_files": 1, "skipped_files": 0,
             "excluded_files": 0, "scanned_bytes": 100, "unsupported_extensions": {}}
    monkeypatch.setattr(service, "inventory_archive", lambda *_: ({"php": 1}, [], ["Example.php"], stats, []))
    monkeypatch.setattr(metrics, "read_revision_chunks", lambda *_: (fixture_chunks(), False, None))
    monkeypatch.setattr(outline, "OutlineConnectionStore", lambda: type("Store", (), {"get": lambda *_: None})())
    monkeypatch.setattr(external, "ExternalAnalyzerSettingsStore", lambda: type("Store", (), {"get": lambda *_: {}})())
    monkeypatch.setattr(external, "effective_worker_target", lambda *_: None)
    monkeypatch.setattr(external, "deployment_mode", lambda: "production")
    monkeypatch.setattr(rules, "CustomRuleStore", lambda: type("Store", (), {"enabled": lambda *_: []})())
    monkeypatch.setattr(service, "_run_static_rules", lambda *_: ([], [], None))
    semantic_summary = {"status": "available", "retriever_version": "test", "query_count": 4, "evidence_count": 0}
    monkeypatch.setattr(semantic, "retrieve_revision_context", lambda *_, **__: (semantic_summary, []))
    flow_summary = {"status": "complete", "tracer_version": "test", "flow_types": [], "limits": {}}
    monkeypatch.setattr(flow, "retrieve_revision_call_traces", lambda *_, **__: (flow_summary, []))
    monkeypatch.setattr(service, "_score", lambda *_, **__: ({category: {"assessed": True, "score": 60, "reason": "Fixture", "evidence_ids": []}
        for category in service.SOURCE_CATEGORIES if category != "clean_code"}, "model", "local", {"total_tokens": 1}, [], []))
    monkeypatch.setattr(llm, "remote_configured", lambda: False)
    judge_calls = []
    def complete(prompt, **_):
        entries = json.loads(prompt.split("Candidates: ", 1)[1])
        judge_calls.append(entries)
        return json.dumps({"verdicts": [{"evidence_id": entry["evidence_id"], "principle": entry["principle"],
                                       "verdict": "violation", "reason": "Disconnected responsibilities."} for entry in entries]}), "model", "local"
    monkeypatch.setattr(llm, "complete_with_model", complete)
    # Existing evidence prevents archive fallback in this isolated orchestration test.
    monkeypatch.setattr(service, "_category_coverage", lambda *_: {c: {"supported": True, "reason": None} for c in service.SOURCE_CATEGORIES})
    import code_quality.category_context as context
    monkeypatch.setattr(context, "build_category_context", lambda *_args, **_kwargs: ({"status": "available", "source": None, "excerpt_count": 0, "categories": {}, "scanned_files": 0, "scanned_bytes": 0, "reason": "fixture"}, []))
    store = ReportStore(tmp_path / "reports.sqlite")
    report = service.assess_revision("pizza", revision, snapshot, b"archive", "alice", store)
    assert report["categories"]["clean_code"]["score"] == 85
    assert report["categories"]["clean_code"]["score_method"] == "design-metrics-2"
    assert report["overall_score"] is None  # the evidence categories have no evidence in this fixture
    assert report["clean_code_design"]["unit_count"] == 1
    assert "card" not in report["clean_code_design"]["candidates"][0]
    assert store.get_report("pizza", report["report_id"])["clean_code_design"]["clean_code_hash"] == report["clean_code_design"]["clean_code_hash"]
    again = service.assess_revision("pizza", revision, snapshot, b"archive", "alice", store)
    assert len(judge_calls) == 1
    assert again["categories"]["clean_code"]["score"] == report["categories"]["clean_code"]["score"]
    assert again["clean_code_design"]["clean_code_hash"] == report["clean_code_design"]["clean_code_hash"]
    first_candidates = report["clean_code_design"]["candidates"]
    next_candidates = again["clean_code_design"]["candidates"]
    assert [c["evidence_id"] for c in first_candidates] == [c["evidence_id"] for c in next_candidates]
    assert all(c["cache_hit"] for c in next_candidates)


def test_twenty_five_candidates_are_batched_without_silent_drops(tmp_path):
    rubric, _ = load_rubric()
    base, _ = select_candidates("pizza", "rev_1", build_units(fixture_chunks(), rubric), rubric)
    candidates = [{**base[0], "evidence_id": f"ev_{i}", "card_hash": str(i)} for i in range(25)]
    store = ReportStore(tmp_path / "reports.sqlite")
    batches = []
    def model(prompt, **kwargs):
        entries = json.loads(prompt.split("Candidates: ", 1)[1])
        batches.append(entries)
        return json.dumps({"verdicts": [{"evidence_id": e["evidence_id"], "principle": e["principle"],
                                          "verdict": "acceptable", "reason": "Acceptable."} for e in entries]}), "model", "local"
    results = judge_candidates("pizza", candidates, rubric, store, "model", "local", model, 4000)
    assert len(batches) > 1
    assert set(results) == {c["evidence_id"] for c in candidates}
    assert all(item["verdict"] == "acceptable" for item in results.values())


def test_php_labelled_candidates_meet_per_rule_fixture_baselines():
    import subprocess
    service_root = Path(__file__).resolve().parents[2]
    fixture = service_root / "app/tests/fixtures/design_metrics/Design.php"
    result = subprocess.run(["php", "php/bin/code-parse", "--path", str(fixture), "--output", "json"],
                            cwd=service_root, capture_output=True, text=True, check=True)
    rubric, _ = load_rubric()
    candidates, _ = select_candidates("pizza", "rev_1", build_units(json.loads(result.stdout), rubric), rubric)
    actual = {(c["rule_id"], c["symbol"].split("\\")[-1]) for c in candidates}
    labels = {("srp-lcom4", "LargeApi"), ("srp-lcom4", "SplitResponsibilities"),
              ("srp-method-count", "LargeApi"), ("ocp-type-switch", "TypeDispatcher"),
              ("isp-interface-size", "BroadContract"), ("lsp-unsupported-override", "RefusingWorker"),
              ("dip-concrete-dependencies", "ConcreteClient"), ("dip-direct-infrastructure", "InfraClient")}
    # Baselines cover candidate detection in this small fixture; model judgments remain separate.
    for rule in {rule for rule, _ in labels}:
        expected = {item for item in labels if item[0] == rule}
        observed = {item for item in actual if item[0] == rule}
        precision = len(expected & observed) / len(observed) if observed else 0
        recall = len(expected & observed) / len(expected)
        assert precision >= 1.0
        assert recall >= 1.0


def test_revision_chunk_reads_filter_identity_and_inventory(monkeypatch):
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams
    from telemetry.snapshot import Snapshot
    from code_quality.design_metrics import read_revision_chunks
    import index.llamaindex_layer as index
    commit, archive = "a" * 40, "c" * 64
    revision = "rev_" + "b" * 32
    collection = Snapshot("pizza", commit, archive).collection
    snapshot = {"project_id": "pizza", "revision_id": revision, "commit_sha": commit,
                "source_archive_sha256": archive, "state": "ready", "collection_name": collection}
    client = QdrantClient(":memory:")
    client.create_collection(collection, vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    identity = {"project_id": "pizza", "revision_id": revision, "commit_sha": commit,
                "source_archive_sha256": archive, "dirty": False, "collection": collection}
    payloads = [{**identity, "file_path": "allowed.php"}, {**identity, "file_path": "excluded.php"},
                {**identity, "file_path": "allowed.php", "dirty": True},
                {**identity, "file_path": "allowed.php", "revision_id": "rev_other"}]
    client.upsert(collection, [PointStruct(id=i, vector=[1.0, 0.0], payload=p) for i, p in enumerate(payloads)])
    monkeypatch.setattr(index.CodePropertyGraph, "_get_qdrant", lambda self: client)
    rows, partial, error = read_revision_chunks("pizza", revision, snapshot, ["allowed.php"], 100)
    assert error is None and not partial
    assert len(rows) == 1 and rows[0]["file_path"] == "allowed.php"


def test_adding_clean_unit_does_not_lower_weighted_score():
    rubric, rubric_hash = load_rubric()
    chunks = fixture_chunks()
    units = build_units(chunks, rubric)
    candidates, counts = select_candidates("pizza", "rev_1", units, rubric)
    verdicts = {c["evidence_id"]: {"verdict": "violation", "reason": "Fixture", "cache_hit": False} for c in candidates}
    before, _, _ = score_design(units, candidates, verdicts, counts, rubric, rubric_hash, False)
    chunks.append({"node_type": "class", "fq_symbol_name": "App\\Clean", "file_path": "Clean.php",
                   "start_line": 1, "loc": 100, "metric_schema": "code-metrics-1", "content_hash": "clean"})
    units = build_units(chunks, rubric)
    candidates, counts = select_candidates("pizza", "rev_1", units, rubric)
    after, _, _ = score_design(units, candidates, verdicts, counts, rubric, rubric_hash, False)
    assert after["score"] >= before["score"]


def test_judge_uses_schema_on_supported_remote_models_and_validates_json_mode():
    from code_quality.design_judge import VERDICT_SCHEMA, response_format
    strict = response_format("openai/gpt-oss-120b", "remote")
    assert strict["type"] == "json_schema"
    assert strict["json_schema"]["strict"] is True
    assert strict["json_schema"]["schema"] == VERDICT_SCHEMA
    assert "score" not in json.dumps(VERDICT_SCHEMA)
    assert response_format("llama-3.3-70b-versatile", "remote") == {"type": "json_object"}


def test_two_class_revision_delta_only_judges_changed_class(tmp_path):
    rubric, _ = load_rubric()
    one = fixture_chunks()
    two = [{**row, **{k: v.replace("Example", "Other") for k, v in row.items()
                      if k in {"parent_symbol", "fq_symbol_name", "file_path"} and isinstance(v, str)}} for row in one]
    store = ReportStore(tmp_path / "reports.sqlite")
    calls = []
    def model(prompt, **kwargs):
        batch = json.loads(prompt.split("Candidates: ", 1)[1])
        calls.append(batch)
        return json.dumps({"verdicts": [{"evidence_id": c["evidence_id"], "principle": c["principle"],
                                       "verdict": "acceptable", "reason": "Fixture."} for c in batch]}), "model", "local"
    first, _ = select_candidates("pizza", "rev_1", build_units(one + two, rubric), rubric)
    judge_candidates("pizza", first, rubric, store, "model", "local", model, 12000)
    two[0]["content_hash"] = "changed"
    second, _ = select_candidates("pizza", "rev_2", build_units(one + two, rubric), rubric)
    verdicts = judge_candidates("pizza", second, rubric, store, "model", "local", model, 12000)
    assert len(calls) == 2 and len(calls[1]) == 1
    assert calls[1][0]["card"]["symbol"] == "App\\Other"
    assert {c["symbol"]: verdicts[c["evidence_id"]]["cache_hit"] for c in second} == {"App\\Example": True, "App\\Other": False}


# ── rubric 2: ranked, budgeted judging and estimated penalties (plan Phase 5) ──

def _unit(name, loc=100, **metrics):
    base = {"method_count": 20, "loc": loc, "lcom4": 1, "type_switch": 0, "concrete_dependencies": 0,
            "unsupported_overrides": 0, "infrastructure_instantiations": 0, "max_cyclomatic": 1, "max_nesting": 1,
            "max_params": 1, "max_method_loc": 10, "fan_out": 0, "public_interface_size": 1}
    return {"declaration": {"node_type": "class", "fq_symbol_name": f"App\\{name}", "file_path": f"src/{name}.php",
                            "start_line": 1, "code": f"class {name}", "content_hash": name},
            "metrics": {**base, **metrics}, "flagged": {}, "methods": [], "properties": [], "related": []}


def _limits(rubric, **limits):
    return {**rubric, "limits": {**rubric["limits"], **limits}}


def test_candidates_are_ranked_by_severity_with_a_minimum_per_principle():
    rubric, _ = load_rubric()
    units = [_unit(f"Big{n}", method_count=16 + n, lcom4=1) for n in range(10)] + [_unit("Switchy", method_count=5, type_switch=3)]
    candidates, counts = select_candidates("p", "r", units, _limits(rubric, min_candidates_per_principle=1, max_candidates=3))
    # Each principle's most severe unit, then the next most severe overall, in severity order
    # (type switch 3/2 x 15 = 22.5 outranks method count 25/15 x 10 = 16.7).
    assert [(c["rule_id"], c["symbol"]) for c in candidates] == [
        ("ocp-type-switch", "App\\Switchy"), ("srp-method-count", "App\\Big9"), ("srp-method-count", "App\\Big8")]
    assert counts["SRP"]["exceeding"] == 10 and counts["SRP"]["unjudged"] == 8 and counts["OCP"]["unjudged"] == 0
    assert candidates[0]["severity"] > candidates[1]["severity"]


def test_the_judge_token_budget_bounds_how_many_are_judged():
    rubric, _ = load_rubric()
    units = [_unit(f"Big{n}", method_count=16 + n) for n in range(30)]
    small, _ = select_candidates("p", "r", units, _limits(rubric, judge_input_tokens=400))
    large, _ = select_candidates("p", "r", units, _limits(rubric, judge_input_tokens=100_000))
    assert 0 < len(small) < len(large) == 30


def test_unjudged_units_carry_the_judged_violation_rate_as_an_estimated_penalty():
    rubric, rubric_hash = load_rubric()
    units = [_unit(f"Big{n}", method_count=20) for n in range(4)]
    rubric = _limits(rubric, min_candidates_per_principle=1, max_candidates=2)
    candidates, counts = select_candidates("p", "r", units, rubric)
    verdicts = {candidates[0]["evidence_id"]: {"verdict": "violation", "reason": "Mixed responsibilities."},
                candidates[1]["evidence_id"]: {"verdict": "acceptable", "reason": "Cohesive."}}
    category, provenance, findings = score_design(units, candidates, verdicts, counts, rubric, rubric_hash, False)
    scores = {u["symbol"]: u for u in provenance["unit_scores"]}
    judged_bad, judged_ok = candidates[0]["symbol"], candidates[1]["symbol"]
    assert scores[judged_bad]["score"] == 90 and scores[judged_ok]["score"] == 100
    unjudged = [u for symbol, u in scores.items() if symbol not in (judged_bad, judged_ok)]
    assert all(u["score"] == 95 and u["estimated_rules"] == ["srp-method-count"] for u in unjudged)  # 10 × 0.5
    assert provenance["unjudged_estimate"]["rates"]["srp-method-count"] == 0.5 and provenance["unjudged_estimate"]["units"] == 2
    assert counts["SRP"]["estimated"] == 2 and category["assessment_status"] == "provisional"
    assert category["score"] == 95 and "estimated penalty" in category["reason"] and len(findings) == 1


def test_without_decisive_verdicts_the_default_rate_applies():
    rubric, rubric_hash = load_rubric()
    units = [_unit("Big1", method_count=20), _unit("Big2", method_count=20)]
    rubric = _limits(rubric, min_candidates_per_principle=1, max_candidates=1)
    candidates, counts = select_candidates("p", "r", units, rubric)
    verdicts = {candidates[0]["evidence_id"]: {"verdict": "uncertain", "reason": "Unclear."}}
    _, provenance, _ = score_design(units, candidates, verdicts, counts, rubric, rubric_hash, False)
    assert provenance["unjudged_estimate"]["rates"]["srp-method-count"] == rubric["unjudged_default_rate"]
