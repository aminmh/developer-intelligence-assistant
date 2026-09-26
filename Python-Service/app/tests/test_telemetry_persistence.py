from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest
from qdrant_client import QdrantClient

from index.llamaindex_layer import CodePropertyGraph, _chunk_node_id, node_id_for
from inspection.service import InspectionService
from telemetry.snapshot import Snapshot, storage_id
from telemetry.registry import SnapshotRegistry


class GraphStore:
    def __init__(self):
        self.nodes = {}
        self.edges = []

    def structured_query(self, query, param_map):
        rows = param_map["data"]
        for row in rows:
            if "properties" in row:
                self.nodes[row["id"]] = row["properties"]
            else:
                assert row["source_id"] in self.nodes
                assert row["target_id"] in self.nodes
                self.edges.append(row)
        assert "RUNTIME_CALLS" not in query
        return [{"count": len(rows)}]


def chunks():
    return [
        {"type": "method", "class_fqn": "App\\Orders", "name": "store", "file": "app/Orders.php",
         "start_line": 3, "code": "function store() { $this->save(); }", "calls": ["App\\Orders::save"]},
        {"type": "method", "class_fqn": "App\\Orders", "name": "save", "file": "app/Orders.php",
         "start_line": 4, "code": "function save() {}"},
    ]


def index_with_local_vectors(monkeypatch):
    graph = CodePropertyGraph()
    store = GraphStore()
    vectors = QdrantClient(":memory:")
    monkeypatch.setattr(graph, "_get_graph_store", lambda: store)
    monkeypatch.setattr(graph, "_get_qdrant", lambda: vectors)
    monkeypatch.setattr(graph, "_get_embed_model", lambda: type("Embedding", (), {
        "get_text_embedding": lambda self, text: [0.2, 0.4, 0.6]})())
    return graph, store, vectors


def test_real_ingest_isolates_two_projects_and_two_commits(monkeypatch):
    graph, store, vectors = index_with_local_vectors(monkeypatch)
    for project in ("one", "two"):
        for commit in ("a" * 40, "b" * 40):
            snapshot = Snapshot(project, commit, "c" * 64)
            for _ in range(2):
                result = graph.ingest(chunks(), snapshot.collection, snapshot=snapshot)
                assert result["ingested"] == result["graph_nodes"] == 2
            records, _ = vectors.scroll(snapshot.collection, with_payload=True)
            assert len(records) == 2
            for record in records:
                assert record.payload["project_id"] == project
                assert record.payload["commit_sha"] == commit
                assert record.payload["dirty"] is False
                assert record.payload["source_archive_sha256"] == "c" * 64
    assert len(store.nodes) == 8
    for edge in store.edges:
        source, target = store.nodes[edge["source_id"]], store.nodes[edge["target_id"]]
        assert (source["project_id"], source["commit_sha"]) == (target["project_id"], target["commit_sha"])
        assert source["symbol_id"] == "App\\Orders::store"
        assert target["symbol_id"] == "App\\Orders::save"
    assert len({edge["source_id"] for edge in store.edges}) == 4


def test_snapshot_requires_graph_and_embeddings(monkeypatch):
    graph, store, vectors = index_with_local_vectors(monkeypatch)
    snapshot = Snapshot("one", "a" * 40, "b" * 64)
    monkeypatch.setattr(graph, "_get_graph_store", lambda: None)
    with pytest.raises(RuntimeError, match="unavailable"):
        graph.ingest(chunks(), snapshot.collection, snapshot=snapshot)
    monkeypatch.setattr(graph, "_get_graph_store", lambda: store)
    monkeypatch.setattr(graph, "_ingest_vectors", lambda *args, **kwargs: (1, 1))
    with pytest.raises(RuntimeError, match="incomplete"):
        graph.ingest(chunks(), snapshot.collection, snapshot=snapshot)


def test_snapshot_requires_successful_relation_writes(monkeypatch):
    graph, store, vectors = index_with_local_vectors(monkeypatch)
    original = store.structured_query
    def fail_edges(query, param_map):
        if "r:CALLS" in query:
            raise RuntimeError("relation failure")
        return original(query, param_map)
    monkeypatch.setattr(store, "structured_query", fail_edges)
    snapshot = Snapshot("one", "a" * 40, "b" * 64)
    with pytest.raises(RuntimeError, match="relation failure"):
        graph.ingest(chunks(), snapshot.collection, snapshot=snapshot)
    assert vectors.get_collections().collections == []


def test_legacy_ids_stay_stable_and_new_closures_are_distinct():
    assert _chunk_node_id(chunks()[0]) == hashlib.sha1(b"App\\Orders::store").hexdigest()
    path = Path(__file__).parents[2] / "php/tests/telemetry/export_fixture.php"
    exported = json.loads(subprocess.check_output(["php", str(path)]))["chunks"]
    closures = [c for c in exported if c["type"] in ("closure", "arrow_function")]
    assert len(closures) >= 2
    assert len({_chunk_node_id(c) for c in closures}) == len(closures)


