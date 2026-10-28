from __future__ import annotations

import pytest

from index.llamaindex_layer import CodePropertyGraph


def _graph(chunks):
    service = CodePropertyGraph()
    graph_calls = []
    service._ingest_graph = lambda rows, collection, strict=False: (
        graph_calls.append((rows, collection, strict)) or (len(rows), 0)
    )
    service._ingest_vectors = lambda rows, collection, **kwargs: (len(rows), 0)
    service._get_qdrant = lambda: pytest.fail("non-snapshot ingestion must not require vector readback")
    return service, graph_calls


def test_non_snapshot_ingestion_requires_strict_graph_write():
    service, graph_calls = _graph([{"name": "handler", "file": "app.py"}])

    result = service.ingest([{"name": "handler", "file": "app.py"}], "project")

    assert graph_calls[0][2] is True
    assert result["graph_nodes"] == result["chunk_count"] == 1


def test_non_snapshot_ingestion_rejects_partial_graph_write():
    service, _graph_calls = _graph([{"name": "handler", "file": "app.py"}])
    service._ingest_graph = lambda *_args, **_kwargs: (0, 0)

    with pytest.raises(RuntimeError, match="Project graph persistence is incomplete"):
        service.ingest([{"name": "handler", "file": "app.py"}], "project")


def test_non_snapshot_ingestion_rejects_unavailable_graph_store():
    service = CodePropertyGraph()
    service._get_graph_store = lambda: None

    with pytest.raises(RuntimeError, match="Neo4j graph store unavailable"):
        service._ingest_graph([{"name": "handler", "file": "app.py"}], "project", strict=True)


def test_project_without_indexable_chunks_fails_instead_of_reporting_empty_success():
    service = CodePropertyGraph()

    with pytest.raises(ValueError, match="no indexable source chunks"):
        service.ingest([], "project")


def test_non_snapshot_language_symbols_persist_local_call_relationships():
    writes = []

    class GraphStore:
        def structured_query(self, query, param_map):
            rows = param_map["data"]
            writes.append((query, rows))
            return [{"count": len(rows)}]

    service = CodePropertyGraph()
    service._get_graph_store = lambda: GraphStore()
    chunks = [
        {
            "type": "function", "name": "caller", "namespace": "js_src_123",
            "class_fqn": "js_src_123\\caller", "file": "src/caller.ts",
            "start_line": 1, "end_line": 2,
            "calls": ["js_src_123\\target"],
        },
        {
            "type": "function", "name": "target", "namespace": "js_src_123",
            "class_fqn": "js_src_123\\target", "file": "src/caller.ts",
            "start_line": 4, "end_line": 5,
        },
    ]

    assert service._ingest_graph(chunks, "project", strict=True) == (2, 1)
    relationship_rows = writes[1][1]
    assert relationship_rows[0]["label"] == "CALLS"
    assert relationship_rows[0]["source_id"] != relationship_rows[0]["target_id"]
