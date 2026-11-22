"""Indexing keeps memory bounded: vectors stream to Qdrant and Neo4j lookups use an id index."""
from __future__ import annotations

import index.llamaindex_layer as lli
from index.llamaindex_layer import CodePropertyGraph


class _Qdrant:
    def __init__(self, events):
        self.events = events
        self.upserted = []

    def delete_collection(self, _name):
        pass

    def create_collection(self, **_kwargs):
        pass

    def create_payload_index(self, **_kwargs):
        pass

    def upsert(self, collection_name, wait, points):
        self.events.append(("upsert", len(points)))
        self.upserted.extend(points)


def _chunks(count):
    return [{"type": "function", "name": f"f{i}", "file": "src/a.php", "code": f"function f{i}() {{}}",
             "start_line": i + 1, "end_line": i + 1} for i in range(count)]


def test_vectors_are_upserted_while_embedding_continues(monkeypatch):
    events = []
    qdrant = _Qdrant(events)

    def embed(texts, **_kwargs):
        events.append(("embed", len(texts)))
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(lli, "embed_texts", embed)
    monkeypatch.setattr(lli, "ensure_payload_indexes", lambda *_args: None)
    service = CodePropertyGraph()
    service._get_qdrant = lambda: qdrant

    chunks = _chunks(300)  # four embedding batches of up to 96
    assert service._ingest_vectors(chunks, "project") == (300, 0)

    assert len(qdrant.upserted) == 300
    first_upsert = next(i for i, event in enumerate(events) if event[0] == "upsert")
    last_embed = max(i for i, event in enumerate(events) if event[0] == "embed")
    assert first_upsert < last_embed
    assert all(size <= lli._QDRANT_UPSERT_MAX_POINTS for kind, size in events if kind == "upsert")


def test_failed_upsert_stops_ingest_and_reports_the_shortfall(monkeypatch):
    events = []
    qdrant = _Qdrant(events)

    def failing_upsert(collection_name, wait, points):
        raise RuntimeError("qdrant down")

    qdrant.upsert = failing_upsert
    monkeypatch.setattr(lli, "embed_texts", lambda texts, **_kwargs: [[0.1] for _ in texts])
    monkeypatch.setattr(lli, "ensure_payload_indexes", lambda *_args: None)
    service = CodePropertyGraph()
    service._get_qdrant = lambda: qdrant

    ingested, fails = service._ingest_vectors(_chunks(200), "project")
    assert ingested == 0 and fails == 0


def test_graph_store_creates_code_symbol_id_index(monkeypatch):
    queries = []

    class Store:
        def __init__(self, **_kwargs):
            pass

        def structured_query(self, query, param_map=None):
            queries.append(query)
            return []

    import llama_index.graph_stores.neo4j as neo4j_module
    monkeypatch.setattr(neo4j_module, "Neo4jPropertyGraphStore", Store)

    service = CodePropertyGraph()
    assert service._get_graph_store() is service._get_graph_store()
    assert queries == [lli._GRAPH_INDEX_CYPHER]
    assert "FOR (n:CodeSymbol) ON (n.id)" in queries[0]


def test_index_creation_failure_keeps_the_graph_store_usable(monkeypatch):
    class Store:
        def __init__(self, **_kwargs):
            pass

        def structured_query(self, query, param_map=None):
            raise RuntimeError("no schema privilege")

    import llama_index.graph_stores.neo4j as neo4j_module
    monkeypatch.setattr(neo4j_module, "Neo4jPropertyGraphStore", Store)

    assert isinstance(CodePropertyGraph()._get_graph_store(), Store)


def test_symbol_graph_store_creates_symbol_id_index(monkeypatch):
    import inspection.graph as inspection_graph

    queries = []

    class Store:
        def __init__(self, **_kwargs):
            pass

        def structured_query(self, query, param_map=None):
            queries.append(query)
            return [{"count": 0}]

    import llama_index.graph_stores.neo4j as neo4j_module
    monkeypatch.setattr(neo4j_module, "Neo4jPropertyGraphStore", Store)
    monkeypatch.setattr(inspection_graph.InspectionGraph, "_legacy_checked", True)

    graph = inspection_graph.InspectionGraph()
    assert isinstance(graph._get_store(), Store)
    assert queries == [inspection_graph._SYMBOL_INDEX_CYPHER]
    assert "FOR (n:Symbol) ON (n.id)" in queries[0]
