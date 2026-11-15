import asyncio
import json
import io
import sys
from types import SimpleNamespace
import zipfile

import pytest

import runtime.llm as llm
from code_quality.service import AssessmentError, CATEGORIES, ReportStore, _finding, _score, inventory_archive, model_failure_diagnostic
from code_quality.deterministic_scoring import SCORING_VERSION, score_categories
from code_quality.source_view import SourceViewError, read_source_window


def _zip_sources(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


def test_normalized_findings_include_the_evidence_kind():
    finding = _finding("app/Controller.php", 12, "unsafe-call", "security", "Review this call.")

    assert finding["kind"] == "finding"


def test_model_failure_diagnostic_preserves_safe_provider_request_message():
    error = type("BadRequestError", (), {
        "status_code": 400,
        "body": {"message": "response_format requires the word json"},
    })()

    category, status, detail = model_failure_diagnostic(error)

    assert category == "request_rejected"
    assert status == 400
    assert "response_format requires the word json" in detail


def test_inventory_scans_supported_sources_in_dot_directories_and_keeps_artifact_exclusions():
    languages, _, files, stats, _ = inventory_archive(_zip_sources({
        ".hivemind/aidd/skills/aidd-sync/scripts/setup-symlinks.sh": "#!/bin/sh\necho ready\n",
        ".cache/generated.py": "print('generated')\n",
    }))

    assert ".hivemind/aidd/skills/aidd-sync/scripts/setup-symlinks.sh" in files
    assert languages == {"shell": 1}
    assert stats["excluded_files"] == 1


def test_source_view_returns_bounded_line_numbered_window_and_highlights_cited_line():
    content = "\n".join(f"line {number}" for number in range(1, 181))
    result = read_source_window(_zip_sources({"app/Controller.php": content}), "app/Controller.php", 90)

    assert result["file"] == "app/Controller.php"
    assert result["requested_line"] == 90
    assert result["start_line"] == 55
    assert result["end_line"] == 154
    assert result["has_previous"] is True
    assert result["has_next"] is True
    assert len(result["lines"]) == 100
    assert result["lines"][35] == {"number": 90, "text": "line 90"}


@pytest.mark.parametrize("file_path", ["../secret.php", "/etc/passwd.php", "vendor/pkg/file.php", "assets/app.min.js"])
def test_source_view_rejects_unsafe_or_non_business_source_paths(file_path):
    archive = _zip_sources({file_path: "contents"})

    with pytest.raises(SourceViewError):
        read_source_window(archive, file_path, 1)


def test_source_view_endpoint_loads_only_the_requested_ready_revision(monkeypatch):
    from starlette.requests import Request

    import code_quality.http as http

    project_id = "pizza"
    revision_id = "rev_0123456789abcdef0123456789abcdef"
    commit_sha = "a" * 40
    archive = _zip_sources({"app/Controller.php": "<?php\nreturn true;\n"})
    registry_calls = []

    class Registry:
        def get_by_revision_id(self, project, revision):
            registry_calls.append((project, revision))
            return {"state": "ready", "project_id": project_id, "commit_sha": commit_sha,
                    "source_archive_sha256": "c" * 64}

        def archive_key(self, project, commit):
            assert (project, commit) == (project_id, commit_sha)
            return "immutable-archive"

    monkeypatch.setenv("RAG_CALLBACK_SECRET", "x" * 40)
    monkeypatch.setattr("telemetry.registry.default_registry", lambda: Registry())
    monkeypatch.setitem(sys.modules, "index.storage", SimpleNamespace(
        storage=SimpleNamespace(get_snapshot_archive=lambda key: archive),
    ))
    request = Request({
        "type": "http", "method": "GET", "path": "/source",
        "headers": [(b"x-internal-secret", b"x" * 40), (b"x-user-id", b"alice")],
    })

    result = asyncio.run(http.source_window(project_id, revision_id, request, "app/Controller.php", 2, None))

    assert registry_calls == [(project_id, revision_id)]
    assert result["requested_line"] == 2
    assert result["lines"][-1] == {"number": 2, "text": "return true;"}


def test_report_store_deletes_only_the_requested_report_in_the_requested_project(tmp_path):
    store = ReportStore(tmp_path / "reports.db")
    first = {"report_id": "report-a", "project_id": "pizza", "revision_id": "rev_a", "created_at": "1"}
    second = {"report_id": "report-b", "project_id": "pizza", "revision_id": "rev_b", "created_at": "2"}
    other_project = {"report_id": "report-c", "project_id": "other", "revision_id": "rev_c", "created_at": "3"}
    for report in (first, second, other_project):
        store.save_report(report)

    assert store.delete_report("pizza", "report-a") is True
    assert store.delete_report("pizza", "report-a") is False
    assert store.get_report("pizza", "report-a") is None
    assert store.get_report("pizza", "report-b") == second
    assert store.get_report("other", "report-c") == other_project


def test_delete_report_endpoint_requires_internal_auth_and_hides_other_project_reports(monkeypatch, tmp_path):
    from fastapi import HTTPException
    from starlette.requests import Request

    import code_quality.http as http

    store = ReportStore(tmp_path / "reports.db")
    store.save_report({"report_id": "report-a", "project_id": "pizza", "revision_id": "rev_a", "created_at": "1"})
    monkeypatch.setattr(http, "ReportStore", lambda: store)
    monkeypatch.setenv("RAG_CALLBACK_SECRET", "x" * 40)
    request = Request({
        "type": "http", "method": "DELETE", "path": "/",
        "headers": [(b"x-internal-secret", b"x" * 40), (b"x-user-id", b"alice")],
    })

    assert http.delete_report("pizza", "report-a", request) == {"deleted": True}
    with pytest.raises(HTTPException) as missing:
        http.delete_report("other", "report-a", request)
    assert missing.value.status_code == 404


@pytest.mark.parametrize(
    ("remote_available", "expected_provider"),
    [(True, "remote"), (False, "local")],
)
def test_score_uses_configured_provider(monkeypatch, remote_available, expected_provider):
    selected = {}
    response = {
        "categories": {
            category: {
                "assessed": True,
                "score": 60,
                "reason": f"Reviewed source sample for {category}.",
                "evidence_ids": [f"ev_{category}"],
            }
            for category in CATEGORIES
        },
        "clean_code_findings": [],
    }

    def complete_with_model(_prompt, **kwargs):
        selected["provider"] = kwargs["provider"]
        selected.update(kwargs)
        return json.dumps(response), "test-model", kwargs["provider"]

    monkeypatch.setattr(llm, "remote_configured", lambda: remote_available)
    monkeypatch.setattr(llm, "complete_with_model", complete_with_model)
    evidence = [
        {"evidence_id": f"ev_{category}", "kind": "semantic_context", "category": category,
         "rule": "local-category-context-1", "file": f"src/{category}.php", "line": 1,
         "message": "Revision-pinned source sample.", "context_snippet": "class Example {}"}
        for category in CATEGORIES
    ]

    _score(
        evidence=evidence,
        coverage={category: {"supported": True, "reason": None} for category in CATEGORIES},
        languages={"php": 1},
        scan_stats={"scanned_files": 1, "unsupported_extensions": {}},
        analyzer_failures=[],
    )

    assert selected["provider"] == expected_provider
    assert selected["max_tokens"] == 3072
    assert selected["timeout"] == 120
    assert selected["fallback_to_local"] is False
    assert selected["response_format"] == {"type": "json_object"}


def test_score_assesses_every_category_with_cited_category_evidence(monkeypatch):
    evidence = [
        {"evidence_id": f"ev_{category}", "kind": "finding", "category": category,
         "rule": f"rule-{category}", "file": f"src/{category}.php", "line": 10,
         "message": f"Concrete {category} evidence."}
        for category in CATEGORIES
    ]
    response_categories = {
        category: {"assessed": True, "score": 70, "reason": f"Evidence supports {category}.",
                   "evidence_ids": [f"ev_{category}"]}
        for category in CATEGORIES
    }
    request = {}

    def complete_with_model(prompt, **kwargs):
        request["prompt"] = prompt
        request.update(kwargs)
        return json.dumps({"categories": response_categories, "clean_code_findings": []}), "test-model", "remote"

    monkeypatch.setattr(llm, "remote_configured", lambda: True)
    monkeypatch.setattr(llm, "complete_with_model", complete_with_model)

    categories, *_ = _score(
        evidence=evidence,
        coverage={category: {"supported": True, "reason": None} for category in CATEGORIES},
        languages={"php": 5},
        scan_stats={"scanned_files": 5, "unsupported_extensions": {}},
        analyzer_failures=[],
    )

    sent_context = json.loads(request["prompt"].split("Evidence: ", 1)[1])
    assert {item["category"] for item in sent_context["evidence"]} == set(CATEGORIES)
    assert "json" in request["prompt"]
    assert all(categories[category]["assessed"] for category in CATEGORIES)
    assert all(categories[category]["evidence_ids"] == [f"ev_{category}"] for category in CATEGORIES)
    assert request["max_tokens"] == 3072


def test_score_keeps_category_unassessed_without_valid_category_citation(monkeypatch):
    response_categories = {
        category: {"assessed": True, "score": 70, "reason": "Evidence supports this score.",
                   "evidence_ids": ["ev_security"]}
        for category in CATEGORIES
    }

    def complete_with_model(_prompt, **_kwargs):
        return json.dumps({"categories": response_categories, "clean_code_findings": []}), "test-model", "remote"

    monkeypatch.setattr(llm, "remote_configured", lambda: True)
    monkeypatch.setattr(llm, "complete_with_model", complete_with_model)
    evidence = [
        {"evidence_id": f"ev_{category}", "kind": "semantic_context", "category": category,
         "rule": "local-category-context-1", "file": f"src/{category}.php", "line": 10,
         "message": f"Concrete {category} review context.", "context_snippet": f"class {category.title()} {{}}"}
        for category in CATEGORIES
    ]

    with pytest.raises(AssessmentError, match="did not return evidence-cited scores for all five categories"):
        _score(
            evidence=evidence,
            coverage={category: {"supported": True, "reason": None} for category in CATEGORIES},
            languages={"php": 1},
            scan_stats={"scanned_files": 1, "unsupported_extensions": {}},
            analyzer_failures=[],
        )


def test_score_explains_clean_code_context_without_claiming_a_finding(monkeypatch):
    evidence = [
        {"evidence_id": f"ev_{category}", "kind": "semantic_context",
         "category": category, "rule": "local-category-context-1",
         "file": f"src/{category}.php", "line": 20,
         "message": "Source excerpt supplied as AI review context.",
         "context_snippet": "class Service { public function run(): void {} }"}
        for category in CATEGORIES
    ]
    response_categories = {
        category: {"assessed": True, "score": 70,
                   "reason": f"Review sample supports {category} score.",
                   "evidence_ids": [f"ev_{category}"]}
        for category in CATEGORIES
    }

    def complete_with_model(_prompt, **_kwargs):
        return json.dumps({"categories": response_categories, "clean_code_findings": []}), "test-model", "remote"

    monkeypatch.setattr(llm, "remote_configured", lambda: True)
    monkeypatch.setattr(llm, "complete_with_model", complete_with_model)
    categories, _model, _provider, _usage, _reviews, findings = _score(
        evidence=evidence,
        coverage={category: {"supported": True, "reason": None} for category in CATEGORIES},
        languages={"php": 1},
        scan_stats={"scanned_files": 1, "unsupported_extensions": {}},
        analyzer_failures=[],
    )

    assert findings == []
    assert categories["clean_code"]["reason"] == (
        "The cited source excerpts supported a Clean Code score, but the evaluator "
        "did not substantiate a specific Clean Code issue."
    )


def test_deterministic_score_ignores_context_and_uses_external_findings_for_official_score():
    evidence = [
        {"evidence_id": "ev-clean-context", "kind": "semantic_context", "category": "clean_code",
         "rule": "local-category-context-1", "file": "src/Service.php", "line": 10,
         "message": "Context only."},
        {"evidence_id": "ev-maintainability-sonar", "kind": "finding", "category": "maintainability",
         "rule": "sonarqube:php:S3776", "file": "src/Cart.php", "line": 24,
         "message": "Cognitive complexity is high.", "severity": "critical", "tool": "sonarqube"},
        {"evidence_id": "ev-coverage-clean", "kind": "coverage", "category": "clean_code",
         "rule": "bounded-scan-coverage", "file": None, "line": None, "message": "Complete scan."},
        *[
            {"evidence_id": f"ev-coverage-{category}", "kind": "coverage", "category": category,
             "rule": "bounded-scan-coverage", "file": None, "line": None, "message": "Complete scan."}
            for category in ("security", "maintainability", "reliability", "performance")
        ],
    ]
    coverage = {category: {"supported": True, "status": "covered"} for category in CATEGORIES}

    result = score_categories(evidence, coverage)

    assert result["clean_code"]["score"] == 100
    assert result["clean_code"]["finding_count"] == 0
    assert result["clean_code"]["evidence_ids"] == ["ev-coverage-clean"]
    assert result["clean_code"]["score_method"] == SCORING_VERSION
    assert result["maintainability"]["score"] < 100
    assert result["maintainability"]["evidence_ids"] == ["ev-maintainability-sonar", "ev-coverage-maintainability"]


def test_deterministic_score_marks_partial_coverage_provisional():
    evidence = [
        {"evidence_id": "ev-coverage", "kind": "coverage", "category": "performance",
         "rule": "bounded-scan-coverage", "file": None, "line": None, "message": "Partial scan."},
    ]

    result = score_categories(
        evidence,
        {category: {"supported": True, "status": "partial"} for category in CATEGORIES},
        analyzer_failures=[{"name": "semgrep", "reason": "Timed out."}],
    )

    assert result["performance"]["assessed"] is True
    assert result["performance"]["assessment_status"] == "provisional"
    assert result["performance"]["confidence"] == "low"
    assert result["performance"]["score"] == 100
    assert result["performance"]["confidence_reasons"]


def test_deterministic_score_marks_unsupported_category_unavailable():
    result = score_categories(
        [],
        {category: {"supported": category != "performance", "status": "covered" if category != "performance" else "unavailable",
                    "reason": "No performance analyzer."}
         for category in CATEGORIES},
    )

    assert result["performance"]["assessed"] is False
    assert result["performance"]["score"] is None
    assert result["performance"]["assessment_status"] == "unavailable"


def test_local_category_context_builds_revision_scoped_samples_for_each_category():
    from code_quality.category_context import build_category_context

    source = """<?php
class SampleService {
    public function run($input) {
        try {
            foreach ($input as $item) {
                $this->db->query($item);
            }
        } catch (\\Throwable $error) {
            throw $error;
        }
    }
}
"""
    summary, evidence = build_category_context(
        "pizza", "rev_0123456789abcdef0123456789abcdef",
        _zip_sources({"src/SampleService.php": source}),
        ["src/SampleService.php", "../outside.php"], CATEGORIES,
        max_file_bytes=2 * 1024 * 1024, max_scan_bytes=48 * 1024 * 1024,
    )

    assert summary["status"] == "available"
    assert summary["excerpt_count"] == len(CATEGORIES)
    assert {item["category"] for item in evidence} == set(CATEGORIES)
    assert len({item["evidence_id"] for item in evidence}) == len(CATEGORIES)
    assert all(item["file"] == "src/SampleService.php" for item in evidence)
    assert all("not a finding" in item["message"] for item in evidence)
    assert all(item["context_snippet"] for item in evidence)
    repeated_summary, repeated_evidence = build_category_context(
        "pizza", "rev_0123456789abcdef0123456789abcdef",
        _zip_sources({"src/SampleService.php": source}),
        ["src/SampleService.php"], CATEGORIES,
        max_file_bytes=2 * 1024 * 1024, max_scan_bytes=48 * 1024 * 1024,
    )
    assert repeated_summary == summary
    assert repeated_evidence == evidence


def test_score_fits_configured_context_window_while_preserving_category_evidence(monkeypatch):
    import code_quality.service as service

    monkeypatch.setattr(service.settings, "llm_context_window_tokens", 20000)
    monkeypatch.setattr(service.settings, "assessment_request_token_limit", 0)
    evidence = [
        {"evidence_id": f"required_{category}", "kind": "finding", "category": category,
         "rule": f"rule-{category}", "file": f"src/{category}.php", "line": 1,
         "message": "Concrete category evidence."}
        for category in CATEGORIES
    ]
    evidence.extend(
        {"evidence_id": f"optional_{index}", "kind": "finding", "category": "maintainability",
         "rule": "extra-signal", "file": f"src/Extra{index}.php", "line": 2,
         "message": "Optional evidence " + ("detail " * 20)}
        for index in range(80)
    )
    response = {"categories": {
        category: {"assessed": True, "score": 70, "reason": "Supported by direct evidence.",
                   "evidence_ids": [f"required_{category}"]}
        for category in CATEGORIES
    }, "clean_code_findings": []}
    captured = {}

    def complete_with_model(prompt, **kwargs):
        if prompt.startswith("Summarize each supplied evidence item"):
            rows = json.loads(prompt.split("Evidence: ", 1)[1])
            return json.dumps({"items": [{"evidence_id": row["evidence_id"], "summary": "Evidence summary."} for row in rows]}), "test-model", "remote"
        captured["prompt"] = prompt
        captured.update(kwargs)
        return json.dumps(response), "test-model", "remote"

    monkeypatch.setattr(llm, "remote_configured", lambda: True)
    monkeypatch.setattr(llm, "complete_with_model", complete_with_model)
    coverage = {category: {"supported": True, "reason": None} for category in CATEGORIES}

    categories, _model, _provider, usage, *_ = _score(
        evidence=evidence,
        coverage=coverage,
        languages={"php": 40},
        scan_stats={"scanned_files": 40, "unsupported_extensions": {}},
        analyzer_failures=[],
    )

    context = json.loads(captured["prompt"].split("Evidence: ", 1)[1])
    included_ids = {item["evidence_id"] for item in context["evidence"]}
    from code_quality.assessment_budget import request_token_bound

    model = service.settings.llm_remote_model or service.settings.ollama_llm_model
    estimated_input_tokens = request_token_bound(
        "You are a cautious code quality evaluator. Follow the response schema exactly.", captured["prompt"], model,
    )
    assert {f"required_{category}" for category in CATEGORIES} <= included_ids
    assert {f"optional_{index}" for index in range(80)} <= included_ids
    assert context["omitted_evidence_count"] == 0
    assert estimated_input_tokens + captured["max_tokens"] + 2000 <= 20000
    assert usage["batch_count"] > 0
    assert usage["summarized_evidence_count"] > 0
    assert all(categories[category]["assessed"] for category in CATEGORIES)


def test_score_stops_before_model_call_when_required_context_exceeds_configured_window(monkeypatch):
    import code_quality.service as service

    monkeypatch.setattr(service.settings, "llm_context_window_tokens", 3500)
    monkeypatch.setattr(llm, "remote_configured", lambda: True)
    model_called = False

    def complete_with_model(*_args, **_kwargs):
        nonlocal model_called
        model_called = True
        return "{}", "test-model", "remote"

    monkeypatch.setattr(llm, "complete_with_model", complete_with_model)
    evidence = [
        {"evidence_id": f"required_{category}", "kind": "finding", "category": category,
         "rule": f"rule-{category}", "file": f"src/{category}.php", "line": 1,
         "message": "Concrete category evidence."}
        for category in CATEGORIES
    ]

    with pytest.raises(AssessmentError, match="configured model context window") as error:
        _score(
            evidence=evidence,
            coverage={category: {"supported": True, "reason": None} for category in CATEGORIES},
            languages={"php": 5},
            scan_stats={"scanned_files": 5, "unsupported_extensions": {}},
            analyzer_failures=[],
        )
    assert "Settings → Models and providers → Assessment request token limit" in str(error.value)
    assert "model context window also limits" in str(error.value)

    assert model_called is False


def test_overflow_batch_rejects_unknown_citations():
    from code_quality.assessment_batches import summarize_overflow

    def complete(*_args, **_kwargs):
        return json.dumps({"items": [{"evidence_id": "invented", "summary": "Unsupported."}]}), "model", "remote"

    with pytest.raises(ValueError, match="invalid evidence batch summaries"):
        summarize_overflow(
            [{"evidence_id": "original", "category": "security", "kind": "finding"}],
            model="unknown", context_limit=0, complete=complete,
            usage_for=lambda *_args: {"total_tokens": 0},
        )


def test_rate_limit_413_is_distinguished_from_context_limit():
    error = type("APIStatusError", (), {
        "status_code": 413,
        "body": {"code": "rate_limit_exceeded", "message": "Request too large for tokens per minute"},
    })()
    category, status, detail = model_failure_diagnostic(error)
    assert (category, status) == ("request_rate_limit", 413)
    assert "token-per-minute" in detail
