from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


def test_configured_neo4j_is_ready_for_graph_queries():
    from index.llamaindex_layer import CodePropertyGraph

    store = CodePropertyGraph()._get_graph_store()
    assert store is not None, "Neo4j graph store is unavailable"
    assert store.structured_query("RETURN 1 AS ready") == [{"ready": 1}]


def test_graph_index_persists_and_retrieves_project_call_relationships():
    from index.llamaindex_layer import CodePropertyGraph, _chunk_node_id

    graph = CodePropertyGraph()
    store = graph._get_graph_store()
    assert store is not None, "Neo4j graph store is unavailable"

    suffix = uuid4().hex
    collection = f"integration_{suffix}"
    class_name = f"Integration{suffix}"
    caller = {
        "type": "method", "name": "caller", "method_name": "caller",
        "class_fqn": class_name, "fq_symbol_name": f"{class_name}::caller",
        "file": "integration/fixture.php", "start_line": 1,
        "code": "function caller() { $this->callee(); }",
        "calls": [f"{class_name}::callee"],
    }
    callee = {
        "type": "method", "name": "callee", "method_name": "callee",
        "class_fqn": class_name, "fq_symbol_name": f"{class_name}::callee",
        "file": "integration/fixture.php", "start_line": 2,
        "code": "function callee() {}",
    }

    try:
        node_count, relation_count = graph._ingest_graph(
            [caller, callee], collection, strict=True,
        )
        assert node_count == 2
        assert relation_count == 1

        neighbours = graph._traverse_neighbours(
            store, [_chunk_node_id(caller)], collection,
        )

        assert len(neighbours) == 1
        assert neighbours[0]["tgt"] == _chunk_node_id(callee)
        assert neighbours[0]["rel"] == "CALLS"
        assert neighbours[0]["collection"] == collection
    finally:
        store.structured_query(
            "MATCH (n:CodeSymbol {collection: $collection}) DETACH DELETE n",
            param_map={"collection": collection},
        )


def test_full_index_writes_vectors_and_graph_then_retrieves_graph_neighbours(monkeypatch):
    from index import llamaindex_layer
    from index.llamaindex_layer import CodePropertyGraph, _chunk_node_id

    graph = CodePropertyGraph()
    store = graph._get_graph_store()
    qdrant = graph._get_qdrant()
    assert store is not None, "Neo4j graph store is unavailable"

    suffix = uuid4().hex
    collection = f"integration_{suffix}"
    class_name = f"Integration{suffix}"
    caller = {
        "type": "method", "name": "caller", "method_name": "caller",
        "class_fqn": class_name, "fq_symbol_name": f"{class_name}::caller",
        "file": "integration/fixture.php", "start_line": 1,
        "code": "function caller() { $this->callee(); }",
        "calls": [f"{class_name}::callee"],
    }
    callee = {
        "type": "method", "name": "callee", "method_name": "callee",
        "class_fqn": class_name, "fq_symbol_name": f"{class_name}::callee",
        "file": "integration/fixture.php", "start_line": 2,
        "code": "function callee() {}",
    }
    monkeypatch.setattr(
        llamaindex_layer, "embed_texts",
        lambda texts, **_kwargs: [[0.1, 0.2, 0.3] for _ in texts],
    )

    try:
        result = graph.ingest(
            [caller, callee], collection, vector_collection_name=collection,
        )
        assert result["ingested"] == 2
        assert result["graph_nodes"] == 2
        assert result["graph_relations"] == 1
        assert qdrant.count(collection_name=collection, exact=True).count == 2

        neighbours = graph._traverse_neighbours(
            store, [_chunk_node_id(caller)], collection,
        )
        assert len(neighbours) == 1
        assert neighbours[0]["tgt"] == _chunk_node_id(callee)
    finally:
        store.structured_query(
            "MATCH (n:CodeSymbol {collection: $collection}) DETACH DELETE n",
            param_map={"collection": collection},
        )
        if qdrant.collection_exists(collection):
            qdrant.delete_collection(collection_name=collection)


