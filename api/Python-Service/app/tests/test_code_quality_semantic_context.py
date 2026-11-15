from __future__ import annotations

import io
import zipfile

from code_quality.semantic_context import MAX_CONTEXTS, retrieve_revision_context
from code_quality.service import ReportStore, assess_revision
from telemetry.snapshot import Snapshot


PROJECT = "pizza"
COMMIT = "a" * 40
REVISION = "rev_" + "b" * 32
ARCHIVE_HASH = "c" * 64
COLLECTION = Snapshot(PROJECT, COMMIT, ARCHIVE_HASH).collection


def _snapshot(**overrides):
    return {
        "project_id": PROJECT,
        "revision_id": REVISION,
        "commit_sha": COMMIT,
        "source_archive_sha256": ARCHIVE_HASH,
        "collection_name": COLLECTION,
        "state": "ready",
        **overrides,
    }


def _hit(*, project=PROJECT, commit=COMMIT, archive_hash=ARCHIVE_HASH,
         revision=REVISION, collection=COLLECTION, code="function save() {}", line=12):
    payload = {
        "project_id": project,
        "commit_sha": commit,
        "source_archive_sha256": archive_hash,
        "revision_id": revision,
        "dirty": False,
        "collection": collection,
        "file_path": "src/PasswordController.php",
        "start_line": line,
        "fq_symbol_name": "App\\PasswordController::save",
        "code": code,
    }
    return {"collection": collection, "payload": payload, "score": 0.9, "via_graph": False}


class FakeGraph:
    def __init__(self, results=None, searched=None, mode="vector_only", failure_code=None):
        self.results = results or []
        self.searched = searched if searched is not None else [COLLECTION]
        self.mode = mode
        self.failure_code = failure_code
        self.calls = []

    def hybrid_retrieve(self, **kwargs):
        self.calls.append(kwargs)
        return {"results": self.results, "searched_collections": self.searched, "mode": self.mode,
                "failure_code": self.failure_code}


def test_retrieval_is_bounded_and_pinned_to_selected_revision(monkeypatch):
    import index.llamaindex_layer as index_layer

    graph = FakeGraph([_hit(), _hit(code="function save() { return true; }")])
    monkeypatch.setattr(index_layer, "CodePropertyGraph", lambda: graph)

    summary, evidence = retrieve_revision_context(PROJECT, REVISION, _snapshot(), ["src/PasswordController.php"])

    assert summary["status"] == "available"
    assert summary["evidence_count"] <= MAX_CONTEXTS
    assert len(graph.calls) == 10
    assert all(call["collection_name"] == COLLECTION for call in graph.calls)
    assert all(call["limit"] == 1 and call["apply_rerank"] is False for call in graph.calls)
    assert all(call["graph_legs"] == ["calls"] and call["join_sibling"] is False for call in graph.calls)
    assert all(item["context_snippet"] for item in evidence)
    assert all(item["file"] == "src/PasswordController.php" and item["line"] == 12 for item in evidence)
    assert all(summary["category_search"][category]["attempted_queries"] == 2
               for category in ("security", "clean_code", "maintainability", "reliability", "performance"))
    assert all(summary["category_search"][category]["accepted_count"] > 0
               for category in ("security", "clean_code", "maintainability", "reliability", "performance"))


def test_category_searches_run_before_optional_business_rule_queries(monkeypatch):
    import index.llamaindex_layer as index_layer
    from code_quality.semantic_context import _CATEGORY_QUERIES

    graph = FakeGraph([_hit()])
    monkeypatch.setattr(index_layer, "CodePropertyGraph", lambda: graph)
    rules = [{"enabled": True, "rule_id": f"rule-{index:02d}", "category": "business_rule",
              "title": f"Rule {index}", "statement": "Use an approved password policy."}
             for index in range(20)]

    summary, evidence = retrieve_revision_context(
        PROJECT, REVISION, _snapshot(), ["src/PasswordController.php"], rules,
    )

    assert [call["query"] for call in graph.calls[:len(_CATEGORY_QUERIES)]] == [
        query for _, query in _CATEGORY_QUERIES
    ]
    assert summary["category_search"]
    assert all(summary["category_search"][category]["attempted_queries"] == 2
               for category in ("security", "clean_code", "maintainability", "reliability", "performance"))
    assert {row["category"] for row in evidence} >= {
        "security", "clean_code", "maintainability", "reliability", "performance",
    }


