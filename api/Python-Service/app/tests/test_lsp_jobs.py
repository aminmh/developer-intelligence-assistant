"""Phase 3 of the LSP-first plan: where LSP indexing runs and how its export is merged.

Contract: external-analyzer spec clauses 32a-32c (worker when consented, else the
API process, else the parser export stands).
"""

from __future__ import annotations

import io
import zipfile

import pytest

from lsp import jobs


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


class FakeRegistry:
    def __init__(self, result=None, languages=("php",)):
        self.result = result or {"status": "complete", "definitions": [{"kind": "class", "fqn": "A"}],
                                 "references": [{"scope": "A::f", "kind": "method_call", "to_fqn": "A",
                                                 "to_member": "g", "file": "a.php", "line": 3, "origin": "lsp"}]}
        self.languages = list(languages)
        self.roots: list[tuple[str, str]] = []
        self.seen_files: list[list[str]] = []

    def index_languages(self):
        return self.languages

    def adapter(self, language):
        return type("A", (), {"extensions": [".php"]})()

    def index_workspace(self, language, root):
        import os

        self.roots.append((language, root))
        self.seen_files.append(sorted(
            os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs))
        return dict(self.result)


PARSER = {"definitions": [{"kind": "class", "fqn": "A", "origin": "php-parser"},
                          {"kind": "class", "fqn": "OnlyParser", "origin": "php-parser"}],
          "references": [{"scope": "A::f", "kind": "method_call", "to_fqn": "B", "to_member": "g",
                          "file": "a.php", "line": 3, "origin": "php-parser"},
                         {"scope": "x\\f", "kind": "function_call", "to_fqn": "x\\g", "file": "x.ts", "line": 1,
                          "origin": "scip"}]}


def test_complete_lsp_export_replaces_parser_references_but_keeps_scip():
    export = {"status": "complete", "definitions": [{"kind": "class", "fqn": "A", "origin": "lsp"}],
              "references": [{"scope": "A::f", "kind": "method_call", "to_fqn": "A", "origin": "lsp"}]}
    merged = jobs.merge_into(PARSER, export)
    assert [(d["fqn"], d["origin"]) for d in merged["definitions"]] == [("OnlyParser", "php-parser"), ("A", "lsp")]
    assert [r["origin"] for r in merged["references"]] == ["scip", "lsp"]


def test_partial_lsp_export_keeps_parser_references_and_writes_lsp_last():
    export = {"status": "partial", "definitions": [{"kind": "class", "fqn": "A", "origin": "lsp"}],
              "references": [{"scope": "A::f", "kind": "method_call", "to_fqn": "A", "origin": "lsp"}]}
    merged = jobs.merge_into(PARSER, export)
    assert [r["origin"] for r in merged["references"]] == ["php-parser", "scip", "lsp"]


@pytest.mark.parametrize("export", [None, {"status": "unavailable", "definitions": [], "references": []},
                                    {"status": "complete", "definitions": [], "references": []}])
def test_unusable_lsp_export_leaves_parser_export_unchanged(export):
    assert jobs.merge_into(PARSER, export) is PARSER


def test_index_directory_reports_per_language_coverage():
    registry = FakeRegistry({"status": "partial", "limits_reached": ["request_timeout"], "definitions": [{}],
                             "references": [{}, {}]})
    out = jobs.index_directory("/src", registry=registry)
    assert out["status"] == "partial"
    assert out["languages"]["php"] == {"status": "partial", "limits_reached": ["request_timeout"],
                                       "definitions": 1, "references": 2}
    assert registry.roots == [("php", "/src")]


def test_index_directory_with_failing_language_is_unavailable():
    class Broken(FakeRegistry):
        def index_workspace(self, language, root):
            raise RuntimeError("boom")

    out = jobs.index_directory("/src", registry=Broken())
    assert out["status"] == "unavailable" and "boom" in out["languages"]["php"]["reason"]


