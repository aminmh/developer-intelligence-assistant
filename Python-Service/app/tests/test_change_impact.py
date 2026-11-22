"""Change impact analysis over the Symbol graph: @.archcore/agent-tools/change-impact-analysis.spec.md.

Phase 1 of @.archcore/agent-tools/implement-change-impact-analysis.plan.md: the engine core over
symbol names (clauses 1, 3, 4, 6, 7, 8, 13 and failure clauses 1, 4, 6, 7).
"""

from __future__ import annotations

import pytest

from inspection.impact import ImpactError, RevisionRef, analyze_symbols
from inspection.queries import SymbolGraphQuery

COLLECTION = "sym_rev"
REVISION = RevisionRef(project_id="p1", revision_id="r1", collection=COLLECTION, ready=True)


def _node(fqn, kind="method", path="app/X.php", line=2, **extra):
    return {"id": "id-" + fqn, "kind": kind, "name": fqn.split("::")[-1].split("\\")[-1], "fqn": fqn,
            "file": path, "line": line, "collection": COLLECTION, "language": "php", "project_id": "p1", **extra}


def _edge(target, caller, rtype="REFERENCES", kind="method_call", origin="lsp", path=None, line=5):
    return {"target": "id-" + target, "n": caller, "rtype": rtype, "rkind": kind,
            "file": path or caller["file"], "line": line, "to_member": None, "origin": origin}


class FakeGraph:
    """In-memory Symbol graph answering the impact queries by Cypher marker."""

    def __init__(self, nodes, edges, *, down=False, fail_upstream=False):
        self.nodes = {n["fqn"]: n for n in nodes}
        self.edges = edges          # rows as the upstream query returns them
        self.down = down
        self.fail_upstream = fail_upstream
        self.upstream_calls = []

    def _get_store(self):
        return None if self.down else self

    def structured_query(self, cypher, param_map=None):
        params = param_map or {}
        if "UNWIND $ids AS tid" in cypher:
            if self.fail_upstream:
                raise RuntimeError("neo4j went away")
            self.upstream_calls.append(sorted(params["ids"]))
            rows = [row for row in self.edges if row["target"] in set(params["ids"])]
            return rows[: params["limit"]]
        if "fqn: $fqn" in cypher:
            node = self.nodes.get(params["fqn"])
            return [{"n": node}] if node else []
        if "name: $name" in cypher:
            return [{"n": n} for n in self.nodes.values() if n["name"] == params["name"]]
        if "ENDS WITH $suffix" in cypher:
            return [{"n": n} for n in self.nodes.values()
                    if n["kind"] == "function" and n["fqn"].endswith(params["suffix"])]
        return []


def _query(graph):
    return SymbolGraphQuery(graph)


# --- T1: batched upstream neighbours ---------------------------------------------------------------

def test_walks_callers_references_implementations_runtime():
    target = _node("App\\Svc::run")
    iface = _node("App\\Contract", kind="interface")
    caller = _node("App\\Ctl::show", path="app/Ctl.php")
    user = _node("App\\Typed::make", path="app/Typed.php")
    impl = _node("App\\Impl", kind="class", path="app/Impl.php")
    seen = _node("App\\Job::handle", path="app/Job.php")
    graph = FakeGraph([target, iface, caller, user, impl, seen], [
        _edge("App\\Svc::run", caller),
        _edge("App\\Svc::run", user, kind="type_hint"),
        _edge("App\\Contract", impl, rtype="DECLARES", kind="implements"),
        _edge("App\\Svc::run", seen, rtype="RUNTIME_CALLS", kind=None, origin=None),
    ])
    rows = _query(graph).upstream(COLLECTION, ["id-App\\Svc::run", "id-App\\Contract"])
    by_fqn = {row["fqn"]: row for row in rows}
    assert set(by_fqn) == {"App\\Ctl::show", "App\\Typed::make", "App\\Impl", "App\\Job::handle"}
    assert by_fqn["App\\Impl"]["ref"]["kind"] == "implements"
    assert by_fqn["App\\Job::handle"]["ref"]["origin"] == "runtime"
    assert by_fqn["App\\Ctl::show"]["target"] == "id-App\\Svc::run"
    assert graph.upstream_calls == [["id-App\\Contract", "id-App\\Svc::run"]]   # one batched read


