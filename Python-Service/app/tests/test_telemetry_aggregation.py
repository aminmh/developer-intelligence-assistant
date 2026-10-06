from __future__ import annotations

from telemetry import aggregation


class FakeClickHouse:
    database = "runtime"
    table = "spans"

    def rows(self, _query, _project_id, _revision_id):
        return [{
            "TraceId": "trace-1",
            "SpanId": "span-1",
            "Timestamp": "2026-10-06T00:00:00Z",
            "exception_type": "RuntimeError",
            "normalized_message": "request failed",
            "top_frames": [],
            "symbol_id": "",
        }]


class FakeQdrant:
    def __init__(self):
        self.created = []
        self.upserted = []

    def collection_exists(self, _collection):
        return False

    def create_collection(self, *, collection_name, vectors_config):
        self.created.append(collection_name)

    def upsert(self, *, collection_name, points, wait):
        self.upserted.append((collection_name, points, wait))


def test_cohere_runtime_exception_vectors_use_separate_collection(monkeypatch):
    from infra import settings as settings_module

    qdrant = FakeQdrant()
    monkeypatch.setattr(settings_module.settings, "embedding_provider", "cohere", raising=False)
    monkeypatch.setattr("qdrant_client.QdrantClient", lambda **_kwargs: qdrant)
    monkeypatch.setattr("index.embeddings.embed_texts", lambda texts: [[0.1, 0.2] for _ in texts])

    count = aggregation._store_exception_vectors(
        FakeClickHouse(), "project", "revision", "a" * 40, 24
    )

    assert count == 1
    assert qdrant.created == ["runtime_exceptions_cohere_v5"]
    assert qdrant.upserted[0][0] == "runtime_exceptions_cohere_v5"
