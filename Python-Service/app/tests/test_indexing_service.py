from __future__ import annotations

from index import indexing_service as module


def test_index_chunks_background_writes_terminal_status(monkeypatch, tmp_path):
    monkeypatch.setattr(module.settings, "download_dir", str(tmp_path))
    monkeypatch.setattr(
        "index.llamaindex_layer.CodePropertyGraph",
        type("Graph", (), {"ingest": lambda self, chunks, collection, **kwargs: {
            "ingested": len(chunks), "graph_nodes": len(chunks), "graph_relations": 0,
        }}),
    )

    service = module.IndexingService()
    service.index_chunks_background("job-1", "project.zip", [{"name": "x"}], "project")

    status = (tmp_path / "job-1.status.json").read_text()
    assert '"status": "done"' in status
    assert '"indexed": 1' in status
    assert '"graph_index_complete": true' in status


def test_graph_failure_never_reports_complete_index(monkeypatch, tmp_path):
    monkeypatch.setattr(module.settings, "download_dir", str(tmp_path))

    class UnavailableGraph:
        def ingest(self, *args, **kwargs):
            raise RuntimeError("Neo4j is unavailable")

    monkeypatch.setattr("index.llamaindex_layer.CodePropertyGraph", UnavailableGraph)
    service = module.IndexingService()
    service.index_chunks_background("job-graph-down", "project.zip", [{"name": "x"}], "project")

    status = (tmp_path / "job-graph-down.status.json").read_text()
    assert '"status": "error"' in status
    assert '"graph_index_complete": false' in status
    assert '"graph_status": "unverified"' in status
    assert "readiness" in status


def test_parse_and_index_cleans_workspace_and_delegates(monkeypatch, tmp_path):
    monkeypatch.setattr(module.settings, "download_dir", str(tmp_path))
    monkeypatch.setattr(module, "extract_zip_safely", lambda data, path: 1)
    monkeypatch.setattr(module, "run_code_parse", lambda path: [{"name": "x"}])

    calls = []
    service = module.IndexingService()
    monkeypatch.setattr(service, "index_chunks_background", lambda *args, **kwargs: calls.append((args, kwargs)))
    service.parse_and_index("job-2", "project.zip", b"zip", "project")

    assert calls == [(
        ("job-2", "project.zip", [{"name": "x"}], "project"),
        {"snapshot": None, "vector_collection_name": None},
    )]
    assert not (tmp_path / "parse_job-2").exists()