def test_run_lsp_extracts_only_selected_files():
    registry = FakeRegistry()
    archive = _zip({"app/A.php": "<?php class A {}", "app/B.php": "<?php", "notes.txt": "x"})
    out = jobs.run_lsp(archive, ["app/A.php"], {"php": 1}, registry=registry)
    assert out["status"] == "complete"
    assert registry.seen_files == [["app/A.php"]]


def test_run_lsp_without_enabled_language_is_unavailable():
    out = jobs.run_lsp(_zip({"a.go": "package a"}), ["a.go"], {"go": 1}, registry=FakeRegistry())
    assert out["status"] == "unavailable" and out["definitions"] == []


def test_lsp_inventory_skips_vendor_and_other_languages():
    from code_quality.remote_worker import lsp_inventory

    archive = _zip({"p/app/A.php": "<?php", "p/vendor/x/Y.php": "<?php", "p/web/app.ts": "x"})
    files, languages = lsp_inventory(archive, {"php": [".php"]})
    assert files == ["p/app/A.php"] and languages == {"php": 1}


def test_worker_lsp_output_is_validated_as_untrusted(monkeypatch):
    from code_quality import remote_worker

    def fake_request(target, method, path, payload, timeout, max_bytes):
        assert payload["analyzers"] == ["lsp"] and timeout >= 900
        return {"job_id": payload["job_id"], "revision_id": payload["revision_id"],
                "bundle_sha256": payload["bundle_sha256"], "analyzers": {"lsp": {
                    "definitions": [
                        {"kind": "class", "fqn": "App\\A", "name": "A", "file": "app/A.php", "line": 3,
                         "relations": [{"kind": "uses_trait", "target": "App\\T"}], "extra": "dropped"},
                        {"kind": "class", "fqn": "Evil", "name": "Evil", "file": "/etc/passwd", "line": 1},
                        {"kind": "class", "fqn": "Bad", "name": "Bad", "file": "app/A.php", "line": 1,
                         "relations": [{"kind": "rm -rf", "target": "x"}]}],
                    "references": [{"scope": "App\\A::f", "kind": "property_fetch", "to_fqn": "App\\A",
                                    "to_member": "$x", "file": "app/A.php", "line": 5, "origin": "scip"},
                                   {"scope": "s", "kind": "exec", "to_fqn": "x", "file": "app/A.php", "line": 1}],
                    "languages": {"php": {"status": "complete"}}, "truncated": False}}}

    monkeypatch.setattr(remote_worker, "_request", fake_request)
    monkeypatch.setattr(remote_worker, "_filtered_source_bundle", lambda data, files: (b"zip", {"app/A.php": 9}))
    out = remote_worker.run_lsp_job({"source_transfer_consent": True}, b"zip", ["app/A.php"], {"php": 1},
                                    "rev_" + "0" * 32, None)
    assert out["status"] == "complete"
    assert out["definitions"] == [{"kind": "class", "fqn": "App\\A", "name": "A", "file": "app/A.php", "line": 3,
                                   "origin": "lsp", "relations": [{"kind": "uses_trait", "target": "App\\T"}]}]
    assert [(r["kind"], r["origin"]) for r in out["references"]] == [("property_fetch", "lsp")]
    with pytest.raises(remote_worker.RemoteWorkerError):
        remote_worker.run_lsp_job({}, b"zip", ["app/A.php"], {"php": 1}, "rev_" + "0" * 32, None)


# ─── Snapshot indexing: worker → API process → parser export ────────────────

@pytest.fixture
def lsp_env(monkeypatch):
    import code_quality.external_analyzers as external
    import code_quality.remote_worker as remote_worker

    calls = {"worker": 0, "api": 0}
    state = {"target": {"source_transfer_consent": True}, "worker": None, "api": None}

    monkeypatch.setattr(jobs, "language_extensions", lambda registry=None: {"php": [".php"]})

    class Store:
        def get(self, project_id):
            return {}

    monkeypatch.setattr(external, "ExternalAnalyzerSettingsStore", Store)
    monkeypatch.setattr(external, "effective_worker_target", lambda settings: state["target"])

    def worker(*args, **kwargs):
        calls["worker"] += 1
        if isinstance(state["worker"], Exception):
            raise state["worker"]
        return dict(state["worker"])

    def api(root, languages=None, registry=None):
        calls["api"] += 1
        return dict(state["api"])

    monkeypatch.setattr(remote_worker, "run_lsp_job", worker)
    monkeypatch.setattr(jobs, "index_directory", api)
    return calls, state


