"""Phase 3 tests — domain-concept Qdrant semantic indexer (hermetic).

The indexer talks to the outside world through two narrow seams that tests
replace with fakes:
  * ``projects.domain.indexer._qdrant`` — a fake Qdrant client recording every
    collection/upsert/delete/scroll call,
  * ``projects.domain.indexer.embed_texts`` — a deterministic vector builder
    derived from the input text (fixed width, content-sensitive).
The shared concept store is pointed at a per-test tmp SQLite file through
``projects.domain.indexer._store``.
"""

from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace
from typing import Any

from qdrant_client.models import PointStruct

from projects.domain import indexer as indexer_module
from projects.domain.indexer import (
    CONCEPT_COLLECTION,
    content_hash,
    embed_input,
    ensure_collection,
    index_project_concepts,
    purge_project_vectors,
    reconcile_vectors,
)
from projects.domain.normalizer import ConceptNormalizer
from projects.domain.store import ConceptStore
from projects.domain.types import DomainConcept

NORMALIZE = ConceptNormalizer().normalize
DIM = 6


# --- fakes -------------------------------------------------------------------


def fake_embed_texts(texts: list[str]) -> list[list[float]]:
    """Deterministic fixed-width vectors whose values track the input text."""
    vectors: list[list[float]] = []
    for text in texts:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vectors.append(
            [float(digest[i % len(digest)]) / 255.0 for i in range(DIM)]
        )
    return vectors


def flaky_embed_texts(trigger: str = "Beta"):
    """Embed that raises for any text mentioning ``trigger``."""

    def embed(texts: list[str]) -> list[list[float]]:
        for text in texts:
            if trigger in text:
                raise RuntimeError("embed server unavailable")
        return fake_embed_texts(texts)

    return embed


class FakeQdrant:
    """Minimal in-memory Qdrant: records every call, honours project filters."""

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim
        self.collections: set[str] = set()
        self.points: dict[str, tuple[list[float], dict]] = {}
        self.upsert_points = 0
        self.delete_calls: list[list[Any]] = []
        self.payload_indexes: list[tuple[str, str]] = []
        self.create_collections: list[str] = []

    # -- client surface used by the indexer ----------------------------------

    def get_collections(self):
        return SimpleNamespace(
            collections=[
                SimpleNamespace(name=name) for name in sorted(self.collections)
            ]
        )

    def get_collection(self, collection_name: str):
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=SimpleNamespace(size=self.dim)
                )
            ),
            points_count=len(self.points),
        )

    def create_collection(self, collection_name, vectors_config, wait=True):
        self.collections.add(collection_name)
        self.create_collections.append(collection_name)
        return True

    def create_payload_index(
        self, collection_name, field_name, field_schema, wait=True
    ):
        self.payload_indexes.append((collection_name, field_name))
        return True

    def upsert(self, collection_name, wait=True, points=None):
        for point in points or []:
            self.points[str(point.id)] = (list(point.vector), dict(point.payload))
        self.upsert_points += len(points or [])
        return True

    def scroll(
        self,
        collection_name,
        scroll_filter=None,
        limit=10,
        offset=None,
        with_payload=True,
        with_vectors=False,
    ):
        matching = [
            (point_id, payload)
            for point_id, (_vector, payload) in self.points.items()
            if self._matches(payload, scroll_filter)
        ]
        records = [
            SimpleNamespace(id=point_id, payload=payload)
            for point_id, payload in matching
        ]
        return records, None

    def delete(self, collection_name, points_selector=None, wait=True):
        point_ids = list(points_selector or [])
        self.delete_calls.append(point_ids)
        for point_id in point_ids:
            self.points.pop(str(point_id), None)
        return True

    # -- fake helpers ---------------------------------------------------------

    @staticmethod
    def _matches(payload: dict, filt) -> bool:
        if filt is None:
            return True
        for condition in getattr(filt, "must", None) or []:
            if condition.key == "project_id":
                expected = condition.match.value
                if str(payload.get("project_id")) != str(expected):
                    return False
        return True

    def identity_keys_for(self, project_id: str) -> set[str]:
        return {
            str(payload.get("identity_key") or "")
            for _vector, payload in self.points.values()
            if payload.get("project_id") == project_id
        }


