"""Phase 3b of the LSP-first plan: runtime call edges (indexr-otel) in lookups and traces.

18g runtime callers/callees in symbol lookup; 18h confirmed first, unconfirmed never absent;
18i runtime-only edges in flow tracing; 18j edges from another revision are stale;
18k runtime coverage per revision.
"""

from __future__ import annotations

from agent_tools.navigation import _with_runtime
from inspection.queries import SymbolGraphQuery


def _item(symbol, path="app/A.php", line=3, origin="lsp"):
    return {"path": path, "line": line, "kind": "method", "symbol": symbol, "origin": origin, "language": "php"}


def _runtime(fqn, count=7):
    return {"fqn": fqn, "file": "app/R.php", "line": 9, "kind": "method",
            "ref": {"origin": "runtime", "file": "app/R.php", "line": 9, "count": count,
                    "last_seen": "2026-11-15T10:00:00Z", "sources": ["hook", "profile"]}}


def test_static_edges_seen_running_are_confirmed_and_runtime_only_edges_are_added():
    items = [_item("App\\A::seen"), _item("App\\B::never", line=8)]
    out = _with_runtime(items, [_runtime("App\\A::seen", 12), _runtime("App\\Facade::magic")], True, None)
    by_symbol = {i["symbol"]: i for i in out}
    assert by_symbol["App\\A::seen"]["runtime"] == "confirmed" and by_symbol["App\\A::seen"]["runtime_count"] == 12
    assert by_symbol["App\\B::never"]["runtime"] == "unconfirmed"
    extra = by_symbol["App\\Facade::magic"]
    assert extra["runtime"] == "runtime_only" and extra["origin"] == "runtime"
    assert extra["sources"] == ["hook", "profile"] and extra["path"] == "app/R.php"


def test_without_runtime_data_static_edges_are_not_marked():
    out = _with_runtime([_item("App\\A::f")], [], False, None)
    assert "runtime" not in out[0]


class _Store:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def structured_query(self, cypher, param_map=None):
        self.calls.append((cypher, param_map))
        for marker, rows in self.responses:
            if marker in cypher:
                return rows
        return []


class _Graph:
    def __init__(self, store):
        self.store = store

    def _get_store(self):
        return self.store


def _node(fqn, **extra):
    return {"id": "id-" + fqn, "kind": "method", "name": fqn.split("::")[-1], "fqn": fqn,
            "file": "app/X.php", "line": 2, "collection": "sym_x", **extra}


def test_call_hierarchy_returns_current_runtime_edges_and_counts_stale_ones():
    target = _node("App\\T::run", runtime_window_hours=24, runtime_count_rolling=3)
    store = _Store([
        ("collection: $collection, fqn: $fqn", [{"n": target}]),
        ("<-[r:RUNTIME_CALLS]-", [
            {"n": _node("App\\C::now"), "calls": 5, "last_seen": "t", "sources": ["hook"], "current": True},
            {"n": _node("App\\C::old"), "calls": 9, "last_seen": "t", "sources": ["span"], "current": False}]),
        ("-[r:RUNTIME_CALLS]->", []),
    ])
    out = SymbolGraphQuery(_Graph(store)).call_hierarchy("sym_x", "App\\T::run")
    assert [r["fqn"] for r in out["runtime_callers"]] == ["App\\C::now"]
    assert out["runtime_callers"][0]["ref"]["origin"] == "runtime" and out["runtime_callers"][0]["ref"]["count"] == 5
    assert out["runtime_stale_edges"] == 1
    assert out["runtime_observed"] is True


def test_runtime_coverage_reports_share_of_methods_seen():
    store = _Store([
        ("WHERE s.kind IN ['method', 'function']", [{"total": 40, "seen": 10, "window_hours": 24, "last_seen": "t"}]),
        ("RuntimeRoute", [{"routes": 6}]),
    ])
    out = SymbolGraphQuery(_Graph(store)).runtime_coverage("sym_x")
    assert out == {"observed": True, "methods_seen": 10, "methods_total": 40, "percent": 25.0,
                   "routes_seen": 6, "window_hours": 24, "last_seen": "t"}


def test_runtime_coverage_without_aggregation_has_no_percent():
    store = _Store([("WHERE s.kind IN ['method', 'function']", [{"total": 40, "seen": 0, "window_hours": None}])])
    out = SymbolGraphQuery(_Graph(store)).runtime_coverage("sym_x")
    assert out["observed"] is False and out["percent"] is None


def test_runtime_facts_are_stamped_with_their_revision():
    from inspection.graph import InspectionGraph

    store = _Store([])
    graph = InspectionGraph()
    graph._store = store
    graph.upsert_runtime_facts("sym_x", {"symbols": [{"symbol_id": "s"}], "calls": [], "routes": [], "tables": []},
                               revision_id="rev_" + "1" * 32)
    calls_statement = next(c for c in store.calls if "RUNTIME_CALLS" in c[0] and "MERGE" in c[0])
    assert "r.revision_id = $revision_id" in calls_statement[0]
    assert calls_statement[1]["revision_id"] == "rev_" + "1" * 32


def test_runtime_reference_names_the_revisions_own_snapshot():
    from telemetry.snapshot import runtime_reference

    sha = "a" * 64
    # Working trees have no commit (only a base commit in registry metadata): before, aggregation skipped them.
    assert runtime_reference("p", {"commit_sha": None, "source_archive_sha256": sha, "dirty": True}) == "wt_" + sha
    assert runtime_reference("p", {"commit_sha": None, "source_archive_sha256": sha}) == "upload_" + sha
    assert runtime_reference("p", {"commit_sha": "c" * 40, "source_archive_sha256": sha}) == "c" * 40
    assert runtime_reference("p", {"commit_sha": "c" * 40}) == "c" * 40  # rows without archive identity


def test_flow_tracing_uses_runtime_edges_only_where_no_static_edge_exists():
    from tests.test_code_quality_flow_tracing import BUSINESS_FILES, PROJECT, REVISION, _chunk, _snapshot
    from code_quality.flow_tracing import build_call_traces

    chunks = [
        _chunk("App\\Controller::create", "src/Controller.php", 10,
               "$v = $_POST['v']; Facade::save($v);", ["__unknown::save"]),
        _chunk("App\\Service::save", "src/Service.php", 20, "$db->query($v);"),
    ]
    runtime_only = [{"caller": "App\\Controller::create", "callee": "App\\Service::save", "origin": "runtime"}]
    summary, evidence = build_call_traces(PROJECT, REVISION, _snapshot(), chunks, BUSINESS_FILES,
                                          graph_edges=runtime_only)
    assert summary["trace_count"] == 1 and evidence[0]["trace"]["edges"][0]["origin"] == "runtime"
    both = [*runtime_only, {"caller": "App\\Controller::create", "callee": "App\\Service::save", "origin": "lsp"}]
    summary, evidence = build_call_traces(PROJECT, REVISION, _snapshot(), chunks, BUSINESS_FILES, graph_edges=both)
    assert evidence[0]["trace"]["edges"][0]["origin"] == "lsp" and summary["edge_origins"] == ["lsp"]
