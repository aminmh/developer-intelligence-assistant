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