def test_retrieval_discards_hits_from_other_revision_or_archive(monkeypatch):
    import index.llamaindex_layer as index_layer

    graph = FakeGraph([
        _hit(revision="rev_" + "d" * 32),
        _hit(archive_hash="d" * 64),
        _hit(collection="snapshot_other_project"),
    ])
    monkeypatch.setattr(index_layer, "CodePropertyGraph", lambda: graph)

    summary, evidence = retrieve_revision_context(PROJECT, REVISION, _snapshot(), ["src/PasswordController.php"])

    assert summary["status"] == "unavailable"
    assert summary["failure_code"] == "revision_metadata_mismatch"
    assert evidence == []


def test_retrieval_reports_unavailable_without_searching_unready_snapshot(monkeypatch):
    import index.llamaindex_layer as index_layer

    graph = FakeGraph()
    monkeypatch.setattr(index_layer, "CodePropertyGraph", lambda: graph)

    summary, evidence = retrieve_revision_context(PROJECT, REVISION, _snapshot(state="indexing"), ["src/PasswordController.php"])

    assert summary["status"] == "unavailable"
    assert summary["reason"]
    assert evidence == []
    assert graph.calls == []


def test_retrieval_rejects_search_that_escaped_the_snapshot_collection(monkeypatch):
    import index.llamaindex_layer as index_layer

    graph = FakeGraph([_hit()], searched=[COLLECTION, "chunk_code_global"])
    monkeypatch.setattr(index_layer, "CodePropertyGraph", lambda: graph)

    summary, evidence = retrieve_revision_context(PROJECT, REVISION, _snapshot(), ["src/PasswordController.php"])

    assert summary["status"] == "unavailable"
    assert evidence == []


def test_retrieval_reports_vector_service_failure_as_unavailable(monkeypatch):
    import index.llamaindex_layer as index_layer

    graph = FakeGraph(searched=[COLLECTION], mode="vector_unavailable")
    monkeypatch.setattr(index_layer, "CodePropertyGraph", lambda: graph)

    summary, evidence = retrieve_revision_context(PROJECT, REVISION, _snapshot(), ["src/PasswordController.php"])

    assert summary["status"] == "unavailable"
    assert summary["failure_code"] == "qdrant_unavailable"
    assert evidence == []


def test_retrieval_distinguishes_missing_collection_from_successful_no_match(monkeypatch):
    import index.llamaindex_layer as index_layer

    missing = FakeGraph(searched=[], mode="no_results", failure_code="collection_missing")
    monkeypatch.setattr(index_layer, "CodePropertyGraph", lambda: missing)
    summary, evidence = retrieve_revision_context(PROJECT, REVISION, _snapshot(), ["src/PasswordController.php"])
    assert summary["status"] == "unavailable"
    assert summary["failure_code"] == "collection_missing"
    assert "re-index" in summary["reason"]
    assert evidence == []

    empty = FakeGraph()
    monkeypatch.setattr(index_layer, "CodePropertyGraph", lambda: empty)
    summary, evidence = retrieve_revision_context(PROJECT, REVISION, _snapshot(), ["src/PasswordController.php"])
    assert summary["status"] == "available_no_matches"
    assert summary["failure_code"] == "no_matching_chunks"
    assert summary["searched_collection_count"] == 1
    assert evidence == []


def test_retrieval_reports_revision_metadata_mismatch(monkeypatch):
    import index.llamaindex_layer as index_layer

    graph = FakeGraph([_hit(revision="rev_" + "d" * 32)])
    monkeypatch.setattr(index_layer, "CodePropertyGraph", lambda: graph)

    summary, evidence = retrieve_revision_context(PROJECT, REVISION, _snapshot(), ["src/PasswordController.php"])

    assert summary["status"] == "unavailable"
    assert summary["failure_code"] == "revision_metadata_mismatch"
    assert summary["candidate_count"] >= 1
    assert summary["candidate_count"] == summary["rejected_revision_count"]
    assert evidence == []


