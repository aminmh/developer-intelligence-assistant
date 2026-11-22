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

    def __init__(self, nodes, edges, *, down=False, fail_upstream=False, inherited=(), routes=(),
                 runtime_observed=False, producers=None):
        self.all_nodes = list(nodes)
        self.nodes = {n["fqn"]: n for n in nodes if n.get("collection") == COLLECTION}
        self.edges = edges          # rows as the upstream query returns them
        self.inherited = list(inherited)
        self.routes = list(routes)
        self.runtime_observed = runtime_observed
        self.producers = producers
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
            rows = [row for row in self.edges if row["target"] in set(params["ids"])
                    and row["n"].get("collection", COLLECTION) == params["collection"]]
            return rows[: params["limit"]]
        if "UNWIND $method_ids AS tid" in cypher:
            return [row for row in self.inherited if row["target"] in set(params["method_ids"])]
        if "UNWIND $route_ids AS tid" in cypher:
            return [row for row in self.routes if row["target"] in set(params["route_ids"])]
        if "UNWIND $symbol_ids AS sid" in cypher:
            return [{"n": n} for n in self.all_nodes
                    if n.get("collection") == params["collection"] and n.get("symbol_id") in set(params["symbol_ids"])]
        if "runtime_window_hours IS NOT NULL" in cypher:
            return [{"observed": self.runtime_observed}]
        if "SymbolCoverage" in cypher:
            return [{"producers": self.producers}] if self.producers is not None else []
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


# === Phase 2: diff and revision-pair inputs, inheritance, routes, tests, runtime, coverage =========

import io
import zipfile

from inspection.impact import analyze_diff, analyze_revisions, is_test_path

BASE = RevisionRef(project_id="p1", revision_id="r0", collection="sym_base", ready=True)


def _sym(fqn, sid, path, line, end_line, collection=COLLECTION, kind="method", **extra):
    node = _node(fqn, kind=kind, path=path, line=line, symbol_id=sid, end_line=end_line, **extra)
    node["collection"] = collection
    if collection != COLLECTION:
        node["id"] = "base-" + fqn
    return node


def _definitions(nodes):
    return {n["symbol_id"]: {"symbol_id": n["symbol_id"], "name": n["fqn"], "file": n["file"],
                             "line": n["line"], "end_line": n["end_line"]} for n in nodes}


def _archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for path, text in files.items():
            bundle.writestr(path, text)
    return buffer.getvalue()


REPO_PHP = "<?php\nclass Repo {\n  function find() {\n    return 1;\n  }\n  function save() {\n    return 2;\n  }\n}\n"
DIFF = """--- a/app/Repo.php
+++ b/app/Repo.php
@@ -3,3 +3,3 @@
   function find() {
-    return 1;
+    return 3;
   }
--- a/app/Notes.md
+++ b/app/Notes.md
@@ -1,1 +1,1 @@
-old
+new
"""


# --- T7: diff input -------------------------------------------------------------------------------

def test_diff_maps_to_changed_symbols():
    find = _sym("App\\Repo::find", "s-find", "app/Repo.php", 3, 5)
    save = _sym("App\\Repo::save", "s-save", "app/Repo.php", 6, 8)
    caller = _node("App\\Svc::run", path="app/Svc.php")
    graph = FakeGraph([find, save, caller], [_edge("App\\Repo::find", caller)])
    report = analyze_diff(_query(graph), REVISION, DIFF, _definitions([find, save]))
    assert [(c["fqn"], c["change"]) for c in report["changed"]] == [("App\\Repo::find", "modified")]
    assert [i["fqn"] for i in report["impacted"]] == ["App\\Svc::run"]
    assert report["unresolved"] == [{"input": "app/Notes.md:1-1", "reason": "no_symbol"}]


def test_diff_without_hunks_errors():
    graph = FakeGraph([], [])
    with pytest.raises(ImpactError, match="unified diff"):
        analyze_diff(_query(graph), REVISION, "just some text", {})
    with pytest.raises(ImpactError, match="200 KB"):
        analyze_diff(_query(graph), REVISION, "x" * (200 * 1024 + 1), {})


# --- T8: revision pair ----------------------------------------------------------------------------