IDENTITY = {"project_id": "pizza", "revision_id": "rev_" + "0" * 32, "commit_sha": None}
LSP_OK = {"status": "complete", "definitions": [{"kind": "class", "fqn": "A", "origin": "lsp"}],
          "references": [{"scope": "A::f", "kind": "method_call", "to_fqn": "A", "origin": "lsp"}]}


@pytest.fixture
def workdir(tmp_path):
    root = tmp_path / "work"
    (root / "app").mkdir(parents=True)
    (root / "app/A.php").write_text("<?php")
    return str(root)


def _with_lsp(workdir, identity=IDENTITY):
    from index.indexing_service import IndexingService

    return IndexingService._with_lsp(PARSER, _zip({"app/A.php": "<?php"}), workdir, identity)


def test_consented_worker_is_used_first(lsp_env, workdir):
    calls, state = lsp_env
    state["worker"] = LSP_OK
    merged = _with_lsp(workdir)
    assert calls == {"worker": 1, "api": 0}
    assert merged["references"][-1]["origin"] == "lsp"


def test_no_worker_falls_back_to_api_process(lsp_env, workdir):
    calls, state = lsp_env
    state["target"] = None
    state["api"] = LSP_OK
    merged = _with_lsp(workdir)
    assert calls == {"worker": 0, "api": 1}
    assert merged["references"][-1]["origin"] == "lsp"


def test_failing_worker_falls_back_to_api_process(lsp_env, workdir):
    calls, state = lsp_env
    state["worker"] = RuntimeError("worker down")
    state["api"] = LSP_OK
    _with_lsp(workdir)
    assert calls == {"worker": 1, "api": 1}


def test_unavailable_everywhere_keeps_parser_export(lsp_env, workdir):
    calls, state = lsp_env
    state["worker"] = {"status": "unavailable", "definitions": [], "references": []}
    state["api"] = {"status": "unavailable", "definitions": [], "references": []}
    assert _with_lsp(workdir) is PARSER
    assert calls == {"worker": 1, "api": 1}


def test_upload_without_identity_indexes_in_api_process(lsp_env, workdir):
    calls, state = lsp_env
    state["api"] = LSP_OK
    _with_lsp(workdir, identity=None)
    assert calls == {"worker": 0, "api": 1}


def test_workspace_without_lsp_language_files_is_left_alone(lsp_env, tmp_path):
    calls, state = lsp_env
    other = tmp_path / "go-only"
    other.mkdir()
    (other / "main.go").write_text("package main")
    (other / "vendor").mkdir()
    (other / "vendor" / "X.php").write_text("<?php")  # dependency trees do not count
    assert jobs.enrich_export(PARSER, str(other)) is PARSER
    assert calls == {"worker": 0, "api": 0}


def test_worker_gets_zipped_workspace_when_no_archive_is_given(lsp_env, monkeypatch, workdir):
    calls, state = lsp_env
    state["worker"] = LSP_OK
    seen = {}
    import code_quality.remote_worker as remote_worker

    def worker(target, zip_data, files, languages, revision_id, commit_sha):
        seen["names"] = sorted(zipfile.ZipFile(io.BytesIO(zip_data)).namelist())
        return dict(LSP_OK)

    monkeypatch.setattr(remote_worker, "run_lsp_job", worker)
    jobs.enrich_export(PARSER, workdir, identity=IDENTITY)
    assert seen["names"] == ["app/A.php"]


def test_identity_without_revision_skips_the_worker(lsp_env, workdir):
    calls, state = lsp_env
    state["api"] = LSP_OK
    jobs.enrich_export(PARSER, workdir, identity={"project_id": "pizza"})
    assert calls == {"worker": 0, "api": 1}