def seed_point(fake: FakeQdrant, project_id: str, identity_key: str) -> str:
    point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{project_id}:{identity_key}"))
    fake.upsert(
        collection_name=CONCEPT_COLLECTION,
        points=[
            PointStruct(
                id=point_id,
                vector=[0.1] * DIM,
                payload={"project_id": project_id, "identity_key": identity_key},
            )
        ],
    )
    return point_id


# --- fixture helpers ----------------------------------------------------------


def _concept(
    identity: str,
    concept_type: str = "class",
    roles: list[str] | None = None,
    description: str | None = None,
    canonical: str | None = None,
    qualified: str | None = None,
) -> DomainConcept:
    name = canonical or identity.rsplit("\\", 1)[-1]
    return DomainConcept(
        identity_key=identity,
        canonical_name=name,
        qualified_name=qualified or identity,
        concept_type=concept_type,
        roles=list(roles or [concept_type]),
        normalized_name=NORMALIZE(name),
        description=description,
        aliases=[],
        sources=[],
        metadata={},
    )


def _tmp_store(tmp_path) -> ConceptStore:
    return ConceptStore(db_path=str(tmp_path / "domain_concepts.db"))


def _store_backed_rows(store: ConceptStore, project_id: str) -> list[dict]:
    return store.list_concepts(project_id)


def _enrich(row: dict, aliases: list[str] | None = None, description: str | None = None) -> dict:
    enriched = dict(row)
    if aliases is not None:
        enriched["aliases"] = list(aliases)
    if description is not None:
        enriched["description"] = description
    return enriched


def _patch_env(
    monkeypatch,
    tmp_path,
    fake: FakeQdrant,
    embed=fake_embed_texts,
    store: ConceptStore | None = None,
) -> ConceptStore:
    concept_store = store or _tmp_store(tmp_path)
    monkeypatch.setattr(indexer_module.settings, "embedding_provider", "ollama")
    monkeypatch.setattr(indexer_module, "_store", concept_store)
    monkeypatch.setattr(indexer_module, "_qdrant", lambda: fake)
    monkeypatch.setattr(indexer_module, "embed_texts", embed)
    return concept_store


# --- pure content tests -------------------------------------------------------


class TestEmbedInput:
    def test_embed_input_deterministic_only_recorded_fields_role_order_fixed(self):
        row = {
            "id": "abc",
            "project_id": "p1",
            "identity_key": "App\\UserService",
            "concept_type": "class",
            "canonical_name": "UserService",
            "qualified_name": "App\\UserService",
            "normalized_name": "user service",
            "description": "handles users",
            "metadata": {"roles": ["service", "class"], "namespace": "App"},
            "roles": ["service", "class"],
            "aliases": [{"alias": "user-svc"}, {"alias": "users"}, "members"],
            "owner": "unused",
        }
        expected = (
            "Type: class\n"
            "Name: UserService\n"
            "Qualified name: App\\UserService\n"
            "Aliases: members, user-svc, users\n"
            "Description: handles users\n"
            "Roles: class, service"
        )
        assert embed_input(row) == expected

        # Same concept whose roles/aliases arrive in a different discovery
        # order produces the exact same text (deterministic hashing).
        reordered = dict(row)
        reordered["roles"] = ["class", "service"]
        reordered["aliases"] = ["members", "user-svc", "users"]
        assert embed_input(reordered) == expected

    def test_embed_input_falls_back_to_canonical_qualified_and_dash_description(self):
        row = {
            "id": "abc",
            "project_id": "p1",
            "identity_key": "route:get:/login",
            "concept_type": "route",
            "canonical_name": "GET /login",
            "qualified_name": None,
            "normalized_name": "get login",
            "description": None,
            "metadata": {"roles": ["route"]},
            "roles": ["route"],
        }
        text = embed_input(row)
        assert "Qualified name: GET /login" in text
        assert "Description: —" in text


class TestContentHash:
    def test_content_hash_stable_and_sensitive(self):
        first = content_hash("Type: class\nName: User")
        assert first == content_hash("Type: class\nName: User")
        assert first != content_hash("Type: class\nName: Member")


# --- collection lifecycle -----------------------------------------------------


