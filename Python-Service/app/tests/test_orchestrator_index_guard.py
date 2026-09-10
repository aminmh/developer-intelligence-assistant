from pipelines.orchestrator import Orchestrator


def test_skip_index_probe_requires_existing_collection(monkeypatch):
    monkeypatch.setattr(
        "index.llamaindex_layer.qdrant_collection_has_points",
        lambda name: name == "chunk_code_other_project",
    )

    assert Orchestrator._qdrant_collection_exists("chunk_code_armaiti_api_develop") is False
    assert Orchestrator._qdrant_collection_exists("chunk_code_other_project") is True


def test_skip_index_probe_retries_when_qdrant_unavailable(monkeypatch):
    def unavailable(_name):
        raise RuntimeError("qdrant unavailable")

    monkeypatch.setattr("index.llamaindex_layer.qdrant_collection_has_points", unavailable)

    assert Orchestrator._qdrant_collection_exists("chunk_code_armaiti_api_develop") is False