def test_revision_pair_detects_added_removed_changed():
    base_find = _sym("App\\Repo::find", "s-find", "app/Repo.php", 3, 5, collection="sym_base")
    base_save = _sym("App\\Repo::save", "s-save", "app/Repo.php", 6, 8, collection="sym_base")
    base_gone = _sym("App\\Repo::gone", "s-gone", "app/Old.php", 2, 3, collection="sym_base")
    head_find = _sym("App\\Repo::find", "s-find", "app/Repo.php", 3, 5)
    head_save = _sym("App\\Repo::save", "s-save", "app/Repo.php", 6, 8)
    head_new = _sym("App\\Repo::fresh", "s-new", "app/New.php", 2, 3)
    old_user = _node("App\\Legacy::use", path="app/Legacy.php")
    old_user["collection"] = "sym_base"
    old_user["id"] = "base-App\\Legacy::use"
    graph = FakeGraph([base_find, base_save, base_gone, head_find, head_save, head_new, old_user],
                      [{**_edge("App\\Repo::gone", old_user), "target": "base-App\\Repo::gone"}])
    base_files = {"app/Repo.php": REPO_PHP, "app/Old.php": "<?php\nfunction gone() {\n}\n"}
    head_files = {"app/Repo.php": REPO_PHP.replace("return 1;", "return 9;"),
                  "app/New.php": "<?php\nfunction fresh() {\n}\n"}
    report = analyze_revisions(
        _query(graph), REVISION, BASE,
        head_definitions=_definitions([head_find, head_save, head_new]),
        base_definitions=_definitions([base_find, base_save, base_gone]),
        head_archive=lambda: _archive(head_files), base_archive=lambda: _archive(base_files))
    assert sorted((c["fqn"], c["change"]) for c in report["changed"]) == [
        ("App\\Repo::find", "modified"), ("App\\Repo::fresh", "added"), ("App\\Repo::gone", "removed")]
    legacy = [i for i in report["impacted"] if i["fqn"] == "App\\Legacy::use"]
    assert legacy and legacy[0]["revision_id"] == "r0"     # found through the base graph
    assert report["base_revision_id"] == "r0"


def test_base_revision_without_graph_unavailable():
    head = _sym("App\\Repo::find", "s-find", "app/Repo.php", 3, 5)
    report = analyze_revisions(
        _query(FakeGraph([head], [])), REVISION, BASE,
        head_definitions=_definitions([head]), base_definitions={},
        head_archive=lambda: _archive({"app/Repo.php": REPO_PHP}), base_archive=lambda: b"")
    assert report["coverage"] == "unavailable" and report["unavailable_revision"] == "r0"
    assert report["impacted"] == []
    with pytest.raises(ImpactError, match="not ready"):
        analyze_revisions(_query(FakeGraph([head], [])), REVISION, RevisionRef("p1", "r0", "sym_base", ready=False),
                          head_definitions=_definitions([head]), base_definitions={},
                          head_archive=lambda: b"", base_archive=lambda: b"")


# --- T9: inherited methods ------------------------------------------------------------------------

def test_inherited_method_callers_on_subclasses():
    method = _node("App\\Base::save", path="app/Base.php")
    via_sub = _node("App\\Ctl::store", path="app/Ctl.php")
    graph = FakeGraph([method, via_sub], [], inherited=[
        {**_edge("App\\Base::save", via_sub, origin="php-parser"), "to_member": "save", "via_class": "App\\Child"}])
    report = analyze_symbols(_query(graph), REVISION, ["App\\Base::save"])
    item = report["impacted"][0]
    assert item["fqn"] == "App\\Ctl::store" and item["edge"]["via_class"] == "App\\Child"


# --- T10: routes ----------------------------------------------------------------------------------

def test_routes_from_runtime_and_static_sources():
    nodes, edges = _chain()          # Repo::find <- Svc::run <- Ctl::show <- Router::dispatch
    invokable = _node("App\\Http\\Ping::__invoke", path="app/Ping.php")
    edges.append(_edge("App\\Repo::find", invokable))
    graph = FakeGraph([*nodes, invokable], edges, routes=[
        {"target": "id-App\\Ctl::show", "method": "GET", "route": "/orders/{id}"}])
    static = [{"method": "POST", "path": "/orders/{id}", "action": "App\\Ctl@show"},
              {"method": "GET", "path": "/ping", "action": "App\\Http\\Ping"},
              {"method": "GET", "path": "/home", "action": "Closure"}]
    report = analyze_symbols(_query(graph), REVISION, ["App\\Repo::find"], static_routes=static)
    routes = {(r["source"], r["method"], r["path"], r["fqn"], r["depth"]) for r in report["routes"]}
    assert routes == {("runtime", "GET", "/orders/{id}", "App\\Ctl::show", 2),
                      ("static", "POST", "/orders/{id}", "App\\Ctl::show", 2),
                      ("static", "GET", "/ping", "App\\Http\\Ping::__invoke", 1)}


