"""Phase 3 tests: durable bundles, analysis persistence service, knowledge
collection naming/join, orchestrator sandbox step wiring.

No daemon/Qdrant/Neo4j contact — heavy services are stubbed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import analysis.analysis_service as svc
import index.llamaindex_layer as lli
from pipelines.orchestrator import JobContext, Orchestrator


def _profile() -> dict:
    return {
        "framework": "laravel",
        "version": "12",
        "language": "php",
        "summary": "demo",
        "routes": [
            {"method": "POST", "uri": "/checkout", "action": "PaymentController@checkout",
             "name": "checkout", "middleware": ["auth"], "file": "web.php"}
        ],
        "schema": [
            {"table": "payments", "columns": [
                {"name": "id", "type": "bigint", "nullable": False, "default": None,
                 "unique": False, "indexed": False, "foreign": None},
                {"name": "user_id", "type": "bigint", "nullable": False, "default": None,
                 "unique": False, "indexed": False,
                 "foreign": {"table": "users", "column": "id"}},
            ]}
        ],
        "models": [{"name": "Payment", "file": "Payment.php", "table": "payments", "relations": []}],
        "controllers": [],
        "config": {},
        "wiring": {},
        "behaviors": {},
        "frontend": {},
        "errors": {},
        "packages": {"payment_gateways": [], "payment_absent": ["Stripe"], "notable_dependencies": []},
    }


def _runtime() -> dict:
    return {
        "staged": True,
        "source": "/jobs/Archive/src",
        "install": {"status": "done", "exit_code": 0},
        "probes": [
            {"name": "routes", "status": "done", "exit_code": 0,
             "command": "php artisan route:list --json", "image": "composer:2",
             "stdout": json.dumps([{"uri": "/checkout", "method": "POST"}])}
        ],
    }


class TestBundle:
    def test_write_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
        data = {"job_id": "j1", "profile": {"framework": "laravel"}, "sandbox": {}}
        path = svc.write_bundle("j1", data)
        assert path.name == "bundle.json" and path.is_file()
        loaded = svc.load_bundle("j1")
        assert loaded and loaded["job_id"] == "j1"
        assert svc.load_bundle("missing") is None

    def test_stem_and_collection_names(self):
        assert svc.project_stem("Archive.zip") == "Archive"
        assert svc.project_stem("my app_v2.zip") == "my_app_v2"
        assert svc.project_stem("weird..zip") == "weird"
        assert svc.knowledge_collection("Archive.zip") == "proj_knowledge_Archive"
        assert svc.knowledge_collection("weird..zip") == "proj_knowledge_weird"

    def test_knowledge_sibling(self):
        assert lli.knowledge_sibling("chunk_code_Archive") == "proj_knowledge_Archive"
        assert lli.knowledge_sibling("proj_knowledge_Archive") is None
        assert lli.knowledge_sibling(None) is None
        assert lli.knowledge_collection_for("Archive") == "proj_knowledge_Archive"

    def test_counts(self):
        counts = svc.build_counts(_profile(), _runtime())
        assert counts["routes"] == 1
        assert counts["tables"] == 1
        assert counts["models"] == 1
        assert counts["probes_done"] == 1
        assert counts["probes_failed"] == 0
        assert counts["payment_gateways"] == []


class TestPersistAnalysis:
    def test_no_bundle_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
        assert svc.persist_analysis("nope") is None

    def test_persist_ingests_and_records(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
        svc.write_bundle(
            "jobA",
            {"filename": "Archive.zip", "project_profile": _profile(), "sandbox": _runtime()},
        )

        ingest_calls = []

        class FakeGraph:
            @staticmethod
            def ingest(chunks, collection_name):
                ingest_calls.append((chunks, collection_name))
                return {"ingested": len(chunks), "collection_name": collection_name}

        class FakeStore:
            def __init__(self):
                self.rows = {}

            def save(self, session_id, job_id, filename, *, collection_name, label,
                     summary, profile, grounding_md, runtime, counts):
                self.rows[session_id] = {
                    "id": session_id, "job_id": job_id, "filename": filename,
                    "collection_name": collection_name, "label": label,
                    "summary": summary, "profile": profile,
                    "grounding_md": grounding_md, "runtime": runtime, "counts": counts,
                }

            def get(self, session_id):
                return self.rows.get(session_id)

            def get_by_job(self, job_id):
                for r in self.rows.values():
                    if r["job_id"] == job_id:
                        return r
                return None

        fake_store = FakeStore()
        monkeypatch.setattr(svc, "analysis_store", fake_store)
        monkeypatch.setattr(lli, "CodePropertyGraph", FakeGraph)
        monkeypatch.setattr(
            svc, "link_knowledge_to_graph",
            lambda chunks, collection: {"linked": len(chunks)},
        )

        row = svc.persist_analysis("jobA", label="payment check")
        assert row is not None
        assert row["job_id"] == "jobA"
        assert row["collection_name"] == "proj_knowledge_Archive"
        assert row["label"] == "payment check"
        assert "Runtime probes (1)" in row["grounding_md"]
        # chunks deterministic + routed kind present
        assert ingest_calls, "ingest must be called"
        chunks, collection = ingest_calls[0]
        assert collection == "proj_knowledge_Archive"
        names = [c["name"] for c in chunks]
        assert "route:checkout" in names and "table:payments" in names
        assert any(c["class_fqn"].startswith("kn:route:Archive") for c in chunks)
        assert row["ingest"]["ingested"] == len(chunks)
        assert row["link"]["linked"] == len(chunks)

    def test_persist_unknown_job_cleanup(self, tmp_path, monkeypatch):
        # garbage bundle never ingests and never raises
        monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
        svc.write_bundle("bad", {"filename": "x.zip", "project_profile": None})
        monkeypatch.setattr(
            svc, "analysis_store",
            SimpleNamespace(save=lambda **k: None, get=lambda sid: None,
                            get_by_job=lambda job: None),
        )
        row = svc.persist_analysis("bad")
        assert row is not None  # recorded even without a profile (output as summary)


class TestLinkPass:
    def test_no_graph_store_degrades(self, monkeypatch):
        class FakeGraph:
            @staticmethod
            def _get_graph_store():
                return None

        monkeypatch.setattr(lli, "CodePropertyGraph", FakeGraph)
        out = svc.link_knowledge_to_graph([], "proj_knowledge_Archive")
        assert out["linked"] == 0
        assert "unavailable" in out["reason"]


class TestRetrievalJoin:
    def _patch_search_pipeline(self, monkeypatch, collections, hits_by_col):
        col_list = SimpleNamespace(collections=[
            SimpleNamespace(name=n) for n in collections
        ])
        fake_qdrant = SimpleNamespace(
            get_collections=lambda: col_list,
            search=lambda **kw: hits_by_col.get(kw["collection_name"], []),
        )

        def fake_store(*a, **k):
            return None

        monkeypatch.setattr(lli.CodePropertyGraph, "_get_qdrant", lambda self: fake_qdrant)
        monkeypatch.setattr(lli.CodePropertyGraph, "_get_embed_model",
                            lambda self: SimpleNamespace(
                                get_text_embedding=lambda q: [0.1] * 4))
        monkeypatch.setattr(lli.CodePropertyGraph, "_get_graph_store", fake_store)
        monkeypatch.setattr(lli.CodePropertyGraph, "_search",
                            staticmethod(lambda qd, col, vec, lim, filt: fake_qdrant.search(collection_name=col)))
        monkeypatch.setattr(lli, "record_retrieval_trace", lambda *a, **k: None)
        monkeypatch.setattr(lli, "_build_query_filter", lambda **k: None)

    def test_pinned_code_collection_joins_knowledge_sibling(self, monkeypatch):
        collections = ["chunk_code_Archive", "proj_knowledge_Archive"]
        self._patch_search_pipeline(monkeypatch, collections, {})
        out = lli.CodePropertyGraph().hybrid_retrieve(
            "how is checkout wired?", collection_name="chunk_code_Archive"
        )
        assert out["searched_collections"] == ["chunk_code_Archive", "proj_knowledge_Archive"]

    def test_pinned_without_sibling_stays_single(self, monkeypatch):
        collections = ["chunk_code_Archive"]
        self._patch_search_pipeline(monkeypatch, collections, {})
        out = lli.CodePropertyGraph().hybrid_retrieve(
            "routes?", collection_name="chunk_code_Archive"
        )
        assert out["searched_collections"] == ["chunk_code_Archive"]

    def test_global_search_does_not_skip_knowledge(self, monkeypatch):
        collections = ["chunk_code_Archive", "proj_knowledge_Archive", "code_symbols"]
        self._patch_search_pipeline(monkeypatch, collections, {})
        out = lli.CodePropertyGraph().hybrid_retrieve("routes?")
        assert out["searched_collections"] == ["chunk_code_Archive", "proj_knowledge_Archive"]


class TestOrchestratorSandboxWiring:
    def _route(self) -> dict:
        return {
            "tool": "chunk_code",
            "framework_aware": True,
            "sandbox": True,
            "sandbox_probes": ["routes"],
            "post_process": [{"action": "llamaindex_ingest"}],
        }

    def test_steps_include_sandbox(self, tmp_path):
        cfg = tmp_path / "o.yml"
        cfg.write_text(
            "routes:\n  sandbox_analyze:\n    sandbox: true\n    framework_aware: true\n",
            encoding="utf-8",
        )
        orch = Orchestrator(str(cfg))
        steps = orch._build_steps(orch.get_route("sandbox_analyze") or {})
        keys = [s["key"] for s in steps]
        assert "sandbox" in keys
        assert keys.index("framework") < keys.index("sandbox")

    def test_real_config_route_enabled(self):
        from pipelines.orchestrator import orchestrator as orch

        route = orch.get_route("sandbox_analyze")
        assert route and route.get("sandbox") is True
        assert route.get("sandbox_probes") == ["routes", "schedule"]

    def test_run_sandbox_uses_executor(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "composer.json").write_text("{}", encoding="utf-8")
        calls = {}

        class FakeExecutor:
            def analyze(self, workspace_path, project_id, framework="php", probes=None):
                calls.update(workspace_path=workspace_path, project_id=project_id,
                             framework=framework, probes=probes)
                return {"staged": True, "install": {"status": "done"},
                        "probes": [{"name": "routes", "status": "done"}]}

        monkeypatch.setattr("sandbox.executor.default_executor", lambda: FakeExecutor())
        orch = Orchestrator(str(tmp_path / "none.yml"))
        ctx = JobContext("j1", "sandbox_analyze", "Archive.zip", "go", self._route(),
                         workspace_path=str(ws))
        result = orch._run_sandbox_analysis(ctx, str(ws), _profile())
        assert result and result["staged"] is True
        assert calls["framework"] == "laravel"
        assert calls["probes"] == ["routes"]
        assert calls["project_id"] == "Archive"

    def test_run_sandbox_missing_workspace(self, tmp_path):
        orch = Orchestrator(str(tmp_path / "none.yml"))
        ctx = JobContext("j1", "sandbox_analyze", "Archive.zip", "go", self._route())
        result = orch._run_sandbox_analysis(ctx, None, None)
        assert result and result["status"] == "failed"
        assert "workspace" in result["error"]

    def test_run_sandbox_no_backend_honest(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr("sandbox.executor.default_executor", lambda: None)
        orch = Orchestrator(str(tmp_path / "none.yml"))
        ctx = JobContext("j1", "sandbox_analyze", "Archive.zip", "go", self._route(),
                         workspace_path=str(ws))
        result = orch._run_sandbox_analysis(ctx, str(ws), _profile())
        assert result and result["status"] == "skipped"
        assert "unavailable" in result["reason"]

    def test_non_sandbox_route_returns_none(self, tmp_path):
        orch = Orchestrator(str(tmp_path / "none.yml"))
        ctx = JobContext("j1", "chat", "Archive.zip", "go", {"framework_aware": False})
        assert orch._run_sandbox_analysis(ctx, "/tmp", None) is None

    def test_safe_project_id(self):
        orch = Orchestrator("/none.yml")
        assert orch._safe_project_id("My App_v2.zip") == "My_App_v2"
        assert orch._safe_project_id("../evil") == "evil"
        assert orch._safe_project_id("") == "project"
