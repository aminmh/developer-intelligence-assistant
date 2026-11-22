"""Change impact surfaces: the impact service and POST /code-tools/impact.

@.archcore/agent-tools/change-impact-analysis.spec.md clauses 1, 15 and failure clauses 1-2;
Phase 3 of @.archcore/agent-tools/implement-change-impact-analysis.plan.md.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from inspection import impact_service
from inspection.impact_service import ImpactRequestError, ImpactUnavailable, run_impact

ROOT = "/app/data/cache/rev_1"
ROW = {"revision_id": "rev_1", "state": "ready", "commit_sha": "c" * 40, "source_kind": "git",
       "collection_name": "kb_rev_1"}


def _node(fqn, path, line, **extra):
    return {"id": "id-" + fqn, "kind": "method", "name": fqn.split("::")[-1], "fqn": fqn, "file": path,
            "line": line, "project_id": "pizza", "language": "php", **extra}


class Graph:
    """Answers the impact queries for one changed method and one caller."""

    def __init__(self):
        self.collections = []
        self.target = _node("App\\Repo::find", f"{ROOT}/app/Repo.php", 10, symbol_id="s-find")
        self.caller = _node("App\\Http\\OrderController::show", f"{ROOT}/app/Http/OrderController.php", 20)

    def _get_store(self):
        return self

    def structured_query(self, cypher, param_map=None):
        params = param_map or {}
        self.collections.append(params.get("collection"))
        if "UNWIND $ids AS tid" in cypher:
            return [{"target": "id-App\\Repo::find", "n": self.caller, "rtype": "REFERENCES", "rkind": "method_call",
                     "file": f"{ROOT}/app/Http/OrderController.php", "line": 22, "origin": "lsp"}] \
                if "id-App\\Repo::find" in params["ids"] else []
        if "fqn: $fqn" in cypher:
            return [{"n": self.target}] if params["fqn"] == "App\\Repo::find" else []
        if "runtime_window_hours IS NOT NULL" in cypher:
            return [{"observed": False}]
        return []


class Registry:
    def get_by_revision_id(self, project_id, revision_id):
        if project_id == "pizza" and revision_id == "rev_1":
            return dict(ROW)
        if project_id == "pizza" and revision_id == "rev_busy":
            return {**ROW, "revision_id": "rev_busy", "state": "indexing"}
        return None

    def latest_ready(self, project_id):
        return dict(ROW) if project_id == "pizza" else None


def _root():
    return SimpleNamespace(relative=lambda p: p[len(ROOT) + 1:] if str(p).startswith(ROOT + "/") else None,
                           wrapper_prefix="")


@pytest.fixture
def deps(monkeypatch):
    import telemetry.registry as registry_module

    graph = Graph()
    # SymbolGraphQuery reads a snapshot collection only once the registry lists it as ready.
    monkeypatch.setattr(registry_module, "default_registry",
                        lambda: SimpleNamespace(is_ready_collection=lambda name: name == "snapshot_rev_1"))
    monkeypatch.setattr(impact_service, "_registry", lambda: Registry())
    monkeypatch.setattr(impact_service, "_graph_query", lambda: _query(graph))
    monkeypatch.setattr(impact_service, "_source_root", lambda project_id, revision_id: _root())
    monkeypatch.setattr(impact_service, "_static_routes", lambda project_id, revision_id: [
        {"method": "GET", "path": "/orders/{id}", "action": "App\\Http\\OrderController@show"}])
    monkeypatch.setattr(impact_service, "_collection", lambda project_id, row: "sym_snapshot_" + row["revision_id"])
    return graph


def _query(graph):
    from inspection.queries import SymbolGraphQuery

    return SymbolGraphQuery(graph)


# --- T13: impact service --------------------------------------------------------------------------

def test_service_resolves_snapshot_collection_and_routes(deps):
    report = run_impact("pizza", "rev_1", {"symbols": ["App\\Repo::find"]})
    assert set(deps.collections) == {"sym_snapshot_rev_1"}
    assert report["revision_id"] == "rev_1" and report["project_id"] == "pizza"
    assert report["changed"][0]["path"] == "app/Repo.php"
    impacted = report["impacted"][0]
    assert impacted["path"] == "app/Http/OrderController.php" and impacted["edge"]["path"] == "app/Http/OrderController.php"
    assert report["routes"] == [{"source": "static", "method": "GET", "path": "/orders/{id}",
                                 "fqn": "App\\Http\\OrderController::show", "depth": 1,
                                 "handler_path": "app/Http/OrderController.php", "handler_line": 20}]


def test_service_resolves_latest_and_rejects_unready_or_unknown_revisions(deps):
    assert run_impact("pizza", "latest", {"symbols": ["App\\Repo::find"]})["revision_id"] == "rev_1"
    with pytest.raises(ImpactUnavailable, match="not ready"):
        run_impact("pizza", "rev_busy", {"symbols": ["App\\Repo::find"]})
    with pytest.raises(ImpactUnavailable, match="does not exist"):
        run_impact("pizza", "rev_9", {"symbols": ["App\\Repo::find"]})
    assert deps.collections == ["sym_snapshot_rev_1"] * len(deps.collections)


def test_service_paths_outside_the_revision_are_dropped(deps):
    deps.caller["file"] = "/etc/passwd"
    report = run_impact("pizza", "rev_1", {"symbols": ["App\\Repo::find"]})
    assert report["impacted"][0]["path"] == ""


# --- T15: HTTP route ------------------------------------------------------------------------------

def _client():
    from routers.code_tools import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_requires_exactly_one_input_kind(deps):
    client = _client()
    base = {"project_id": "pizza", "revision_id": "rev_1"}
    assert client.post("/code-tools/impact", json=base).status_code == 422
    both = client.post("/code-tools/impact", json={**base, "symbols": ["x"], "diff": "--- a/x\n"})
    assert both.status_code == 422 and "exactly one" in both.json()["detail"]
    assert client.post("/code-tools/impact", json={**base, "symbols": "App\\Repo::find"}).status_code == 422
    assert client.post("/code-tools/impact", json={**base, "symbols": ["x"], "depth": "deep"}).status_code == 422
    assert client.post("/code-tools/impact", json={"revision_id": "rev_1", "symbols": ["x"]}).status_code == 422
    assert client.post("/code-tools/impact", json={**base, "revision_id": "rev_busy",
                                                   "symbols": ["x"]}).status_code == 409


def test_route_matches_tool_report(deps):
    from agent_tools.navigation import impact_report
    from agent_tools.types import ToolContext

    body = {"project_id": "pizza", "revision_id": "rev_1", "symbols": ["App\\Repo::find"], "depth": 2}
    via_route = _client().post("/code-tools/impact", json=body).json()
    via_tool = impact_report({"symbols": ["App\\Repo::find"], "depth": 2},
                             ToolContext(workflow="chat", project_id="pizza", revision_id="rev_1"))
    assert via_route == via_tool
    assert via_route["impacted"][0]["fqn"] == "App\\Http\\OrderController::show"


def test_service_input_errors_are_request_errors(deps):
    with pytest.raises(ImpactRequestError, match="exactly one"):
        run_impact("pizza", "rev_1", {})
    with pytest.raises(ImpactRequestError, match="list"):
        run_impact("pizza", "rev_1", {"symbols": "App\\Repo::find"})