# --- T11: tests and runtime confirmation ----------------------------------------------------------

def test_tests_collected_and_walk_stops():
    target = _node("App\\Repo::find", path="app/Repo.php")
    test = _node("Tests\\RepoTest::testFind", path="tests/Unit/RepoTest.php")
    runner = _node("Tests\\Suite::all", path="tests/Suite.php")
    graph = FakeGraph([target, test, runner], [_edge("App\\Repo::find", test), _edge("Tests\\RepoTest::testFind", runner)])
    report = analyze_symbols(_query(graph), REVISION, ["App\\Repo::find"])
    assert [t["fqn"] for t in report["tests"]] == ["Tests\\RepoTest::testFind"]
    assert [i["fqn"] for i in report["impacted"]] == ["Tests\\RepoTest::testFind"]
    assert report["impacted"][0]["test"] is True
    assert all(is_test_path(p) for p in ("tests/A.php", "src/FooTest.php", "pkg/test_x.py", "x_test.go",
                                         "web/a.spec.ts", "web/a.test.ts", "web/__tests__/a.js", "spec/a_spec.rb"))
    assert not any(is_test_path(p) for p in ("app/Testimonial.php", "app/Contest.php", "src/latest.py"))


def test_runtime_confirmation_marks():
    target = _node("App\\Svc::run")
    seen = _node("App\\A::seen", path="app/A.php")
    never = _node("App\\B::never", path="app/B.php")
    edges = [_edge("App\\Svc::run", seen), _edge("App\\Svc::run", never),
             _edge("App\\Svc::run", seen, rtype="RUNTIME_CALLS", kind=None, origin=None)]
    observed = analyze_symbols(_query(FakeGraph([target, seen, never], edges, runtime_observed=True)),
                               REVISION, ["App\\Svc::run"])
    marks = {i["fqn"]: (i["confirmed"], i["origin"]) for i in observed["impacted"]}
    assert marks == {"App\\A::seen": (True, "lsp"), "App\\B::never": (False, "lsp")}
    assert observed["runtime"] == {"observed": True}
    blind = analyze_symbols(_query(FakeGraph([target, never], edges[1:2])), REVISION, ["App\\Svc::run"])
    assert blind["impacted"][0]["confirmed"] is None


# --- T12: producer coverage -----------------------------------------------------------------------

def test_partial_producer_coverage_reported():
    nodes, edges = _chain()
    partial = analyze_symbols(_query(FakeGraph(nodes, edges, producers={
        "lsp": {"status": "partial", "languages": ["php"]}, "scip": {"status": "complete", "languages": ["python"]}})),
        REVISION, ["App\\Repo::find"])
    assert partial["coverage"] == "partial" and partial["coverage_reasons"] == ["lsp_partial"]
    assert partial["producers"]["lsp"]["status"] == "partial"
    complete = analyze_symbols(_query(FakeGraph(nodes, edges, producers={
        "lsp": {"status": "unavailable", "languages": []}})), REVISION, ["App\\Repo::find"])
    assert complete["coverage"] == "complete" and complete["coverage_reasons"] == []


def test_partial_producer_coverage_recorded_from_exports():
    from lsp.jobs import record_producer

    index = {"definitions": [], "references": []}
    stamped = record_producer(index, "lsp", {"status": "partial", "languages": {"php": {"status": "partial"}}})
    stamped = record_producer(stamped, "scip", {"status": "complete", "languages": {"python": {}}})
    assert stamped["producers"] == {"lsp": {"status": "partial", "languages": ["php"]},
                                    "scip": {"status": "complete", "languages": ["python"]}}
    assert "producers" not in index          # the input export is not mutated


def test_partial_producer_coverage_written_with_the_graph():
    from inspection.service import InspectionService

    class Graph:
        def __init__(self):
            self.coverage = None

        def upsert_nodes(self, rows):
            return len(rows)

        def upsert_references(self, rows):
            return len(rows)

        def upsert_declaration_relations(self, rows):
            return len(rows)

        def upsert_coverage(self, collection, producers):
            self.coverage = (collection, producers)
            return 1

        def _get_store(self):
            return None

    graph = Graph()
    definitions = [{"kind": "class", "name": "A", "fqn": "App\\A", "file": "/p/app/A.php", "line": 1,
                    "end_line": 3, "relations": []}]
    producers = {"lsp": {"status": "partial", "languages": ["php"]}}
    InspectionService(graph=graph).index("/p", "proj", exported={
        "definitions": definitions, "references": [], "producers": producers})
    assert graph.coverage == ("sym_proj", producers)