class TestEnsureCollection:
    def test_ensure_collection_creates_with_probe_dimension_and_payload_index(
        self, monkeypatch, tmp_path
    ):
        fake = FakeQdrant(dim=DIM)
        _patch_env(monkeypatch, tmp_path, fake)

        result = ensure_collection()

        assert result["ok"] is True
        assert result["error"] is None
        assert result["payload_index"] is True
        assert CONCEPT_COLLECTION in fake.collections
        assert fake.create_collections == [CONCEPT_COLLECTION]
        assert ("domain_concepts", "project_id") in fake.payload_indexes

        # A second call on an existing same-dimension collection is a no-op.
        second = ensure_collection()
        assert second["ok"] is True
        assert fake.create_collections == [CONCEPT_COLLECTION]

    def test_ensure_collection_reports_dimension_mismatch_honestly(
        self, monkeypatch, tmp_path
    ):
        fake = FakeQdrant(dim=DIM - 1)
        fake.collections.add(CONCEPT_COLLECTION)
        _patch_env(monkeypatch, tmp_path, fake)

        result = ensure_collection()

        assert result["ok"] is False
        assert "dimension mismatch" in result["error"]


# --- indexing idempotency -----------------------------------------------------


class TestIndexProjectConcepts:
    def test_store_backed_flow_indexes_all_and_marks_done(self, monkeypatch, tmp_path):
        fake = FakeQdrant()
        store = _patch_env(monkeypatch, tmp_path, fake)
        store.upsert_concept("p1", _concept("UserService", roles=["class", "service"]))
        store.upsert_concept("p1", _concept("Invoice", roles=["class", "entity"]))
        store.upsert_concept("p1", _concept("route:get:/ping", concept_type="route"))

        rows = _store_backed_rows(store, "p1")
        result = index_project_concepts("p1", rows)

        assert result["ok"] is True
        assert result["indexed"] == 3
        assert result["skipped_unmodified"] == 0
        assert result["failed"] == 0
        assert result["pending"] == 0
        assert fake.upsert_points == 3
        assert fake.identity_keys_for("p1") == {
            "UserService",
            "Invoice",
            "route:get:/ping",
        }
        done = store.index_state_rows("p1", status="done")
        assert sorted(r["identity_key"] for r in done) == [
            "Invoice",
            "UserService",
            "route:get:/ping",
        ]

    def test_second_identical_run_skips_all_without_upsert(
        self, monkeypatch, tmp_path
    ):
        fake = FakeQdrant()
        store = _patch_env(monkeypatch, tmp_path, fake)
        store.upsert_concept("p1", _concept("UserService"))
        store.upsert_concept("p1", _concept("Invoice"))

        first = index_project_concepts("p1", _store_backed_rows(store, "p1"))
        assert first["indexed"] == 2
        assert fake.upsert_points == 2

        second = index_project_concepts("p1", _store_backed_rows(store, "p1"))
        assert second["ok"] is True
        assert second["indexed"] == 0
        assert second["skipped_unmodified"] == 2
        assert fake.upsert_points == 2  # no re-embed / re-upsert happened

    def test_changed_description_and_aliases_reupsert(self, monkeypatch, tmp_path):
        fake = FakeQdrant()
        store = _patch_env(monkeypatch, tmp_path, fake)
        store.upsert_concept("p1", _concept("UserService", description="v1"))

        row = _store_backed_rows(store, "p1")[0]
        first = index_project_concepts(
            "p1", [_enrich(row, aliases=["user-svc", "users"])]
        )
        assert first["indexed"] == 1
        assert fake.upsert_points == 1

        changed = index_project_concepts(
            "p1", [_enrich(row, aliases=["user-svc", "users", "members"])]
        )
        assert changed["indexed"] == 1
        assert changed["skipped_unmodified"] == 0
        assert fake.upsert_points == 2

        unchanged = index_project_concepts(
            "p1", [_enrich(row, aliases=["user-svc", "users", "members"])]
        )
        assert unchanged["skipped_unmodified"] == 1
        assert fake.upsert_points == 2

    def test_per_concept_embed_failure_counts_failed_and_persists_pending(
        self, monkeypatch, tmp_path
    ):
        fake = FakeQdrant()
        store = _patch_env(monkeypatch, tmp_path, fake, embed=flaky_embed_texts())
        store.upsert_concept("p1", _concept("Alpha"))
        store.upsert_concept("p1", _concept("Beta"))
        store.upsert_concept("p1", _concept("Gamma"))

        result = index_project_concepts("p1", _store_backed_rows(store, "p1"))

        assert result["ok"] is True
        assert result["indexed"] == 2
        assert result["failed"] == 1
        assert result["pending"] == 0
        assert fake.upsert_points == 2
        done = {r["identity_key"] for r in store.index_state_rows("p1", status="done")}
        assert done == {"Alpha", "Gamma"}
        pending = store.index_state_rows("p1", status="pending")
        assert [r["identity_key"] for r in pending] == ["Beta"]
        assert pending[0]["last_error"] and "embed" in pending[0]["last_error"]

    def test_qdrant_ensure_down_records_pending_and_returns_ok_false(
        self, monkeypatch, tmp_path
    ):
        store = _tmp_store(tmp_path)
        monkeypatch.setattr(indexer_module, "_store", store)

        def broken_qdrant():
            raise RuntimeError("qdrant connection refused")

        monkeypatch.setattr(indexer_module, "_qdrant", broken_qdrant)
        monkeypatch.setattr(indexer_module, "embed_texts", fake_embed_texts)
        store.upsert_concept("p1", _concept("UserService"))
        store.upsert_concept("p1", _concept("Invoice"))

        result = index_project_concepts("p1", _store_backed_rows(store, "p1"))

        assert result["ok"] is False
        assert result["pending"] == 2
        assert result["indexed"] == 0
        assert "qdrant" in result["error"].lower() or "unavailable" in result["error"]
        pending = store.index_state_rows("p1", status="pending")
        assert sorted(r["identity_key"] for r in pending) == ["Invoice", "UserService"]
        assert all(r["last_error"] for r in pending)

    def test_no_concepts_returns_empty_without_touching_qdrant(
        self, monkeypatch, tmp_path
    ):
        fake = FakeQdrant()
        _patch_env(monkeypatch, tmp_path, fake)

        result = index_project_concepts("p1", [])

        assert result["ok"] is True
        assert result["indexed"] == 0
        assert result["pending"] == 0
        assert fake.upsert_points == 0
        assert fake.collections == set()