def test_upstream_returns_none_when_the_graph_cannot_be_read():
    assert _query(FakeGraph([], [], down=True)).upstream(COLLECTION, ["x"]) is None
    assert _query(FakeGraph([], [], fail_upstream=True)).upstream(COLLECTION, ["x"]) is None


# --- T2: resolution, walk, via paths, readiness ---------------------------------------------------

def _chain():
    """Repo::find <- Svc::run <- Ctl::show <- Router::dispatch"""
    repo = _node("App\\Repo::find", path="app/Repo.php", line=10)
    svc = _node("App\\Svc::run", path="app/Svc.php", line=20)
    ctl = _node("App\\Ctl::show", path="app/Ctl.php", line=30)
    router = _node("App\\Router::dispatch", path="app/Router.php", line=40)
    edges = [_edge("App\\Repo::find", svc), _edge("App\\Svc::run", ctl), _edge("App\\Ctl::show", router)]
    return [repo, svc, ctl, router], edges


def test_symbol_names_resolve_by_fqn_then_name():
    nodes, edges = _chain()
    fn = _node("App\\helpers\\money_format", kind="function", path="app/helpers.php")
    report = analyze_symbols(_query(FakeGraph([*nodes, fn], edges)), REVISION,
                             ["App\\Repo::find", "run", "money_format"])
    assert [c["fqn"] for c in report["changed"]] == ["App\\Repo::find", "App\\Svc::run", "App\\helpers\\money_format"]
    assert report["unresolved"] == []


def test_ambiguous_name_marks_all_matches():
    a = _node("App\\A::save", path="app/A.php")
    b = _node("App\\B::save", path="app/B.php")
    report = analyze_symbols(_query(FakeGraph([a, b], [])), REVISION, ["save"])
    assert {c["fqn"] for c in report["changed"]} == {"App\\A::save", "App\\B::save"}
    assert report["ambiguous"] == [{"input": "save", "matches": 2}]


def test_records_depth_and_shortest_via():
    nodes, edges = _chain()
    # A shortcut makes Router reachable at depth 2 as well as depth 3.
    edges.append(_edge("App\\Svc::run", nodes[3], line=99))
    report = analyze_symbols(_query(FakeGraph(nodes, edges)), REVISION, ["App\\Repo::find"])
    by_fqn = {i["fqn"]: i for i in report["impacted"]}
    assert by_fqn["App\\Svc::run"]["depth"] == 1
    assert by_fqn["App\\Router::dispatch"]["depth"] == 2
    assert by_fqn["App\\Router::dispatch"]["via"] == ["App\\Router::dispatch", "App\\Svc::run", "App\\Repo::find"]
    assert by_fqn["App\\Ctl::show"]["via"] == ["App\\Ctl::show", "App\\Svc::run", "App\\Repo::find"]


def test_changed_symbols_are_not_reported_as_impacted():
    nodes, edges = _chain()
    report = analyze_symbols(_query(FakeGraph(nodes, edges)), REVISION, ["App\\Repo::find", "App\\Svc::run"])
    assert "App\\Svc::run" not in {i["fqn"] for i in report["impacted"]}
    assert {i["fqn"]: i["depth"] for i in report["impacted"]}["App\\Ctl::show"] == 1


def test_rejects_unready_revision():
    graph = FakeGraph(*_chain())
    with pytest.raises(ImpactError, match="not ready"):
        analyze_symbols(_query(graph), RevisionRef("p1", "r1", COLLECTION, ready=False), ["App\\Repo::find"])
    with pytest.raises(ImpactError, match="no symbol graph"):
        analyze_symbols(_query(graph), RevisionRef("p1", "r1", "", ready=True), ["App\\Repo::find"])
    assert graph.upstream_calls == []


# --- T3: origin preference ------------------------------------------------------------------------

def test_keeps_most_precise_origin():
    target = _node("App\\Svc::run")
    caller = _node("App\\Ctl::show", path="app/Ctl.php")
    graph = FakeGraph([target, caller], [
        _edge("App\\Svc::run", caller, origin="php-parser", line=7),
        _edge("App\\Svc::run", caller, rtype="RUNTIME_CALLS", kind=None, origin=None),
        _edge("App\\Svc::run", caller, origin="lsp", line=7),
    ])
    item = analyze_symbols(_query(graph), REVISION, ["App\\Svc::run"])["impacted"][0]
    assert item["origin"] == "lsp" and item["edge"]["origin"] == "lsp"
    assert item["edge"]["line"] == 7


