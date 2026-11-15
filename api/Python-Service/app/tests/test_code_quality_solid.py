import json
from types import SimpleNamespace

import pytest

from code_quality.php_design import (
    build_php_assessment_evidence_from_archive,
    build_php_design_signals,
)
from telemetry.snapshot import Snapshot


PROJECT = "pizza"
COMMIT = "a" * 40
REVISION = "rev_" + "b" * 32
ARCHIVE_HASH = "c" * 64
COLLECTION = Snapshot(PROJECT, COMMIT, ARCHIVE_HASH).collection
BUSINESS_FILES = [
    "src/Domain/PaymentCoordinator.php",
    "src/Contracts/Exporter.php",
    "src/Export/CsvExporter.php",
]


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


def _chunk(node_type, file, line, code, **overrides):
    return {
        "project_id": PROJECT,
        "revision_id": REVISION,
        "commit_sha": COMMIT,
        "source_archive_sha256": ARCHIVE_HASH,
        "dirty": False,
        "collection": COLLECTION,
        "node_type": node_type,
        "file_path": file,
        "start_line": line,
        "end_line": line + 4,
        "code": code,
        **overrides,
    }


def _solid_fixture():
    chunks = [
        _chunk("class", BUSINESS_FILES[0], 1, "class PaymentCoordinator {}",
               class_fqn="App\\Domain\\PaymentCoordinator"),
        _chunk("interface", BUSINESS_FILES[1], 1, "interface Exporter { public function export(); }",
               class_fqn="App\\Contracts\\Exporter"),
        _chunk("method", BUSINESS_FILES[1], 2, "public function export();",
               parent_symbol="App\\Contracts\\Exporter", method_name="export"),
        _chunk("class", BUSINESS_FILES[2], 1, "class CsvExporter implements Exporter {}",
               class_fqn="App\\Export\\CsvExporter", implements=["Exporter"],
               imports=["App\\Contracts\\Exporter"]),
        _chunk("method", BUSINESS_FILES[2], 5, "public function export() { throw new \\BadMethodCallException(); }",
               parent_symbol="App\\Export\\CsvExporter", method_name="export"),
    ]
    for number in range(15):
        code = "public function run() {}"
        if number == 0:
            code = "public function run() { $db = new \\PDO($dsn); if ($x instanceof A) {} elseif ($x instanceof B) {} elseif ($x instanceof C) {} }"
        chunks.append(_chunk(
            "method", BUSINESS_FILES[0], number + 2, code,
            parent_symbol="App\\Domain\\PaymentCoordinator", method_name=f"run{number}",
        ))
    for chunk in chunks:
        chunk["metric_schema"] = "code-metrics-1"
        if chunk["node_type"] in {"class", "interface"}:
            chunk["fq_symbol_name"] = chunk["class_fqn"]
        if chunk.get("method_name") == "run0":
            chunk["type_checks"] = 3
            chunk["instantiates"] = ["PDO"]
        if chunk.get("method_name") == "export" and chunk["file_path"] == BUSINESS_FILES[2]:
            chunk["throws"] = ["BadMethodCallException"]
    return chunks


def test_php_design_signals_are_cited_deterministic_and_limited_to_supported_patterns():
    chunks = _solid_fixture()

    summary, findings = build_php_design_signals(
        PROJECT, REVISION, _snapshot(), chunks, BUSINESS_FILES,
    )
    repeated_summary, repeated_findings = build_php_design_signals(
        PROJECT, REVISION, _snapshot(), chunks, BUSINESS_FILES,
    )

    assert summary == repeated_summary
    assert findings == repeated_findings
    assert summary["status"] == "complete"
    assert summary["principles_with_static_signals"] == ["DIP", "ISP", "LSP", "OCP", "SRP"]
    assert {finding["design_principle"] for finding in findings} == {"SRP", "OCP", "LSP", "DIP"}
    assert all(finding["category"] == "clean_code" for finding in findings)
    assert all(finding["kind"] == "finding" for finding in findings)
    assert all(finding["file"] in BUSINESS_FILES and finding["line"] > 0 for finding in findings)
    assert all(finding["evidence_id"].startswith("ev_") for finding in findings)
    assert all(len(finding["context_snippet"]) <= 1_400 for finding in findings)