def test_inspection_snapshot_nodes_and_references_are_scoped():
    class InspectionStore:
        def __init__(self):
            self.nodes, self.refs = [], []
        def upsert_nodes(self, rows):
            self.nodes.extend(rows)
            return len(rows)
        def upsert_declaration_relations(self, rows):
            return len(rows)
        def upsert_references(self, rows):
            self.refs.extend(rows)
            return len(rows)
    store = InspectionStore()
    service = InspectionService(store)
    defs = [{"kind": "method", "fqn": "App\\Orders::" + method, "name": method,
             "owner": "App\\Orders", "file": "app/Orders.php"} for method in ("store", "save")]
    refs = [{"kind": "method_call", "scope": "App\\Orders::store", "to_fqn": "App\\Orders", "to_member": "save"}]
    for p in ("one", "two"):
        for c in ("a" * 40, "b" * 40):
            snapshot = Snapshot(p, c, "c" * 64)
            result = service.index("unused", "unused", exported={"definitions": defs, "references": refs}, snapshot=snapshot)
            assert result["indexed"]
            assert store.refs[-1]["source_id"] == storage_id(p, c, "App\\Orders::store")
            assert store.refs[-1]["target_id"] == storage_id(p, c, "App\\Orders::save")
    assert len({node["id"] for node in store.nodes}) == 8


def test_reserved_collections_reject_legacy_writes():
    with pytest.raises(ValueError, match="explicit snapshot"):
        CodePropertyGraph().ingest([], "snapshot_reserved")
    with pytest.raises(ValueError, match="explicit snapshot"):
        InspectionService().index("unused", "snapshot_reserved", exported={"definitions": [], "references": []})


def test_inspection_reads_use_stored_ids_and_require_ready(tmp_path, monkeypatch):
    from inspection.queries import SymbolGraphQuery
    from telemetry import registry as registry_module
    registry = SnapshotRegistry(tmp_path)
    snapshot = Snapshot("one", "a" * 40, "b" * 64)
    registry.register(snapshot)
    monkeypatch.setattr(registry_module, "default_registry", lambda: registry)
    calls = []
    node = {"id": storage_id("one", "a" * 40, "App\\Orders::store"),
            "symbol_id": "App\\Orders::store", "kind": "method", "fqn": "App\\Orders::store", "name": "store",
            **snapshot.metadata()}
    class Store:
        def structured_query(self, query, param_map):
            calls.append(param_map)
            return [{"n": node}] if "fqn" in param_map else []
    query = SymbolGraphQuery(type("Graph", (), {"_get_store": lambda self: Store()})())
    collection = "sym_" + snapshot.collection
    assert query.resolve_by_fqn(collection, node["fqn"]) == []
    assert calls == []
    registry.transition(snapshot, "ready")
    query.find_references(collection, node["fqn"])
    query.find_implementations(collection, node["fqn"])
    query.call_hierarchy(collection, node["fqn"])
    assert len(calls) == 7
    for params in calls:
        if "target_id" in params or "source_id" in params:
            assert params.get("target_id", params.get("source_id")) == node["id"]


def test_real_source_parser_to_snapshot_persistence(tmp_path, monkeypatch):
    from telemetry.source import SourceService
    from infra.settings import settings
    from inspection.graph import InspectionGraph
    from index import llamaindex_layer
    if not (Path(settings.php_cli_root) / "bin" / "code-parse").is_file():
        pytest.skip("PHP CLI parser binary is unavailable in this environment")
    graph, store, vectors = index_with_local_vectors(monkeypatch)
    monkeypatch.setattr(llamaindex_layer, "CodePropertyGraph", lambda: graph)
    monkeypatch.setattr(InspectionGraph, "_get_store", lambda self: store)
    from test_telemetry_source import archive, Storage, deploy
    registry = SnapshotRegistry(tmp_path)
    service = SourceService(registry, Storage())
    code = '<?php namespace App; class Orders { function store() { $this->save(); } function save() {} }'
    service.upload("parser", "a" * 40, archive(code))
    deploy(registry, "parser", "a" * 40)
    assert service.process_one()
    state = registry.get("parser", "a" * 40)
    assert state["state"] == "ready"
    records, _ = vectors.scroll(state["collection_name"], with_payload=True)
    assert {r.payload.get("symbol_id") for r in records if r.payload.get("symbol_id")} == {
        "App\\Orders::store", "App\\Orders::save"}


def test_deferred_worker_publishes_ready_after_complete_snapshot_index(tmp_path):
    from telemetry.source import SourceService
    from test_telemetry_source import archive, Storage, deploy
    registry = SnapshotRegistry(tmp_path)
    def complete_index(workspace, snapshot):
        assert Path(workspace, "src/example.php").is_file()
        assert snapshot.project_id == "parser"
        return {"chunk_count": 1, "ingested": 1, "graph_nodes": 1, "embed_fails": 0}
    service = SourceService(registry, Storage(), complete_index)
    service.upload("parser", "a" * 40, archive("<?php function example() {}"))
    deploy(registry, "parser", "a" * 40)
    assert service.process_one()
    state = registry.get("parser", "a" * 40)
    assert state["state"] == "ready"
    assert state["collection_name"].startswith("snapshot_")


def test_partial_php_parse_cannot_be_ready(tmp_path, monkeypatch):
    from infra.php_cli import PhpCli, PhpCommandResult
    from telemetry.source import SourceService
    from test_telemetry_source import archive, Storage, deploy
    monkeypatch.setattr(PhpCli, "run", lambda *a, **kw: PhpCommandResult((), 2, "[]", "partial"))
    registry = SnapshotRegistry(tmp_path)
    service = SourceService(registry, Storage())
    service.upload("partial", "a" * 40, archive())
    deploy(registry, "partial", "a" * 40)
    assert service.process_one()
    state = registry.get("partial", "a" * 40)
    assert state["state"] == "failed"
    assert "collection_name" not in state