# --- T4: limits -----------------------------------------------------------------------------------

def _hub(callers):
    hub = _node("App\\Api::respond", path="app/Api.php")
    nodes = [hub] + [_node(f"App\\C{i:04d}::act", path=f"app/C{i:04d}.php") for i in range(callers)]
    return nodes, [_edge("App\\Api::respond", n) for n in nodes[1:]]


def test_limits_truncate_with_reason():
    nodes, edges = _hub(2500)
    report = analyze_symbols(_query(FakeGraph(nodes, edges)), REVISION, ["App\\Api::respond"], max_nodes=50)
    assert len(report["impacted"]) == 50 and report["truncated"] is True
    assert report["limit_reached"] == "nodes" and report["coverage"] == "partial"
    assert report["impacted"][0]["fqn"] == "App\\C0000::act"          # stable order before the cut

    chain, chain_edges = _chain()
    deep = analyze_symbols(_query(FakeGraph(chain, chain_edges)), REVISION, ["App\\Repo::find"], depth=1)
    assert [i["fqn"] for i in deep["impacted"]] == ["App\\Svc::run"]
    assert deep["limit_reached"] == "depth" and deep["truncated"] is True

    ticks = iter([0.0, 0.0, 11.0, 11.0, 11.0, 11.0])
    slow = analyze_symbols(_query(FakeGraph(chain, chain_edges)), REVISION, ["App\\Repo::find"],
                           clock=lambda: next(ticks))
    assert slow["limit_reached"] == "time" and slow["truncated"] is True


def test_limits_are_clamped_to_the_maximums():
    chain, chain_edges = _chain()
    report = analyze_symbols(_query(FakeGraph(chain, chain_edges)), REVISION, ["App\\Repo::find"],
                             depth=99, max_nodes=10**6)
    assert report["depth"] == 6 and report["max_nodes"] == 2000
    assert report["limit_reached"] is None and report["coverage"] == "complete"


# --- T5: unavailable graph and unresolved inputs ---------------------------------------------------

def test_unresolved_inputs_empty_report():
    report = analyze_symbols(_query(FakeGraph(*_chain())), REVISION, ["App\\Nope::gone", "  "])
    assert report["changed"] == [] and report["impacted"] == []
    assert report["unresolved"] == [{"input": "App\\Nope::gone", "reason": "not_found"},
                                    {"input": "", "reason": "empty"}]


def test_graph_down_unavailable():
    down = analyze_symbols(_query(FakeGraph([], [], down=True)), REVISION, ["App\\Repo::find"])
    assert down["coverage"] == "unavailable" and down["impacted"] == [] and down["changed"] == []
    nodes, edges = _chain()
    broken = analyze_symbols(_query(FakeGraph(nodes, edges, fail_upstream=True)), REVISION, ["App\\Repo::find"])
    assert broken["coverage"] == "unavailable" and broken["impacted"] == []


# --- T6: ordering ---------------------------------------------------------------------------------

def test_impacted_order():
    target = _node("App\\Svc::run")
    b = _node("App\\B::x", path="app/B.php", line=1)
    a_late = _node("App\\A::y", path="app/A.php", line=50)
    a_early = _node("App\\A::z", path="app/A.php", line=5)
    seen = _node("App\\Z::seen", path="app/Z.php")
    deeper = _node("App\\0::top", path="app/0.php")
    graph = FakeGraph([target, b, a_late, a_early, seen, deeper], [
        _edge("App\\Svc::run", b), _edge("App\\Svc::run", a_late), _edge("App\\Svc::run", a_early),
        _edge("App\\Svc::run", seen, rtype="RUNTIME_CALLS", kind=None, origin=None),
        _edge("App\\B::x", deeper),
    ])
    report = analyze_symbols(_query(graph), REVISION, ["App\\Svc::run"])
    assert [i["fqn"] for i in report["impacted"]] == [
        "App\\Z::seen", "App\\A::z", "App\\A::y", "App\\B::x", "App\\0::top"]
    assert report["impacted"][0]["confirmed"] is True
