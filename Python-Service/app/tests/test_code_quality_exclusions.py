import asyncio
import io
import zipfile

import pytest

from code_quality.exclusions import matches_exclusion, normalize_exclusion_patterns
from code_quality.service import ReportStore, inventory_archive


def _zip_sources(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


def test_normalize_project_exclusions_strips_deduplicates_and_rejects_unsafe_patterns():
    assert normalize_exclusion_patterns([" src/legacy/ ", "src/legacy", "**/generated/**"]) == [
        "**/generated/**", "src/legacy",
    ]

    for pattern in ("../secrets", "/etc/passwd", "src//file.py", "src/./file.py", "src/" + "a" * 300):
        with pytest.raises(ValueError):
            normalize_exclusion_patterns([pattern])


@pytest.mark.parametrize(
    ("path", "patterns", "expected"),
    [
        ("app/Legacy/Controller.php", ["app/Legacy"], True),
        ("app/Legacy/Controller.php", ["app/*/Controller.php"], True),
        ("packages/a/generated/client.py", ["**/generated/**"], True),
        ("app/Other/Controller.php", ["app/Legacy/Controller.php"], False),
        ("app/Legacy/Controller.php", ["app/Lega?y/*.php"], True),
    ],
)
def test_project_exclusion_globs_match_files_and_directory_descendants(path, patterns, expected):
    assert matches_exclusion(path, patterns) is expected


def test_inventory_applies_project_exclusions_before_source_evidence_collection():
    archive = _zip_sources({
        "src/Keep.php": "<?php\nreturn true;\n",
        "src/Legacy/Controller.php": "<?php\nreturn false;\n",
        "project_data/test_inputs/Example.py": "print('fixture')\n",
        "packages/app/templates/client.ts": "export const template = true;\n",
    })

    languages, _, files, stats, _ = inventory_archive(
        archive, ["src/Legacy", "project_data/test_inputs/**", "**/templates/**"],
    )

    assert files == ["src/Keep.php"]
    assert languages == {"php": 1}
    assert stats["user_excluded_files"] == 3
    assert stats["scanned_files"] == 1
    assert stats["detected_files"] == 4


def test_project_exclusions_persist_independently_per_project(tmp_path):
    store = ReportStore(tmp_path / "quality.db")

    saved = store.save_exclusions("pizza", ["tests/fixtures/**", "src/Legacy"])
    store.save_exclusions("other-project", ["vendor/**"])

    assert saved == ["src/Legacy", "tests/fixtures/**"]
    assert store.exclusions("pizza") == saved
    assert store.exclusions("other-project") == ["vendor/**"]
    assert store.exclusions("new-project") == []


def test_assessment_shares_filtered_inventory_and_persists_exclusion_provenance(monkeypatch, tmp_path):
    import code_quality.flow_tracing as flow_tracing
    import code_quality.php_design as php_design
    import code_quality.rules as rules
    import code_quality.semantic_context as semantic_context
    import code_quality.service as service

    monkeypatch.setenv("TELEMETRY_SNAPSHOT_ROOT", str(tmp_path))
    store = ReportStore(tmp_path / "quality.db")
    store.save_exclusions("pizza", ["src/Legacy"])
    archive = _zip_sources({
        "src/Keep.php": "<?php\nreturn true;\n",
        "src/Legacy/Controller.php": "<?php\nreturn false;\n",
        "src/Legacy/README.md": "# Old\n",
        "README.md": "# Pizza\n",
        ".env": "DB_PASSWORD=x\n",
    })
    seen = {}

    def run_static(_archive, files, _custom_rules):
        seen["static"] = list(files)
        return [], [], None

    def retrieve_semantic(_project, _revision, _snapshot, files, *_args):
        seen["semantic"] = list(files)
        return ({"status": "available_no_matches", "revision_id": "rev_0123456789abcdef0123456789abcdef",
                 "retriever_version": "test", "query_count": 0, "evidence_count": 0, "reason": "No matches."}, [])

    def retrieve_flow(_project, _revision, _snapshot, files):
        seen["flow"] = list(files)
        return ({"status": "unavailable", "revision_id": "rev_0123456789abcdef0123456789abcdef",
                 "tracer_version": "test", "flow_types": [], "limits": {}, "edge_types": [], "reason": "Unavailable."}, [])

    monkeypatch.setattr(rules, "CustomRuleStore", lambda: type("Rules", (), {"enabled": lambda self, _user: []})())
    monkeypatch.setattr(service, "_run_static_rules", run_static)
    monkeypatch.setattr(semantic_context, "retrieve_revision_context", retrieve_semantic)
    monkeypatch.setattr(flow_tracing, "retrieve_revision_call_traces", retrieve_flow)
    monkeypatch.setattr(php_design, "build_php_assessment_evidence_from_archive", lambda *_args: (
        {"status": "unavailable", "reason": "Parser intentionally isolated in this inventory test."}, [], [],
    ))
    def score(evidence, *_args):
        categories = {
            category: {"assessed": True, "score": 60, "reason": f"Reviewed {category} source context.",
                       "evidence_ids": [next(row["evidence_id"] for row in evidence
                                             if row.get("category") == category and row.get("kind") != "coverage")]}
            for category in service.CATEGORIES
        }
        return categories, "test-model", "test-provider", None, [], []

    monkeypatch.setattr(service, "_score", score)

    report = service.assess_revision(
        "pizza", "rev_0123456789abcdef0123456789abcdef",
        {"commit_sha": "a" * 40, "source_archive_sha256": "b" * 64},
        archive, "alice", store,
    )

    assert seen == {"static": ["src/Keep.php"], "semantic": ["src/Keep.php"], "flow": ["src/Keep.php"]}
    assert report["coverage"]["user_exclusion_patterns"] == ["src/Legacy"]
    assert report["coverage"]["user_excluded_files"] == 1
    evidence = report["coverage"]["evidence_files"]  # plan task 13: excluded paths stay out, secrets by name only
    assert evidence["by_kind"] == {"docs": 1, "secret": 1} and evidence["excluded_files"] == 1
    assert {(row["path"], row["readable"]) for row in evidence["files"]} == {("README.md", True), (".env", False)}
    assert store.get_report("pizza", report["report_id"]) == report


def test_exclusion_api_requires_internal_service_auth_and_rejects_invalid_patterns(monkeypatch, tmp_path):
    from fastapi import HTTPException
    from starlette.requests import Request

    import code_quality.http as http

    store = ReportStore(tmp_path / "quality.db")
    monkeypatch.setattr(http, "ReportStore", lambda: store)
    monkeypatch.setenv("RAG_CALLBACK_SECRET", "x" * 40)

    def request(method: str, payload: bytes = b"", secret: bytes = b"x" * 40) -> Request:
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}

        return Request({
            "type": "http", "method": method, "path": "/projects/pizza/exclusions",
            "headers": [(b"x-internal-secret", secret), (b"x-user-id", b"alice")],
        }, receive)

    assert http.get_project_exclusions("pizza", request("GET")) == {"patterns": []}
    assert asyncio.run(http.put_project_exclusions(
        "pizza", request("PUT", b'{"patterns":["tests/fixtures/**"]}'),
    )) == {"patterns": ["tests/fixtures/**"]}
    assert store.exclusions("pizza") == ["tests/fixtures/**"]

    with pytest.raises(HTTPException) as invalid:
        asyncio.run(http.put_project_exclusions(
            "pizza", request("PUT", b'{"patterns":["../outside"]}'),
        ))
    assert invalid.value.status_code == 422
    assert store.exclusions("pizza") == ["tests/fixtures/**"]

    with pytest.raises(HTTPException) as unauthorized:
        http.get_project_exclusions("pizza", request("GET", secret=b"wrong"))
    assert unauthorized.value.status_code == 401