def test_retrieval_only_returns_files_from_business_inventory(monkeypatch):
    import index.llamaindex_layer as index_layer

    vendor = _hit()
    vendor["payload"]["file_path"] = "vendor/library/PasswordController.php"
    graph = FakeGraph([vendor, _hit()])
    monkeypatch.setattr(index_layer, "CodePropertyGraph", lambda: graph)

    summary, evidence = retrieve_revision_context(
        PROJECT, REVISION, _snapshot(), ["src/PasswordController.php"]
    )

    assert summary["status"] == "available"
    assert evidence
    assert all(item["file"] == "src/PasswordController.php" for item in evidence)


def test_retrieval_skips_queries_when_inventory_has_no_business_files(monkeypatch):
    import index.llamaindex_layer as index_layer

    graph = FakeGraph([_hit()])
    monkeypatch.setattr(index_layer, "CodePropertyGraph", lambda: graph)

    summary, evidence = retrieve_revision_context(PROJECT, REVISION, _snapshot(), [])

    assert summary["status"] == "available_no_matches"
    assert evidence == []
    assert graph.calls == []


def test_assessment_sends_snippets_to_model_but_does_not_persist_source(monkeypatch, tmp_path):
    import code_quality.rules as rules
    import code_quality.service as service

    source = "<?php class PasswordController { public function save() {} }"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("src/PasswordController.php", source)
    archive_data = buffer.getvalue()

    monkeypatch.setattr(service, "inventory_archive", lambda _archive, _exclusions=None: (
        {"php": 1}, [], ["src/PasswordController.php"],
        {"detected_files": 1, "supported_files": 1, "scanned_files": 1,
         "skipped_files": 0, "excluded_files": 0, "scanned_bytes": len(source),
         "unsupported_extensions": {}}, [],
    ))
    monkeypatch.setattr(rules, "CustomRuleStore", lambda: type("Rules", (), {"enabled": lambda self, _user: []})())
    monkeypatch.setattr(service, "_run_static_rules", lambda *_args: ([], [], None))
    model_input = {}

    def score(evidence, coverage, languages, scan_stats, analyzer_failures, *_args):
        model_input["evidence"] = evidence
        model_input["coverage"] = coverage
        categories = {
            category: {"assessed": True, "score": 60, "reason": f"Reviewed {category} source context.",
                       "evidence_ids": [next(row["evidence_id"] for row in evidence
                                             if row.get("category") == category and row.get("kind") != "coverage")]}
            for category in service.CATEGORIES
        }
        return categories, "model", "local", {"total_tokens": 1}, [], []

    monkeypatch.setattr(service, "_score", score)
    snippet = "public function save(string $password): void { $this->validator->validate($password); }"
    summary = {"status": "available", "revision_id": REVISION, "retriever_version": "revision-context-1",
               "query_count": 5, "evidence_count": 1, "reason": None}
    semantic_evidence = [{"evidence_id": "ev_context", "kind": "semantic_context", "category": "security",
                          "rule": "revision-context-1", "file": "src/PasswordController.php", "line": 12,
                          "message": "Revision-matched vector match.", "context_snippet": snippet}]
    import code_quality.semantic_context as semantic_context
    monkeypatch.setattr(semantic_context, "retrieve_revision_context", lambda *_args: (summary, semantic_evidence))
    store = ReportStore(tmp_path / "reports.sqlite")

    report = assess_revision(PROJECT, REVISION, _snapshot(), archive_data, "alice", store)

    assert any(item.get("context_snippet") == snippet for item in model_input["evidence"])
    assert model_input["coverage"]["security"]["evidence_count"] >= 1
    assert report["coverage"]["semantic_context"] == summary
    saved_context = next(item for item in report["evidence"] if item["evidence_id"] == "ev_context")
    assert "context_snippet" not in saved_context