# --- reconciliation + purge ---------------------------------------------------


class TestReconcileAndPurge:
    def test_reconcile_deletes_only_stale_identity_keys(self, monkeypatch, tmp_path):
        fake = FakeQdrant()
        _patch_env(monkeypatch, tmp_path, fake)
        seed_point(fake, "p1", "A")
        stale_c = seed_point(fake, "p1", "C")
        stale_d = seed_point(fake, "p1", "D")
        seed_point(fake, "p2", "Z")

        result = reconcile_vectors("p1", {"A", "B"})

        assert result["ok"] is True
        assert result["deleted"] == 2
        assert fake.identity_keys_for("p1") == {"A"}
        assert fake.identity_keys_for("p2") == {"Z"}
        assert {point_id for point_id in fake.delete_calls[0]} == {stale_c, stale_d}

    def test_purge_deletes_all_for_project_only(self, monkeypatch, tmp_path):
        fake = FakeQdrant()
        _patch_env(monkeypatch, tmp_path, fake)
        seed_point(fake, "p1", "A")
        seed_point(fake, "p1", "B")
        seed_point(fake, "p2", "Z")

        result = purge_project_vectors("p1")

        assert result["ok"] is True
        assert result["deleted"] == 2
        assert fake.identity_keys_for("p1") == set()
        assert fake.identity_keys_for("p2") == {"Z"}

    def test_reconcile_qdrant_down_returns_ok_false(self, monkeypatch, tmp_path):
        store = _tmp_store(tmp_path)
        monkeypatch.setattr(indexer_module, "_store", store)

        def broken_qdrant():
            raise RuntimeError("qdrant connection refused")

        monkeypatch.setattr(indexer_module, "_qdrant", broken_qdrant)
        result = reconcile_vectors("p1", {"A"})
        assert result["ok"] is False
        assert result["deleted"] == 0

        purge = purge_project_vectors("p1")
        assert purge["ok"] is False
        assert purge["deleted"] == 0
