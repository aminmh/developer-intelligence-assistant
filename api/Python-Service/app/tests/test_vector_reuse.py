"""Phase 2 of revision sync: re-embed only chunks whose embeddable text changed."""

import pytest

import index.llamaindex_layer as layer


class _Record:
    def __init__(self, payload, vector):
        self.payload, self.vector = payload, vector


class _Qdrant:
    """Just enough of QdrantClient for _ingest_vectors and its reuse lookup."""

    def __init__(self):
        self.points: dict[str, list] = {}
        self.indexes: list[tuple[str, str]] = []

    def delete_collection(self, name):
        self.points.pop(name, None)

    def create_collection(self, collection_name, vectors_config):
        self.points.setdefault(collection_name, [])

    def create_payload_index(self, collection_name, field_name, field_schema):
        self.indexes.append((collection_name, field_name))

    def upsert(self, collection_name, wait, points):
        self.points.setdefault(collection_name, []).extend(points)

    def scroll(self, collection_name, scroll_filter, limit, with_payload, with_vectors):
        conditions = {c.key: c.match for c in scroll_filter.must}
        profile, hashes = conditions["embed_profile"].value, set(conditions["embed_hash"].any)
        hits = [_Record(p.payload, p.vector) for p in self.points.get(collection_name, [])
                if p.payload.get("embed_profile") == profile and p.payload.get("embed_hash") in hashes]
        return hits[:limit], None


def _chunk(name, code):
    return {"type": "function", "name": name, "class_fqn": f"app\\{name}", "namespace": "app", "file": "a.py",
            "start_line": 1, "end_line": 2, "code": code}


@pytest.fixture
def graph(monkeypatch):
    qdrant = _Qdrant()
    embedded: list[str] = []

    def fake_embed(texts, input_type, progress=None):
        embedded.extend(texts)
        return [[float(len(text)), 1.0, 0.5] for text in texts]

    monkeypatch.setattr(layer, "embed_texts", fake_embed)
    monkeypatch.setattr(layer, "embed_profile_key", lambda: "profile-a")
    cpg = layer.CodePropertyGraph.__new__(layer.CodePropertyGraph)
    cpg._get_qdrant = lambda: qdrant
    return cpg, qdrant, embedded


def test_unchanged_chunks_reuse_vectors_and_changed_ones_are_embedded(graph):
    cpg, qdrant, embedded = graph
    cpg._ingest_vectors([_chunk("f", "return 1"), _chunk("g", "return 2")], "rev1", replace=False)
    first = list(embedded)
    embedded.clear()

    ingested, fails = cpg._ingest_vectors([_chunk("f", "return 1"), _chunk("g", "return 22")], "rev2",
                                          replace=False, reuse_from=["rev1"])

    assert (ingested, fails) == (2, 0) and len(first) == 2
    assert len(embedded) == 1 and "return 22" in embedded[0]
    assert cpg._last_vector_stats == {"embedded": 1, "reused": 1}
    reused = next(p for p in qdrant.points["rev2"] if p.payload["name"] == "f")
    original = next(p for p in qdrant.points["rev1"] if p.payload["name"] == "f")
    assert reused.vector == original.vector and reused.payload["embed_hash"] == original.payload["embed_hash"]
    assert all(p.payload["embed_profile"] == "profile-a" for p in qdrant.points["rev2"])
    assert ("rev2", "embed_hash") in qdrant.indexes and ("rev2", "embed_profile") in qdrant.indexes


def test_a_different_embedding_profile_is_never_reused(graph, monkeypatch):
    cpg, qdrant, embedded = graph
    cpg._ingest_vectors([_chunk("f", "return 1")], "rev1", replace=False)
    embedded.clear()
    monkeypatch.setattr(layer, "embed_profile_key", lambda: "profile-b")

    cpg._ingest_vectors([_chunk("f", "return 1")], "rev2", replace=False, reuse_from=["rev1"])

    assert len(embedded) == 1 and cpg._last_vector_stats == {"embedded": 1, "reused": 0}


def test_no_reuse_without_sources_and_never_from_the_target_itself(graph):
    cpg, qdrant, embedded = graph
    cpg._ingest_vectors([_chunk("f", "return 1")], "rev1", replace=False)
    embedded.clear()

    cpg._ingest_vectors([_chunk("f", "return 1")], "rev1", replace=False, reuse_from=["rev1"])
    assert len(embedded) == 1
    cpg._ingest_vectors([_chunk("f", "return 1")], "rev3", replace=False)
    assert len(embedded) == 2 and cpg._last_vector_stats == {"embedded": 1, "reused": 0}


def test_a_failed_lookup_falls_back_to_embedding(graph):
    cpg, qdrant, embedded = graph

    def broken(**kwargs):
        raise RuntimeError("collection not found")

    qdrant.scroll = broken
    cpg._ingest_vectors([_chunk("f", "return 1")], "rev2", replace=False, reuse_from=["gone"])
    assert len(embedded) == 1 and cpg._last_vector_stats == {"embedded": 1, "reused": 0}


def test_mixed_vector_sizes_are_never_reused():
    class Mixed:
        def scroll(self, **kwargs):
            return [_Record({"embed_hash": "a"}, [1.0, 2.0]), _Record({"embed_hash": "b"}, [1.0, 2.0, 3.0])], None

    assert layer.CodePropertyGraph._reusable_vectors(Mixed(), ["old"], "p", {"a", "b"}, "new") == {}


def test_profile_key_changes_with_the_embeddable_text_format(monkeypatch):
    monkeypatch.setattr("index.embeddings.embedding_profile",
                        lambda role: {"provider": "cloudflareWorkersAI", "model": "@cf/baai/bge-m3", "dimensions": 1024})
    before = layer.embed_profile_key()
    monkeypatch.setattr(layer, "EMBEDDABLE_TEXT_FORMAT", "2")
    assert layer.embed_profile_key() != before and len(before) == 32


def test_reuse_sources_are_the_projects_newest_ready_revisions(monkeypatch):
    revisions = [
        {"revision_id": "rev_new", "state": "ready", "collection_name": "snapshot_new"},
        {"revision_id": "rev_self", "state": "ready", "collection_name": "snapshot_self"},
        {"revision_id": "rev_queued", "state": "queued", "collection_name": None},
        {"revision_id": "rev_b", "state": "ready", "collection_name": "snapshot_b"},
        {"revision_id": "rev_c", "state": "ready", "collection_name": "snapshot_c"},
        {"revision_id": "rev_d", "state": "ready", "collection_name": "snapshot_d"},
    ]

    class Registry:
        def project_revisions(self, project_id):
            assert project_id == "shop"
            return revisions

    monkeypatch.setattr("telemetry.registry.default_registry", lambda: Registry())
    monkeypatch.setattr("index.embedding_rebuild.active_vector_collection",
                        lambda collection: None if collection == "snapshot_b" else collection)

    class Snap:
        project_id, revision_id = "shop", "rev_self"

    cpg = layer.CodePropertyGraph.__new__(layer.CodePropertyGraph)
    # rev_b has no active Cohere generation, so it is skipped rather than read from a retained one.
    assert cpg._reuse_sources(Snap()) == ["snapshot_new", "snapshot_c", "snapshot_d"]
