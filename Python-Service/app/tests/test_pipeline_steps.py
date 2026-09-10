"""Tests for the orchestration step abstraction (pipelines.steps)."""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import yaml

from pipelines.steps import SharedState, build_steps, run_plan


def test_documentation_retrieval_is_limited_to_code_changes():
    config_path = Path(__file__).resolve().parents[1] / "config" / "orchestrator.yml"
    routes = yaml.safe_load(config_path.read_text(encoding="utf-8"))["routes"]

    assert routes["code_refactor"]["doc_retrieval"] is True
    assert routes["code_explain"].get("doc_retrieval", False) is False
    assert routes["knowledge"].get("doc_retrieval", False) is False
    assert routes["debug"].get("doc_retrieval", False) is False


class _FakeRunner:
    """Records capability calls in the order steps make them."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.step_marks: list[tuple[str, str]] = []
        self.fake_profile = {
            "framework": "laravel",
            "version": "12",
            "routes": [{"method": "GET", "uri": "/", "action": "C@x", "name": None, "middleware": [], "file": "web.php"}],
        }
        self.fake_doc = {"chunks": ["a"], "provenance": [], "provider": "context7", "warning": None}

    # helpers steps invoke -------------------------------------------------
    def _update_step(self, job_id: str, key: str, status: str, **extra):
        self.step_marks.append((key, status))

    def _prepare_workspace(self, ctx):
        self.calls.append("prepare")
        return "/ws/1", 7

    def _select_tool(self, ctx):
        self.calls.append("select_tool")
        return "chunk_code"

    def _call_php_tool(self, ctx, tool_name, workspace_path):
        self.calls.append(f"tool:{tool_name}")
        return {"success": True, "chunks": []}

    def _extract_tool_output(self, tool_result):
        return "raw-out"

    def _run_post_process_pipeline(self, ctx, result, tool_name=None, tool_result=None):
        self.calls.append("post_process")
        return result, "chunk_code_Archive"

    def _collect_doc_context(self, ctx, workspace_path, post_process_result):
        self.calls.append("docs")
        return self.fake_doc if (ctx.route or {}).get("doc_retrieval") else None

    def _collect_project_profile(self, ctx, workspace_path):
        self.calls.append("framework")
        return self.fake_profile if (ctx.route or {}).get("framework_aware") else None

    def _run_sandbox_analysis(self, ctx, workspace_path, project_profile):
        self.calls.append("sandbox")
        if not (ctx.route or {}).get("sandbox"):
            return None
        return {"status": "done", "install": {"status": "done", "exit_code": 0}, "probes": []}

    def _merge_runtime_routes(self, project_profile, sandbox_result):
        self.calls.append("merge_routes")
        return project_profile

    def _synthesize_with_llamaindex(self, **kwargs):
        self.calls.append("synthesize")
        return "synth-out"

    def _save_outputs(self, job_id, **kwargs):
        self.calls.append("save_outputs")

    def _persist_result_bundle(self, ctx, doc_context, project_profile, sandbox_result):
        self.calls.append("persist_bundle")


class _Ctx:
    def __init__(self, route: dict) -> None:
        self.job_id = "j1"
        self.route = route
        self.route_name = "sandbox_analyze"
        self.filename = "Archive.zip"
        self.prompt = "analyze"


def _full_route() -> dict:
    return {
        "tool": "chunk_code",
        "llamaindex": True,
        "doc_retrieval": True,
        "framework_aware": True,
        "sandbox": True,
        "sandbox_probes": ["routes", "schedule"],
        "post_process": [{"action": "llamaindex_ingest"}],
    }


class TestBuildSteps:
    def test_full_route_plan_order(self):
        steps = build_steps(_full_route())
        keys = [s["key"] for s in steps]
        assert keys == [
            "extracting", "routing", "tool", "doc_retrieval",
            "framework", "sandbox", "post_llamaindex_ingest",
        ]
        assert steps[2]["label"].startswith("Running chunk_code")

    def test_bare_route_keeps_core(self):
        steps = build_steps({"tool": None, "llamaindex": True})
        keys = [s["key"] for s in steps]
        assert keys == ["extracting", "routing", "tool"]  # tool listed when no explicit tool

    def test_sandbox_requires_flag(self):
        steps = build_steps({"tool": "x", "llamaindex": False})
        assert "sandbox" not in [s["key"] for s in steps]


class TestRunPlan:
    def test_step_sequence_and_shared_state(self):
        runner = _FakeRunner()
        shared = run_plan(runner, _Ctx(_full_route()), SharedState())
        # capability call order (execute plan, not UI list)
        assert runner.calls == [
            "prepare",
            "select_tool",
            "tool:chunk_code",
            "post_process",
            "docs",
            "framework",
            "sandbox",
            "merge_routes",
            "synthesize",
            "save_outputs",
            "persist_bundle",
        ]
        assert shared.workspace_path == "/ws/1"
        assert shared.collection_name == "chunk_code_Archive"
        assert shared.output == "synth-out"
        assert shared.project_profile["framework"] == "laravel"
        assert shared.sandbox_result["status"] == "done"
        # UI markers emitted for every enabled phase
        marks = dict(runner.step_marks)
        assert marks["extracting"] == "done"
        assert marks["tool"] == "done"
        assert marks["doc_retrieval"] == "done"
        assert marks["framework"] == "done"
        assert marks["sandbox"] == "done"

    def test_off_route_steps_noop(self):
        runner = _FakeRunner()
        shared = run_plan(runner, _Ctx({"tool": None, "llamaindex": True}), SharedState())
        # helpers are always invoked (they no-op off-route, exactly like the
        # guarded real helpers) — results stay None and no UI markers fire
        assert "docs" in runner.calls and "framework" in runner.calls and "sandbox" in runner.calls
        assert shared.doc_context is None
        assert shared.project_profile is None
        assert shared.sandbox_result is None
        marks = dict(runner.step_marks)
        assert "doc_retrieval" not in marks and "framework" not in marks and "sandbox" not in marks

    def test_marker_detail_on_sandbox_unavailable(self):
        route = _full_route()

        class BoomRunner(_FakeRunner):
            def _run_sandbox_analysis(self, ctx, ws, profile):
                return None  # backend missing

        runner = BoomRunner()
        run_plan(runner, _Ctx(route), SharedState())
        assert ("sandbox", "done") in runner.step_marks


class TestOrchestratorDelegation:
    def test_build_steps_delegates_to_steps_module(self):
        from pipelines.orchestrator import orchestrator

        assert orchestrator._build_steps({"tool": "chunk_code"}) == build_steps({"tool": "chunk_code"})


class TestCollectProjectProfileCache:
    """Real `_collect_project_profile` cache path (sandbox bypass guard)."""

    @staticmethod
    def _fresh_profile() -> dict:
        return {"framework": "laravel", "version": "12", "routes": [], "controllers": []}

    def test_non_sandbox_reuses_cached_profile(self, monkeypatch, tmp_path):
        from pipelines.orchestrator import Orchestrator
        from projects import knowledge_cache, project_profile

        runner = Orchestrator.__new__(Orchestrator)
        ws = tmp_path / "ws"
        ws.mkdir()
        store: dict[str, dict] = {}
        monkeypatch.setattr(knowledge_cache, "get", lambda filename: store.get(filename))
        monkeypatch.setattr(knowledge_cache, "put", lambda filename, profile: store.__setitem__(filename, profile))
        builds: list[str] = []
        monkeypatch.setattr(
            project_profile,
            "build_project_profile",
            lambda workspace_path: builds.append(workspace_path) or self._fresh_profile(),
        )
        ctx = SimpleNamespace(
            job_id="j1", route_name="chat", filename="LaravelApp.zip", route={"framework_aware": True}
        )

        first = runner._collect_project_profile(ctx, str(ws))
        second = runner._collect_project_profile(ctx, str(ws))

        assert builds == [str(ws)]  # built once; second call hit the cache
        assert first == second == self._fresh_profile()
        assert store["LaravelApp.zip"] == self._fresh_profile()

    def test_sandbox_route_bypasses_cache(self, monkeypatch, tmp_path):
        from pipelines.orchestrator import Orchestrator
        from projects import knowledge_cache, project_profile

        runner = Orchestrator.__new__(Orchestrator)
        ws = tmp_path / "ws"
        ws.mkdir()
        stale = {"framework": "laravel", "routes": [{"method": "GET", "uri": "/old", "action": "C@old"}]}
        monkeypatch.setattr(knowledge_cache, "get", lambda filename: stale)
        builds: list[str] = []
        monkeypatch.setattr(
            project_profile,
            "build_project_profile",
            lambda workspace_path: builds.append(workspace_path) or self._fresh_profile(),
        )
        ctx = SimpleNamespace(
            job_id="j2",
            route_name="sandbox_analyze",
            filename="LaravelApp.zip",
            route={"framework_aware": True, "sandbox": True},
        )

        result = runner._collect_project_profile(ctx, str(ws))

        assert builds == [str(ws)]  # sandbox always re-derives, stale cache ignored
        assert result == self._fresh_profile()  # fresh build, not the stale cached dict
