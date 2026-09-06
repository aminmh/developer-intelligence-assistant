"""P2/P3 tests: language-dispatch chunking (lang_index) + kind-aware Save.

Hermetic: no daemon/Qdrant/Neo4j/php-CLI contact. ``CodePropertyGraph``, the
Neo4j link pass, workspace registry/prepare, the php ``run_code_parse``
delegate and ``inspection`` sym-graph index are all stubbed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import analysis.analysis_service as svc
import index.llamaindex_layer as lli
from analysis.analysis_store import AnalysisStore
from projects import lang_index


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --- P2: lang_index dispatch -------------------------------------------------


_PY_SRC = '''"""Module doc."""
import os
import math
from collections import OrderedDict

def helper(a, b=2, *args, c=1, **kw):
    "helper doc"
    return a + b

class Service:
    """Service class doc."""
    def __init__(self, name: str):
        self.name = name

    @staticmethod
    def ping(url: str) -> bool:
        return os.path.exists(url)

    async def run(self):
        helper(1)
        OrderedDict(a=1)
        math.floor(2.5)
        for _ in range(10):
            helper(2)
'''


def _py_workspace(tmp_path: Path) -> Path:
    _write(tmp_path, "src/__init__.py", "")
    _write(tmp_path, "src/mod.py", _PY_SRC)
    _write(tmp_path, "src/noise/s.py", "x = 1\n")
    return tmp_path


class TestProduceChunksDispatch:
    def test_python_ast_chunk_shape_and_parity(self, tmp_path):
        ws = _py_workspace(tmp_path)
        out = lang_index.produce_chunks(str(ws))
        assert out["language"] == "python"
        assert out["producer"] == "python-ast"
        assert out["error"] is None
        assert out["files"] == 3  # __init__ + mod + noise/s (venv never exists)
        by = {(c["type"], c["name"]): c for c in out["chunks"]}
        # class + method + module-level function chunks all present
        assert ("class", "Service") in by
        assert ("method", "ping") in by
        assert ("method", "run") in by
        assert ("function", "helper") in by
        for c in out["chunks"]:
            # every chunk carries the full flat contract the ingest reads
            for key in (
                "type", "name", "class", "class_fqn", "namespace", "file",
                "start_line", "end_line", "modifiers", "parameters",
                "return_type", "calls", "callers", "has_docblock",
                "docblock", "code",
            ):
                assert key in c, f"{key} missing from {c}"
        cls = by[("class", "Service")]
        assert cls["class_fqn"] == "src.mod.Service"  # dotted
        assert cls["has_docblock"] is True
        assert "Service class doc." in cls["docblock"]
        assert cls["code"].startswith("class Service:")
        ping = by[("method", "ping")]
        assert ping["class"] == "Service"
        assert ping["class_fqn"] == "src.mod.Service"  # owner fqn (php parity)
        assert ping["modifiers"] == ["static"]
        assert ping["parameters"] == ["url"]
        assert ping["return_type"] == "bool"
        assert ping["calls"] == ["os.path.exists"]
        assert ping["callers"] == []
        run = by[("method", "run")]
        assert run["modifiers"] == ["async"]
        assert len(run["calls"]) <= 16
        assert "helper" in run["calls"]  # deduped across the loop body
        assert run["calls"] == sorted(run["calls"])

    def test_python_chunks_are_deterministic(self, tmp_path):
        ws = _py_workspace(tmp_path)
        first = lang_index.produce_chunks(str(ws))
        second = lang_index.produce_chunks(str(ws))
        assert first == second

    def test_skip_dirs_ignored(self, tmp_path):
        ws = _py_workspace(tmp_path)
        _write(ws, "node_modules/x.py", "def bad():\n    return 1\n")
        _write(ws, ".venv/x.py", "def bad2():\n    return 1\n")
        out = lang_index.produce_chunks(str(ws))
        names = [c["name"] for c in out["chunks"]]
        assert "bad" not in names and "bad2" not in names
        assert out["files"] == 3

    def test_python_file_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(lang_index, "_MAX_PY_FILES", 2)
        ws = _py_workspace(tmp_path)
        _write(ws, "extra1.py", "def f1():\n    pass\n")
        _write(ws, "extra2.py", "def f2():\n    pass\n")
        out = lang_index.produce_chunks(str(ws))
        assert out["files"] == 2  # capped: deterministic sorted subset

    def test_php_producer_delegates_to_code_parse(self, tmp_path, monkeypatch):
        _write(tmp_path, "src/App.php", "<?php class App { public function x() {} }")
        fixture = [
            {
                "type": "method", "name": "x", "class": "App", "class_fqn": "src.App",
                "namespace": None, "file": "src/App.php", "start_line": 1,
                "end_line": 1, "modifiers": ["public"], "parameters": [],
                "return_type": None, "calls": [], "callers": [],
                "has_docblock": False, "docblock": None, "code": "<?php ...",
            }
        ]
        called = {}

        def fake_run_code_parse(project_path):
            called["path"] = project_path
            return fixture

        monkeypatch.setattr("infra.php_tools.run_code_parse", fake_run_code_parse)
        out = lang_index.produce_chunks(str(tmp_path))
        assert out["language"] == "php"
        assert out["producer"] == "code-parse"
        assert out["chunks"] == fixture  # php chunk list is the ingest contract
        assert out["files"] == 1  # census php file count
        assert called["path"] == str(tmp_path)

    def test_unsupported_language_is_honest(self, tmp_path):
        _write(tmp_path, "app.js", "const x = 1;\n")
        out = lang_index.produce_chunks(str(tmp_path))
        assert out["language"] == "javascript"
        assert out["producer"] is None
        assert out["chunks"] == []
        assert out["error"] == "no indexer for javascript"

    def test_missing_workspace_is_honest(self, tmp_path):
        out = lang_index.produce_chunks(str(tmp_path / "ghost"))
        assert out["error"] is not None and "workspace" in out["error"]
        assert out["chunks"] == []
        assert out["producer"] is None


# --- P3: structure facts + persist dispatch ----------------------------------


_STRUCT_PROFILE = {
    "framework": "Django",
    "version": "5.1",
    "language": "python",
    "languages": [
        {"language": "python", "files": 40},
        {"language": "javascript", "files": 7},
        {"language": "html", "files": 3},
    ],
    "database": "PostgreSQL",
    "database_confidence": "high",
    "top_level_dirs": ["apps", "config", "static"],
    "entrypoints": ["manage.py"],
    "manifests": ["requirements.txt", "pyproject.toml"],
    "dependency_count": 23,
    "total_files": 51,
    "summary": "Django project; python; 51 file(s); DB PostgreSQL",
}


class TestStructureFacts:
    def test_deterministic_chunks_and_fact_count(self):
        a = svc._structure_chunks(_STRUCT_PROFILE, "DjangoApp")
        b = svc._structure_chunks(_STRUCT_PROFILE, "DjangoApp")
        assert a == b
        assert len(a) > 0
        slugs = [c["name"] for c in a]
        assert "framework:Django:5.1" in slugs
        assert "language:python" in slugs
        assert "database:PostgreSQL" in slugs
        assert all(c["class_fqn"] == "kn:structure:DjangoApp" for c in a)
        assert all(c["file"] == "structure" for c in a)
        assert all(c["type"] == "structure_fact" for c in a)
        # determinism + counts line up for persist_structure
        counts = svc.build_counts(_STRUCT_PROFILE, None)
        counts["structure_facts"] = len(a)
        assert counts["structure_facts"] == len(a)

    def test_grounding_lists_same_facts(self):
        facts = svc._structure_facts(_STRUCT_PROFILE)
        md = svc._structure_grounding(facts)
        assert "## Structure facts" in md
        assert "**framework:Django:5.1**" in md


class _FakeGraph:
    """Records ingest calls like the real CodePropertyGraph().ingest."""

    def __init__(self, results=None):
        self.calls = []
        self.results = results

    def ingest(self, chunks, collection_name):
        self.calls.append((chunks, collection_name))
        if self.results is not None:
            return dict(self.results)
        return {
            "collection_name": collection_name,
            "ingested": len(chunks),
            "chunk_count": len(chunks),
            "graph_nodes": len(chunks),
            "graph_relations": 0,
            "embed_fails": 0,
        }


class _FakeStore:
    def __init__(self):
        self.rows = {}
        self.saved = []

    def save(self, session_id, job_id, filename, **kwargs):
        row = {
            "id": session_id, "job_id": job_id, "filename": filename,
            "kind": kwargs.get("kind", "analysis"),
            **{k: v for k, v in kwargs.items() if k not in ("profile", "kind")},
            "profile": kwargs.get("profile"),
        }
        self.rows[session_id] = row
        self.saved.append(row)

    def get(self, session_id):
        return self.rows.get(session_id)

    def get_by_job(self, job_id):
        for row in self.rows.values():
            if row["job_id"] == job_id:
                return row
        return None


class TestPersistStructure:
    def test_happy_path_ingests_facts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
        svc.write_bundle(
            "jobS",
            {
                "filename": "DjangoApp.zip",
                "structure_profile": _STRUCT_PROFILE,
                "project_profile": None,
            },
        )
        graph = _FakeGraph()
        fake_store = _FakeStore()
        monkeypatch.setattr(svc, "analysis_store", fake_store)
        monkeypatch.setattr(lli, "CodePropertyGraph", lambda: graph)
        monkeypatch.setattr(
            svc, "link_knowledge_to_graph",
            lambda chunks, collection: {"linked": len(chunks)},
        )

        row = svc.persist_structure("jobS", label="struct")
        assert row is not None
        assert row["kind"] == "structure"
        assert row["collection_name"] == "proj_knowledge_DjangoApp"
        assert row["counts"]["structure_facts"] > 0
        chunks, collection = graph.calls[0]
        assert collection == "proj_knowledge_DjangoApp"
        assert len(chunks) == row["counts"]["structure_facts"]
        assert row["ingest"]["ingested"] == len(chunks)
        assert "Structure facts" in row["grounding_md"]

    def test_no_bundle_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
        assert svc.persist_structure("missingS") is None


class TestPersistCodeIndex:
    def _write_bundle(self, tmp_path, job_id="jobC", filename="Archive.zip"):
        svc.write_bundle(
            job_id,
            {
                "filename": filename,
                "route_name": "index_code",
                "manifest": {"route": "index_code"},
            },
        )

    def test_with_retained_workspace_ingests(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
        ws = _py_workspace(tmp_path / "ws")
        self._write_bundle(tmp_path, "jobC", "Archive.zip")

        graph = _FakeGraph()
        fake_store = _FakeStore()
        monkeypatch.setattr(svc, "analysis_store", fake_store)
        monkeypatch.setattr(svc, "_retained_workspace", lambda filename: str(ws))
        prepared = []
        monkeypatch.setattr(
            svc, "_prepare_workspace_http", lambda filename: prepared.append(filename) or None
        )
        monkeypatch.setattr(lli, "CodePropertyGraph", lambda: graph)
        # producer is python-ast → sym graph is never attempted

        row = svc.persist_code_index("jobC", label="idx")
        assert row is not None
        assert row["kind"] == "code_index"
        assert row["collection_name"] == "chunk_code_Archive"
        assert row["counts"]["language"] == "python"
        assert row["counts"]["chunks"] > 0
        assert prepared == []  # retained workspace used, no HTTP prepare
        chunks, collection = graph.calls[0]
        assert collection == "chunk_code_Archive"
        assert len(chunks) == row["counts"]["chunks"]
        assert "Code index:" in row["grounding_md"]
        assert "sym" not in row

    def test_php_runs_sym_graph(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
        ws = tmp_path / "ws"
        _write(ws, "src/App.php", "<?php class App {}")
        self._write_bundle(tmp_path, "jobP", "PApp.zip")
        fake_store = _FakeStore()
        graph = _FakeGraph()
        monkeypatch.setattr(svc, "analysis_store", fake_store)
        monkeypatch.setattr(svc, "_retained_workspace", lambda filename: str(ws))
        monkeypatch.setattr(lli, "CodePropertyGraph", lambda: graph)
        sym_calls = []

        def fake_produce_chunks(path):
            return {
                "language": "php",
                "chunks": [{"type": "method", "name": "x", "class_fqn": "src.App",
                            "class": "App", "file": "src/App.php", "code": "<?php"}],
                "files": 1,
                "producer": "code-parse",
                "error": None,
            }

        def fake_sym(ws_path, stem):
            sym_calls.append((ws_path, stem))
            return {"collection": "sym_PApp", "indexed": True, "nodes": 3}

        monkeypatch.setattr("projects.lang_index.produce_chunks", fake_produce_chunks)
        monkeypatch.setattr(svc, "_index_sym_graph", fake_sym)

        row = svc.persist_code_index("jobP")
        assert row is not None
        assert row["counts"]["producer"] == "code-parse"
        assert row["counts"]["sym_nodes"] == 3
        assert sym_calls == [(str(ws), "PApp")]

    def test_no_workspace_unsupported_lang_row_saved(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
        self._write_bundle(tmp_path, "jobX", "X.zip")
        fake_store = _FakeStore()
        graph = _FakeGraph()
        monkeypatch.setattr(svc, "analysis_store", fake_store)
        monkeypatch.setattr(lli, "CodePropertyGraph", lambda: graph)
        monkeypatch.setattr(svc, "_retained_workspace", lambda filename: None)
        monkeypatch.setattr(svc, "_prepare_workspace_http", lambda filename: None)

        row = svc.persist_code_index("jobX")
        assert row is not None
        assert row["kind"] == "code_index"
        assert row["counts"]["error"] == "workspace unavailable"
        assert row["ingest"]["ingested"] == 0
        assert graph.calls == []  # nothing ingested without a workspace
        assert "failed" in row["summary"]

    def test_workspace_with_unsupported_language_honest(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
        ws = tmp_path / "ws"
        _write(ws, "app.rb", "puts 1\n")
        self._write_bundle(tmp_path, "jobY", "Y.zip")
        fake_store = _FakeStore()
        graph = _FakeGraph()
        monkeypatch.setattr(svc, "analysis_store", fake_store)
        monkeypatch.setattr(lli, "CodePropertyGraph", lambda: graph)
        monkeypatch.setattr(svc, "_retained_workspace", lambda filename: str(ws))

        row = svc.persist_code_index("jobY")
        assert row is not None
        assert row["kind"] == "code_index"
        assert row["counts"]["language"] == "ruby"
        assert row["counts"]["error"] == "no indexer for ruby"
        assert graph.calls == []
        assert "failed" in row["summary"]


class TestPersistForBundleDispatch:
    def _store(self, tmp_path, bundle):
        monkeypatch_store = None
        svc.write_bundle("jobD", bundle)
        return monkeypatch_store

    def test_routes_to_structure_for_structure_analyze(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
        svc.write_bundle(
            "jobS",
            {
                "filename": "S.zip",
                "route_name": "structure_analyze",
                "manifest": {"route": "structure_analyze"},
                "structure_profile": _STRUCT_PROFILE,
            },
        )
        graph = _FakeGraph()
        fake_store = _FakeStore()
        monkeypatch.setattr(svc, "analysis_store", fake_store)
        monkeypatch.setattr(lli, "CodePropertyGraph", lambda: graph)
        monkeypatch.setattr(svc, "link_knowledge_to_graph",
                            lambda chunks, collection: {"linked": 0})
        row = svc.persist_for_bundle("jobS")
        assert row is not None and row["kind"] == "structure"
        assert row["counts"]["structure_facts"] > 0

    def test_routes_to_analysis_for_unknown_route(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
        profile = dict(_STRUCT_PROFILE)
        profile["routes"] = [
            {"method": "GET", "uri": "/", "action": "C@index",
             "name": "home", "middleware": [], "file": "web.py"}
        ]
        svc.write_bundle(
            "jobZ",
            {
                "filename": "Z.zip",
                "route_name": "sandbox_analyze",
                "manifest": {"route": "sandbox_analyze"},
                "project_profile": profile,
                "sandbox": {"probes": []},
                "output": "out",
            },
        )
        graph = _FakeGraph()
        fake_store = _FakeStore()
        monkeypatch.setattr(svc, "analysis_store", fake_store)
        monkeypatch.setattr(lli, "CodePropertyGraph", lambda: graph)
        row = svc.persist_for_bundle("jobZ")
        assert row is not None and row["kind"] == "analysis"
        assert graph.calls[0][1] == "proj_knowledge_Z"

    def test_no_bundle_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
        assert svc.persist_for_bundle("jobNope") is None


class TestDispatchStepsAndOrchestratorSeam:
    """index step ordering/markers + the orchestrator produce seam (hermetic)."""

    class _FakeRunner:
        def __init__(self, produce_result):
            self.produce_result = produce_result
            self._select_result = None
            self.calls: list[str] = []
            self.step_marks: list[tuple[str, str, dict]] = []

        def _update_step(self, job_id, key, status, **extra):
            self.step_marks.append((key, status, extra))

        def _prepare_workspace(self, ctx):
            self.calls.append("prepare")
            return "/ws/1", 1

        def _select_tool(self, ctx):
            self.calls.append("select_tool")
            return self._select_result

        def _extract_tool_output(self, tool_result):
            return "raw-json"

        def _run_post_process_pipeline(self, ctx, result, tool_name=None,
                                       tool_result=None):
            self.calls.append("post_process")
            return result, None

        def _collect_doc_context(self, ctx, workspace_path, post_process_result):
            return None

        def _collect_project_profile(self, ctx, workspace_path):
            return None

        def _run_sandbox_analysis(self, ctx, workspace_path, project_profile):
            return None

        def _synthesize_with_llamaindex(self, **kwargs):
            self.calls.append("synthesize")
            return ""

        def _produce_code_chunks(self, ctx, workspace_path):
            self.calls.append("produce_chunks")
            return self.produce_result

        def _save_outputs(self, job_id, **kwargs):
            self.calls.append(("save_outputs", kwargs.get("code_index")))

        def _persist_result_bundle(self, ctx, doc_context, project_profile,
                                   sandbox_result):
            self.calls.append("persist_bundle")

    @staticmethod
    def _ctx():
        from types import SimpleNamespace

        return SimpleNamespace(
            job_id="j1",
            route_name="index_code",
            filename="Archive.zip",
            prompt="index it",
            route={"language_dispatch": True, "tool": None, "llamaindex": False},
        )

    def test_build_steps_places_index_after_structure(self):
        from pipelines.steps import build_steps

        plan = build_steps({"structure": True, "language_dispatch": True})
        keys = [s["key"] for s in plan]
        assert keys.index("structure") < keys.index("index")
        assert plan[keys.index("index")]["label"] == "Indexing codebase"
        # off-route routes keep the exact historical plan (no index entry)
        assert "index" not in [s["key"] for s in build_steps({"structure": True})]

    def test_run_plan_dispatch_summary(self):
        from pipelines.steps import SharedState, run_plan

        runner = self._FakeRunner(
            {
                "language": "python",
                "chunks": [{"type": "function", "name": "f"}],
                "files": 1,
                "producer": "python-ast",
                "error": None,
            }
        )
        shared = run_plan(runner, self._ctx(), SharedState())
        assert "produce_chunks" in runner.calls
        assert shared.code_chunks and shared.code_language == "python"
        assert "Indexed 1 python chunk(s) with python-ast." in shared.output
        assert ("index", "done", {"detail": "python-ast · 1 chunk(s)"}) in runner.step_marks
        code_index = next(c for c in runner.calls if isinstance(c, tuple))[1]
        assert code_index == {"language": "python", "chunks": 1,
                              "producer": "python-ast"}

    def test_run_plan_dispatch_error_honest_output(self):
        from pipelines.steps import SharedState, run_plan

        runner = self._FakeRunner(
            {
                "language": "ruby",
                "chunks": [],
                "files": 0,
                "producer": None,
                "error": "no indexer for ruby",
            }
        )
        shared = run_plan(runner, self._ctx(), SharedState())
        assert shared.code_error == "no indexer for ruby"
        assert shared.output == "No code chunks indexed: no indexer for ruby"
        assert ("index", "done", {"detail": "no indexer for ruby"}) in runner.step_marks
        code_index = next(c for c in runner.calls if isinstance(c, tuple))[1]
        assert code_index["error"] == "no indexer for ruby"
        assert code_index["chunks"] == 0

    def test_off_route_plan_and_steps_unaffected(self):
        from types import SimpleNamespace

        from pipelines.steps import SharedState, build_steps, run_plan

        assert "index" not in [s["key"] for s in build_steps({"tool": "x"})]
        calls: list[str] = []
        marks: list[str] = []

        class NoDispatchRunner:
            def _update_step(self, job_id, key, status, **extra):
                marks.append(key)

            def _prepare_workspace(self, ctx):
                return "/ws/1", 1

            def _select_tool(self, ctx):
                self.tool_selected = True

            def _extract_tool_output(self, tool_result):
                return "raw"

            def _run_post_process_pipeline(self, ctx, result, tool_name=None,
                                           tool_result=None):
                return result, None

            def _collect_doc_context(self, ctx, workspace_path, post_process_result):
                return None

            def _collect_project_profile(self, ctx, workspace_path):
                return None

            def _run_sandbox_analysis(self, ctx, workspace_path, project_profile):
                return None

            def _synthesize_with_llamaindex(self, **kwargs):
                return "synth-out"

            def _save_outputs(self, job_id, **kwargs):
                calls.append("save")

            def _persist_result_bundle(self, ctx, doc_context, project_profile,
                                       sandbox_result):
                calls.append("bundle")

        ctx = SimpleNamespace(
            job_id="j", route_name="chat", filename="A.zip", prompt="hi",
            route={"tool": None, "llamaindex": False},
        )
        shared = run_plan(NoDispatchRunner(), ctx, SharedState())
        assert "index" not in marks
        assert shared.code_chunks is None
        assert shared.output == "synth-out"
        assert calls == ["save", "bundle"]


class TestOrchestratorProduceCodeChunks:
    def test_missing_workspace_honest(self, tmp_path):
        from pipelines.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        out = orch._produce_code_chunks(None, None)
        assert out["chunks"] == []
        assert out["error"] and "workspace" in out["error"]

    def test_returns_producer_dict(self, tmp_path, monkeypatch):
        from pipelines.orchestrator import Orchestrator

        ws = tmp_path / "ws"
        ws.mkdir()
        orch = Orchestrator.__new__(Orchestrator)
        fake = {
            "language": "python", "chunks": [], "files": 0,
            "producer": "python-ast", "error": None,
        }
        monkeypatch.setattr(
            "projects.lang_index.produce_chunks",
            lambda path, **kwargs: dict(fake),
        )
        assert orch._produce_code_chunks(None, str(ws)) == fake

    def test_lang_index_failure_wrapped(self, tmp_path, monkeypatch):
        from pipelines.orchestrator import Orchestrator

        ws = tmp_path / "ws"
        ws.mkdir()
        orch = Orchestrator.__new__(Orchestrator)

        def boom(path, **kwargs):
            raise RuntimeError("parser exploded")

        monkeypatch.setattr("projects.lang_index.produce_chunks", boom)
        out = orch._produce_code_chunks(None, str(ws))
        assert out["chunks"] == []
        assert "parser exploded" in out["error"]


class TestAnalysisStoreKind:
    def _store(self, tmp_path) -> AnalysisStore:
        return AnalysisStore(
            db_path=str(tmp_path / "settings.db"),
            analysis_dir=str(tmp_path / "analyses"),
        )

    def test_kind_default_and_kinds_roundtrip(self, tmp_path):
        store = self._store(tmp_path)
        store.save("s1", "job-1", "a.zip", label="analysis row")
        store.save("s2", "job-2", "b.zip", label="structure row", kind="structure")
        store.save("s3", "job-3", "c.zip", label="code row", kind="code_index")
        assert store.get("s1")["kind"] == "analysis"
        assert store.get("s2")["kind"] == "structure"
        assert store.get("s3")["kind"] == "code_index"
        kinds = {r["id"]: r["kind"] for r in store.list()}
        assert kinds == {"s3": "code_index", "s2": "structure", "s1": "analysis"}

    def test_overwrite_same_job_and_kind_is_idempotent(self, tmp_path):
        store = self._store(tmp_path)
        store.save("s1", "job-1", "a.zip", label="one", kind="structure")
        first_created = store.get("s1")["created_at"]
        store.save("s1", "job-1", "a.zip", label="two", kind="structure")
        db = str(tmp_path / "settings.db")
        conn = sqlite3.connect(db)
        count = conn.execute(
            "SELECT COUNT(*) FROM analysis_sessions WHERE job_id='job-1'"
        ).fetchone()[0]
        conn.close()
        row = store.get("s1")
        assert count == 1  # overwritten, never a second row
        assert row["label"] == "two"
        assert row["kind"] == "structure"
        assert row["created_at"] == first_created

    def test_legacy_table_migrates_kind(self, tmp_path):
        db = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE analysis_sessions (
                id              TEXT PRIMARY KEY,
                job_id          TEXT UNIQUE,
                filename        TEXT NOT NULL,
                collection_name TEXT,
                label           TEXT,
                status          TEXT NOT NULL DEFAULT 'saved',
                created_at      TEXT,
                finished_at     TEXT,
                summary         TEXT,
                profile_json    TEXT,
                grounding_md    TEXT,
                runtime_json    TEXT,
                counts_json     TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO analysis_sessions (id, job_id, filename) "
            "VALUES ('old1', 'legacy-job', 'legacy.zip')"
        )
        conn.commit()
        conn.close()

        store = AnalysisStore(db_path=db, analysis_dir=str(tmp_path / "analyses"))
        # legacy row readable and defaults to 'analysis' after migration
        row = store.get_by_job("legacy-job")
        assert row is not None
        assert row["kind"] == "analysis"
        assert row["filename"] == "legacy.zip"
        # new rows carry explicit kinds against the migrated schema
        store.save("s9", "job-9", "n.zip", kind="code_index")
        assert store.get("s9")["kind"] == "code_index"

    def test_kind_migration_is_idempotent(self, tmp_path):
        store = self._store(tmp_path)
        store.save("s1", "job-1", "a.zip", kind="structure")
        store2 = AnalysisStore(
            db_path=str(tmp_path / "settings.db"),
            analysis_dir=str(tmp_path / "analyses"),
        )
        assert store2.get("s1")["kind"] == "structure"  # no dup/migration error