def test_php_design_signals_discard_wrong_revision_and_non_business_paths():
    chunks = _solid_fixture()
    chunks.append(_chunk(
        "method", "vendor/acme/Secret.php", 1, "new \\PDO();",
        parent_symbol="App\\Domain\\Hidden", method_name="run",
    ))
    chunks.append(_chunk(
        "method", BUSINESS_FILES[0], 40, "new \\PDO();",
        parent_symbol="App\\Domain\\Stale", method_name="run",
        revision_id="rev_" + "d" * 32,
    ))

    _, findings = build_php_design_signals(
        PROJECT, REVISION, _snapshot(), chunks, BUSINESS_FILES,
    )

    assert all(finding["file"] in BUSINESS_FILES for finding in findings)
    assert not any(finding["file"] == BUSINESS_FILES[0] and finding["line"] == 40 for finding in findings)


def test_php_design_signals_report_unavailable_for_another_language():
    summary, findings = build_php_design_signals(
        PROJECT, REVISION, _snapshot(), [], ["src/main.py"],
    )

    assert summary["status"] == "unavailable"
    assert findings == []


def test_php_design_signals_report_missing_index_chunks_as_unavailable():
    summary, findings = build_php_design_signals(
        PROJECT, REVISION, _snapshot(), [], BUSINESS_FILES,
    )

    assert summary["status"] == "unavailable"
    assert "no matching PHP AST chunks" in summary["reason"]
    assert findings == []


def test_php_design_signals_fall_back_to_bounded_local_parser(monkeypatch):
    import infra.php_cli as php_cli
    import infra.php_tools as php_tools

    class FakePhpCli:
        def run(self, name, args, *, timeout):
            assert name == "code-parse"
            assert args[-2:] == ["--output", "json"]
            assert timeout == 90
            return SimpleNamespace(returncode=0, stdout=json.dumps(_solid_fixture()))

    monkeypatch.setattr(php_cli, "PhpCli", FakePhpCli)
    monkeypatch.setattr(php_tools, "extract_zip_safely", lambda _archive, _workspace: 1)

    summary, findings, review_context = build_php_assessment_evidence_from_archive(
        PROJECT, REVISION, _snapshot(), b"source archive", BUSINESS_FILES,
    )

    assert summary["source"] == "local-php-ast-fallback"
    assert summary["status"] == "complete"
    assert summary["signal_count"] > 0
    assert {finding["design_principle"] for finding in findings} == {"SRP", "OCP", "LSP", "DIP"}
    assert all(finding["file"] in BUSINESS_FILES for finding in findings)
    assert review_context
    assert len(review_context) <= 8
    assert all(item["kind"] == "semantic_context" and item["category"] == "clean_code"
               for item in review_context)
    assert all(item["file"] in BUSINESS_FILES and item["context_snippet"]
               for item in review_context)
    assert len({item["evidence_id"] for item in review_context}) == len(review_context)


@pytest.mark.parametrize(
    ("snapshot", "returncode", "stdout"),
    [
        (_snapshot(source_archive_sha256=None), 0, "[]"),
        (_snapshot(), 1, ""),
        (_snapshot(), 0, "[]"),
    ],
    ids=["invalid-source-identity", "parser-failure", "empty-parse"],
)
def test_php_design_fallback_failure_always_returns_three_values(
    monkeypatch, snapshot, returncode, stdout,
):
    import infra.php_cli as php_cli
    import infra.php_tools as php_tools

    class FakePhpCli:
        def run(self, _name, _args, *, timeout):
            assert timeout == 90
            return SimpleNamespace(returncode=returncode, stdout=stdout)

    monkeypatch.setattr(php_cli, "PhpCli", FakePhpCli)
    monkeypatch.setattr(php_tools, "extract_zip_safely", lambda _archive, _workspace: None)

    result = build_php_assessment_evidence_from_archive(
        PROJECT, REVISION, snapshot, b"source archive", BUSINESS_FILES,
    )

    assert len(result) == 3
    summary, findings, review_context = result
    assert summary["status"] == "unavailable"
    assert findings == []
    assert review_context == []


def test_php_ai_review_context_is_deterministic_and_filters_non_business_files():
    from code_quality.php_design import _build_ai_review_context

    payloads = [
        _chunk("method", BUSINESS_FILES[0], 10, "public function process() { " + ("if ($a) { return $b; } " * 8) + "}",
               parent_symbol="App\\Domain\\PaymentCoordinator", method_name="process"),
        _chunk("method", "vendor/acme/Injected.php", 1, "public function unsafe() { " + ("exec($x); " * 20) + "}",
               parent_symbol="Injected", method_name="unsafe"),
    ]
    first = _build_ai_review_context(PROJECT, REVISION, payloads, BUSINESS_FILES)
    second = _build_ai_review_context(PROJECT, REVISION, list(reversed(payloads)), BUSINESS_FILES)

    assert first == second
    assert len(first) == 1
    assert first[0]["file"] == BUSINESS_FILES[0]
    assert first[0]["evidence_id"].startswith("ev_")