def test_assessment_persists_all_five_cited_scores_and_equal_weight_overall(monkeypatch, tmp_path):
    import code_quality.external_analyzers as external
    import code_quality.flow_tracing as flow_tracing
    import code_quality.outline as outline
    import code_quality.rules as rules
    import code_quality.service as service

    monkeypatch.setattr(service, "inventory_archive", lambda *_args: (
        {"python": 1}, [], ["app/domain.py"],
        {"detected_files": 1, "supported_files": 1, "scanned_files": 1,
         "skipped_files": 0, "excluded_files": 0, "scanned_bytes": 100,
         "unsupported_extensions": {}}, [],
    ))
    monkeypatch.setattr(outline, "OutlineConnectionStore", lambda: type("Store", (), {"get": lambda *_args: None})())
    monkeypatch.setattr(external, "ExternalAnalyzerSettingsStore", lambda: type("Store", (), {"get": lambda *_args: {}})())
    monkeypatch.setattr(external, "effective_worker_target", lambda _settings: None)
    monkeypatch.setattr(external, "deployment_mode", lambda: "production")
    monkeypatch.setattr(external, "developer_mode_enabled", lambda: False)
    monkeypatch.setattr(rules, "CustomRuleStore", lambda: type("Store", (), {"enabled": lambda *_args: []})())
    monkeypatch.setattr(service, "_run_static_rules", lambda *_args: ([], [], None))
    monkeypatch.setattr(flow_tracing, "retrieve_revision_call_traces", lambda *_args: (
        {"status": "unavailable", "revision_id": REVISION, "tracer_version": "test",
         "flow_types": [], "node_count": 0, "edge_count": 0, "trace_count": 0,
         "unresolved_edge_count": 0, "reason": "No trace fixture."}, []
    ))
    semantic_summary = {"status": "available", "revision_id": REVISION,
                        "retriever_version": "test", "query_count": 10,
                        "evidence_count": 5, "reason": None,
                        "category_search": {category: {"attempted_queries": 2, "candidate_count": 2,
                                                       "accepted_count": 1, "rejected_revision_count": 0,
                                                       "rejected_source_count": 0}
                                            for category in ("security", "clean_code", "maintainability",
                                                             "reliability", "performance")}}
    semantic_evidence = [
        {"evidence_id": f"ev_{category}", "kind": "semantic_context", "category": category,
         "rule": "test-context", "file": "app/domain.py", "line": index + 1,
         "message": "Revision-matched fixture evidence.", "context_snippet": f"evidence for {category}"}
        for index, category in enumerate(("security", "clean_code", "maintainability", "reliability", "performance"))
    ]
    monkeypatch.setattr("code_quality.semantic_context.retrieve_revision_context",
                        lambda *_args: (semantic_summary, semantic_evidence))
    values = dict(zip(("security", "clean_code", "maintainability", "reliability", "performance"),
                      (90, 80, 70, 60, 50)))

    def score(_evidence, _coverage, _languages, _stats, _failures, *_args):
        return ({category: {"assessed": True, "score": score, "reason": "Cited evidence supports score.",
                            "evidence_ids": [f"ev_{category}"]}
                 for category, score in values.items()}, "test-model", "test-provider",
                {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
                 "estimated": False, "source": "provider"}, [], [])

    monkeypatch.setattr(service, "_score", score)

    report = assess_revision(PROJECT, REVISION, _snapshot(), b"archive", "alice",
                             ReportStore(tmp_path / "reports.sqlite"))

    assert all(report["categories"][category]["assessed"] for category in values)
    assert all(report["categories"][category]["evidence_ids"] == [f"ev_{category}"] for category in values)
    assert all(report["shadow_scoring"]["categories"][category]["deterministic_evidence_ids"] for category in values)
    assert report["shadow_scoring"]["status"] == "experimental"
    assert report["overall_score"] == 70
    assert report["coverage"]["semantic_context"]["category_search"] == semantic_summary["category_search"]
