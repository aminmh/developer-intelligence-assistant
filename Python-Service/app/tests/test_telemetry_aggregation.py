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


class _AggregationRegistry:
    def runtime_projects(self):
        return [{"project_id": "pizza", "commit_sha": "c" * 40, "revision_id": "rev_" + "a" * 32, "state": "ready"}]

    def lock(self, _project_id, _commit_sha):
        from contextlib import nullcontext
        return nullcontext()

    def get_by_revision_id(self, _project_id, _revision_id):
        return {"state": "ready"}


class _AggregationClickHouse:
    configured = True
    signature = (10, "2026-11-06 12:00:00.000000000")

    def __init__(self):
        self.summaries = 0
        self.stored = []

    def span_signature(self, _project_id, _revision_id):
        return self.signature

    def summary(self, _project_id, _revision_id, _known):
        self.summaries += 1
        return {"available": True, "symbols": [{"symbol_id": "App\\Models\\Pizza::size", "runs": 3}], "aggregation_window_hours": 24}

    def store_unresolved_symbols(self, rows):
        self.stored.extend(rows)


def _patch_aggregation(monkeypatch, clickhouse):
    import inspection.graph

    class Graph:
        def upsert_runtime_facts(self, _collection, _facts):
            pass

    monkeypatch.setattr(aggregation, "default_registry", lambda: _AggregationRegistry())
    monkeypatch.setattr(aggregation, "ClickHouseRuntime", lambda: clickhouse)
    monkeypatch.setattr(aggregation, "snapshot_definitions", lambda _project, _commit: [])
    monkeypatch.setattr(aggregation, "_graph_facts", lambda *_args: {})
    monkeypatch.setattr(aggregation, "_store_exception_vectors", lambda *_args: 0)
    monkeypatch.setattr(inspection.graph, "InspectionGraph", Graph)
    monkeypatch.setattr(aggregation, "_aggregated_signatures", {})


def test_aggregation_skips_a_revision_whose_spans_have_not_changed(monkeypatch):
    clickhouse = _AggregationClickHouse()
    _patch_aggregation(monkeypatch, clickhouse)

    first = aggregation.aggregate_runtime()
    second = aggregation.aggregate_runtime()
    clickhouse.signature = (11, "2026-11-06 12:05:00.000000000")
    third = aggregation.aggregate_runtime()

    assert (first["processed"], first["skipped"]) == (1, 0)
    assert (second["processed"], second["skipped"]) == (0, 1)
    assert (third["processed"], third["skipped"]) == (1, 0)
    assert clickhouse.summaries == 2


def test_aggregation_retries_a_revision_whose_cycle_failed(monkeypatch):
    clickhouse = _AggregationClickHouse()
    _patch_aggregation(monkeypatch, clickhouse)
    calls = []

    def failing_store(rows):
        calls.append(rows)
        if len(calls) == 1:
            raise RuntimeError("ClickHouse insert failed")

    clickhouse.store_unresolved_symbols = failing_store

    aggregation.aggregate_runtime()
    retry = aggregation.aggregate_runtime()

    assert retry["skipped"] == 0 and len(calls) == 2


def test_observed_symbol_rows_use_a_clickhouse_datetime(monkeypatch):
    import re

    clickhouse = _AggregationClickHouse()
    _patch_aggregation(monkeypatch, clickhouse)

    aggregation.aggregate_runtime()

    stamp = clickhouse.stored[0]["snapshot_updated_at"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}", stamp), stamp