def test_symbol_graph_relabels_legacy_nodes_and_serves_lsp_edges(monkeypatch):
    """Phase 2 of the LSP-first plan: `PhpSymbol` → `Symbol`, language and origin on every node."""
    from inspection import graph as graph_module
    from inspection.graph import InspectionGraph, node_key
    from inspection.service import InspectionService

    suffix = uuid4().hex
    legacy = f"sym_legacy_{suffix}"
    fresh = f"sym_lsp_{suffix}"
    monkeypatch.setattr(InspectionGraph, "_legacy_checked", False)
    graph = InspectionGraph()
    store = graph._get_store()
    assert store is not None, "Neo4j graph store is unavailable"
    try:
        # A graph written by the old code: PhpSymbol label, no language, no origin.
        store.structured_query(
            "CREATE (a:PhpSymbol {id: $a, collection: $c, kind: 'method', name: 'run', fqn: $run, file: '/w/A.php'})"
            "-[:REFERENCES {kind: 'method_call', file: '/w/A.php', line: 4}]->"
            "(b:PhpSymbol {id: $b, collection: $c, kind: 'method', name: 'go', fqn: $go, file: '/w/A.php'})",
            param_map={"a": node_key("method", "L\\A::run", "run") + suffix,
                       "b": node_key("method", "L\\A::go", "go"), "c": legacy,
                       "run": "L\\A::run", "go": "L\\A::go"},
        )
        InspectionGraph._legacy_checked = False
        assert graph_module.migrate_legacy_labels(store) >= 2
        rows = store.structured_query(
            "MATCH (s {collection: $c}) RETURN labels(s) AS labels, s.language AS language ORDER BY s.name",
            param_map={"c": legacy})
        assert [r["labels"] for r in rows] == [["Symbol"], ["Symbol"]]
        assert {r["language"] for r in rows} == {"php"}
        callers = InspectionService(graph=graph).call_hierarchy(legacy, "L\\A::go", name="go")
        assert [c["fqn"] for c in callers["callers"]] == ["L\\A::run"]
        assert callers["callers"][0]["ref"]["origin"] == "php-parser"  # legacy edge, no stored origin

        # A fresh LSP export through the normal writer.
        exported = {
            "definitions": [
                {"kind": "class", "name": "Cart", "fqn": "App\\Cart", "file": "app/Cart.php", "line": 3, "origin": "lsp"},
                {"kind": "method", "name": "total", "fqn": "App\\Cart::total", "owner": "App\\Cart",
                 "file": "app/Cart.php", "line": 5, "origin": "lsp"},
                {"kind": "method", "name": "show", "fqn": "App\\Http::show", "owner": "App\\Http",
                 "file": "app/Http.php", "line": 9, "origin": "lsp"},
                {"kind": "class", "name": "Http", "fqn": "App\\Http", "file": "app/Http.php", "line": 7, "origin": "lsp"},
            ],
            "references": [{"scope": "App\\Http::show", "kind": "method_call", "to_fqn": "App\\Cart",
                            "to_member": "total", "file": "app/Http.php", "line": 11, "origin": "lsp"}],
        }
        out = InspectionService(graph=graph).index("/unused", fresh.removeprefix("sym_"), exported=exported)
        assert out["indexed"] and out["links"] == 1
        node = store.structured_query("MATCH (s:Symbol {id: $id}) RETURN s.language AS language, s.origin AS origin",
                                      param_map={"id": node_key("method", "App\\Cart::total", "total")})
        assert node == [{"language": "php", "origin": "lsp"}]
        refs = InspectionService(graph=graph).find_references(out["collection"], "App\\Cart::total", name="total")
        assert [(r["fqn"], r["ref"]["origin"], r["language"]) for r in refs] == [("App\\Http::show", "lsp", "php")]
        assert not store.structured_query("MATCH (s:PhpSymbol) WHERE s.collection IN [$a, $b] RETURN s",
                                          param_map={"a": legacy, "b": out["collection"]})
    finally:
        store.structured_query("MATCH (s) WHERE s.collection STARTS WITH 'sym_legacy_' + $x OR "
                               "s.collection STARTS WITH 'sym_lsp_' + $x DETACH DELETE s", param_map={"x": suffix})


def test_runtime_edges_join_lookups_and_traces_only_for_their_revision():
    """Phase 3b: RUNTIME_CALLS edges of this revision show up; edges of another revision are stale."""
    from inspection.graph import InspectionGraph, node_key
    from inspection.service import InspectionService

    suffix = uuid4().hex
    collection = f"sym_rt_{suffix}"
    rev, other = "rev_" + "1" * 32, "rev_" + "2" * 32
    graph = InspectionGraph()
    store = graph._get_store()
    assert store is not None, "Neo4j graph store is unavailable"
    names = {n: f"App\\Rt{suffix}::{n}" for n in ("target", "static", "magic", "stale")}
    try:
        for name, fqn in names.items():
            store.structured_query(
                "CREATE (:Symbol {id: $id, collection: $c, kind: 'method', name: $name, fqn: $fqn, file: 'app/R.php',"
                " line: 3, revision_id: $rev, symbol_id: $sid, runtime_window_hours: 24,"
                " runtime_count_rolling: CASE WHEN $name = 'stale' THEN 0 ELSE 4 END})",
                param_map={"id": node_key("method", fqn, name), "c": collection, "name": name, "fqn": fqn,
                           "rev": rev, "sid": "sym:" + fqn})
        ids = {name: node_key("method", fqn, name) for name, fqn in names.items()}
        store.structured_query(
            "MATCH (a:Symbol {id: $a}), (t:Symbol {id: $t}) "
            "CREATE (a)-[:REFERENCES {kind: 'method_call', file: 'app/R.php', line: 5, origin: 'lsp'}]->(t)",
            param_map={"a": ids["static"], "t": ids["target"]})
        for caller, revision in (("static", rev), ("magic", rev), ("stale", other)):
            store.structured_query(
                "MATCH (a:Symbol {id: $a}), (t:Symbol {id: $t}) "
                "CREATE (a)-[:RUNTIME_CALLS {calls: 7, last_seen: '2026-11-15', sources: ['hook'], revision_id: $r}]->(t)",
                param_map={"a": ids[caller], "t": ids["target"], "r": revision})
        service = InspectionService(graph=graph)
        h = service.call_hierarchy(collection, names["target"], name="target")
        assert [c["fqn"] for c in h["callers"]] == [names["static"]]
        assert sorted(c["fqn"] for c in h["runtime_callers"]) == sorted([names["static"], names["magic"]])
        assert h["runtime_stale_edges"] == 1 and h["runtime_observed"] is True
        coverage = service.runtime_coverage(collection)
        assert coverage["methods_total"] == 4 and coverage["methods_seen"] == 3 and coverage["percent"] == 75.0
        edges = graph.call_edges(collection)
        assert sorted((e["caller"].rsplit("::", 1)[-1], e["origin"]) for e in edges) == [
            ("magic", "runtime"), ("static", "lsp"), ("static", "runtime")]
    finally:
        store.structured_query("MATCH (s:Symbol {collection: $c}) DETACH DELETE s", param_map={"c": collection})
