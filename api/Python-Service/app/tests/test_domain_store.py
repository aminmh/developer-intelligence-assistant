"""Phase 1 tests — SQLite domain-concept store (hermetic, tmp files)."""

from __future__ import annotations

import sqlite3

from projects.domain.normalizer import ConceptNormalizer
from projects.domain.store import ConceptStore
from projects.domain.types import (
    DomainAlias,
    DomainConcept,
    DomainRelationship,
    DomainSource,
)

NORMALIZE = ConceptNormalizer().normalize


def _alias(
    alias: str,
    alias_type: str = "generated",
    confidence: float = 0.5,
    provenance: str = "normalizer",
) -> DomainAlias:
    return DomainAlias(
        alias=alias,
        alias_type=alias_type,
        confidence=confidence,
        provenance=provenance,
    )


def _source(
    file: str,
    source_hash: str,
    start_line: int = 1,
    end_line: int = 10,
    method: str = "parser",
) -> DomainSource:
    return DomainSource(
        concept_identity="x",
        file=file,
        source_hash=source_hash,
        start_line=start_line,
        end_line=end_line,
        method=method,
        confidence=1.0,
        metadata={"kind": "file"},
    )


def _concept(
    identity: str,
    canonical: str | None = None,
    concept_type: str = "class",
    roles: list[str] | None = None,
    qualified: str | None = None,
    description: str | None = None,
    aliases: list[DomainAlias] | None = None,
    sources: list[DomainSource] | None = None,
) -> DomainConcept:
    name = canonical or identity.rsplit("\\", 1)[-1]
    return DomainConcept(
        identity_key=identity,
        canonical_name=name,
        qualified_name=qualified,
        concept_type=concept_type,
        roles=list(roles or []),
        normalized_name=NORMALIZE(name),
        description=description,
        aliases=list(aliases or []),
        sources=list(sources or []),
        metadata={},
    )


def _relationship(
    source: str,
    target: str,
    relation_type: str = "depends",
    confidence: float = 1.0,
) -> DomainRelationship:
    return DomainRelationship(
        relation_type=relation_type,
        source_identity=source,
        target_identity=target,
        confidence=confidence,
        sources=[],
        metadata={},
    )


class TestDomainStore:
    def _store(self, tmp_path, name: str = "domain_concepts.db") -> ConceptStore:
        return ConceptStore(db_path=str(tmp_path / name))

    # -- file + PRAGMAs ------------------------------------------------------

    def test_file_created_with_pragmas_and_schema(self, tmp_path):
        db = tmp_path / "domain_concepts.db"
        store = self._store(tmp_path)
        store.upsert_concept("p1", _concept("UserService"))

        conn = sqlite3.connect(str(db))
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert {
                "concepts",
                "concept_aliases",
                "concept_sources",
                "concept_relationships",
                "index_state",
            } <= tables
        finally:
            conn.close()

    # -- concept upsert ------------------------------------------------------

    def test_upsert_preserves_created_at_updates_fields(self, tmp_path):
        store = self._store(tmp_path)
        first = store.upsert_concept(
            "p1",
            _concept(
                "UserService",
                concept_type="class",
                roles=["class", "service"],
                description="v1",
            ),
        )
        assert first["concept_type"] == "class"
        assert first["roles"] == ["class", "service"]

        second = store.upsert_concept(
            "p1",
            _concept(
                "UserService",
                canonical="UserManager",
                concept_type="facade",
                roles=["service"],
                description="v2",
            ),
        )
        assert second["created_at"] == first["created_at"]  # preserved
        assert second["updated_at"] >= first["updated_at"]
        assert second["canonical_name"] == "UserManager"
        assert second["concept_type"] == "facade"
        assert second["description"] == "v2"
        assert second["roles"] == ["service"]

    def test_upsert_unique_per_project_identity(self, tmp_path):
        store = self._store(tmp_path)
        store.upsert_concept("p1", _concept("UserService"))
        store.upsert_concept("p1", _concept("UserService"))
        store.upsert_concept("p2", _concept("UserService"))
        assert len(store.list_concepts("p1")) == 1
        assert len(store.list_concepts("p2")) == 1

    # -- alias + source replace semantics ------------------------------------

    def test_aliases_replace_and_match_by_normalized_alias(self, tmp_path):
        store = self._store(tmp_path)
        store.upsert_concept("p1", _concept("UserService"))
        store.replace_aliases(
            "p1",
            "UserService",
            [_alias("user-svc"), _alias("users", provenance="code")],
        )
        hits = store.alias_matches("p1", "user svc")
        assert [h["identity_key"] for h in hits] == ["UserService"]

        store.replace_aliases("p1", "UserService", [_alias("member")])
        assert store.alias_matches("p1", "user svc") == []
        hits = store.alias_matches("p1", "member")
        assert [h["identity_key"] for h in hits] == ["UserService"]

    def test_alias_matches_attaches_exact_stored_spelling_only(self, tmp_path):
        store = self._store(tmp_path)
        store.upsert_concept("p1", _concept("OrderBook"))
        store.replace_aliases("p1", "OrderBook", [_alias("orders")])
        store.upsert_concept("p1", _concept("UserService"))
        store.replace_aliases("p1", "UserService", [_alias("user-svc")])

        # Case-insensitive exact spelling -> real stored spelling is returned.
        exact = store.alias_matches("p1", "Orders")
        assert [h["identity_key"] for h in exact] == ["OrderBook"]
        assert exact[0]["matched_alias"] == "orders"

        # Merely normalized match carries no matched_alias key at all.
        normalized_only = store.alias_matches("p1", "user svc")
        assert [h["identity_key"] for h in normalized_only] == ["UserService"]
        assert "matched_alias" not in normalized_only[0]

        # Probing with the stored spelling itself surfaces it as matched_alias.
        stored_spelling = store.alias_matches("p1", "user-svc")
        assert [h["identity_key"] for h in stored_spelling] == ["UserService"]
        assert stored_spelling[0]["matched_alias"] == "user-svc"

    def test_sources_replace(self, tmp_path):
        store = self._store(tmp_path)
        store.upsert_concept("p1", _concept("UserService"))
        store.replace_sources(
            "p1",
            "UserService",
            [_source("a.php", "h1"), _source("b.php", "h2")],
        )
        assert len(store.find_sources_for_concept("p1", "UserService")) == 2

        store.replace_sources(
            "p1", "UserService", [_source("c.php", "h3", start_line=5, end_line=9)]
        )
        sources = store.find_sources_for_concept("p1", "UserService")
        assert [s["source_file"] for s in sources] == ["c.php"]
        assert sources[0]["source_hash"] == "h3"
        assert sources[0]["start_line"] == 5

    # -- relationships --------------------------------------------------------

    def test_relationship_requires_endpoints_in_same_project(self, tmp_path):
        store = self._store(tmp_path)
        store.upsert_concept("pa", _concept("svc_a"))
        store.upsert_concept("pb", _concept("svc_b"))

        # target exists only in another project -> boundary failure
        assert store.upsert_relationship(
            "pa", _relationship("svc_a", "svc_b")
        ) is False
        assert store.upsert_relationship("pa", _relationship("svc_a", "ghost")) is False
        assert store.counts("pa")["relationships"] == 0

        store.upsert_concept("pa", _concept("svc_b"))
        assert store.upsert_relationship(
            "pa", _relationship("svc_a", "svc_b")
        ) is True
        assert store.counts("pa")["relationships"] == 1

    def test_relationship_dedupe_idempotent(self, tmp_path):
        store = self._store(tmp_path)
        store.upsert_concept("p1", _concept("a"))
        store.upsert_concept("p1", _concept("b"))
        rel = _relationship("a", "b", relation_type="calls")
        assert store.upsert_relationship("p1", rel) is True
        assert store.upsert_relationship("p1", rel) is True
        assert store.counts("p1")["relationships"] == 1

    def test_remove_concept_cascades(self, tmp_path):
        store = self._store(tmp_path)
        store.upsert_concept("p1", _concept("a"))
        store.upsert_concept("p1", _concept("b"))
        store.upsert_concept("p1", _concept("c"))
        store.replace_aliases("p1", "b", [_alias("b-alias")])
        store.replace_sources("p1", "b", [_source("b.php", "h")])
        store.upsert_relationship("p1", _relationship("a", "b"))
        store.upsert_relationship("p1", _relationship("b", "c"))

        assert store.remove_concept("p1", "b") is True
        assert store.find_by_identity("p1", "b") is None
        assert store.find_sources_for_concept("p1", "b") == []
        assert store.alias_matches("p1", "b alias") == []
        assert store.relationships_for("p1") == []
        assert store.remove_concept("p1", "b") is False

    def test_remove_concepts_not_in_batch(self, tmp_path):
        store = self._store(tmp_path)
        for ident in ("a", "b", "c"):
            store.upsert_concept("p1", _concept(ident))
        store.upsert_concept("p2", _concept("keeper"))
        store.replace_aliases("p1", "b", [_alias("b-alias")])
        store.replace_sources("p1", "b", [_source("b.php", "h")])
        store.upsert_relationship("p1", _relationship("a", "b"))
        store.upsert_relationship("p1", _relationship("b", "c"))
        store.upsert_relationship("p1", _relationship("a", "c"))
        store.mark_index_state("p1", "b", "h")

        removed = store.remove_concepts_not_in("p1", {"a", "c"})
        assert removed == 1
        assert [c["identity_key"] for c in store.list_concepts("p1")] == ["a", "c"]
        assert store.counts("p1")["relationships"] == 1  # only a->c survives
        assert store.alias_matches("p1", "b alias") == []
        assert store.find_sources_for_concept("p1", "b") == []
        # untouched project keeps its concept
        assert [c["identity_key"] for c in store.list_concepts("p2")] == ["keeper"]
        # keeping everything removes nothing
        assert store.remove_concepts_not_in("p1", {"a", "c"}) == 0
        assert store.counts("p1")["concepts"] == 2

    # -- lookups --------------------------------------------------------------

    def test_find_by_identity_and_prefix(self, tmp_path):
        store = self._store(tmp_path)
        store.upsert_concept("p1", _concept("PaymentService"))
        store.upsert_concept("p1", _concept("PaymentGateway"))
        store.upsert_concept("p1", _concept("UserService"))

        assert store.find_by_identity("p1", "PaymentService")["identity_key"] == (
            "PaymentService"
        )
        assert store.find_by_identity("p1", "missing") is None

        prefixed = [
            c["identity_key"] for c in store.find_by_identity_prefix("p1", "Pay")
        ]
        assert prefixed == ["PaymentGateway", "PaymentService"]

    def test_exact_matches_uses_normalized_name(self, tmp_path):
        store = self._store(tmp_path)
        store.upsert_concept("p1", _concept("PaymentService"))
        store.upsert_concept("p1", _concept("payment_service"))
        store.upsert_concept("p1", _concept("InvoiceBuilder"))
        hits = store.exact_matches("p1", "payment service")
        assert sorted(h["identity_key"] for h in hits) == [
            "PaymentService",
            "payment_service",
        ]

    # -- related traversal ----------------------------------------------------

    def _chain(self, tmp_path):
        store = self._store(tmp_path)
        for ident in ("a", "b", "c", "d"):
            store.upsert_concept("p1", _concept(ident))
        ids: dict[str, str] = {}
        for ident in ("a", "b", "c", "d"):
            ids[ident] = store.find_by_identity("p1", ident)["id"]
        store.upsert_relationship("p1", _relationship("a", "b"))
        store.upsert_relationship("p1", _relationship("b", "c"))
        store.upsert_relationship("p1", _relationship("c", "d"))
        store.upsert_relationship("p1", _relationship("b", "b"))  # self-loop
        return store, ids

    def test_related_respects_depth(self, tmp_path):
        store, ids = self._chain(tmp_path)
        near = store.related_nodes("p1", ids["a"], depth=1)
        assert sorted(n["identity_key"] for n in near["nodes"]) == ["a", "b"]
        assert near["truncated"] is False

    def test_related_includes_self_loop_at_depth_two(self, tmp_path):
        store, ids = self._chain(tmp_path)
        result = store.related_nodes("p1", ids["a"], depth=2)
        assert sorted(n["identity_key"] for n in result["nodes"]) == ["a", "b", "c"]
        assert result["nodes"][0]["distance"] == 0
        self_loops = [
            e
            for e in result["edges"]
            if e["source_concept_id"] == e["target_concept_id"]
        ]
        assert len(self_loops) == 1
        assert self_loops[0]["source_identity"] == "b"

    def test_related_cycles_terminate(self, tmp_path):
        store, ids = self._chain(tmp_path)
        store.upsert_relationship("p1", _relationship("c", "a"))  # back-edge
        result = store.related_nodes("p1", ids["a"], depth=10)
        assert sorted(n["identity_key"] for n in result["nodes"]) == ["a", "b", "c", "d"]
        assert result["truncated"] is False

    def test_related_truncates_on_caps(self, tmp_path):
        store, ids = self._chain(tmp_path)
        result = store.related_nodes("p1", ids["a"], depth=10, node_cap=2)
        assert sorted(n["identity_key"] for n in result["nodes"]) == ["a", "b"]
        assert result["truncated"] is True

        edge_capped = store.related_nodes(
            "p1", ids["a"], depth=10, edge_cap=1
        )
        assert len(edge_capped["edges"]) == 1
        assert edge_capped["truncated"] is True

    def test_related_unknown_seed_empty(self, tmp_path):
        store, _ids = self._chain(tmp_path)
        assert store.related_nodes("p1", "no-such-id") == {
            "nodes": [],
            "edges": [],
            "truncated": False,
        }

    # -- index state ----------------------------------------------------------

    def test_index_state_lifecycle(self, tmp_path):
        store = self._store(tmp_path)
        store.upsert_concept("p1", _concept("a"))
        store.upsert_concept("p1", _concept("b"))
        store.mark_index_state("p1", "a", "hash-1")
        store.mark_index_state("p1", "a", "hash-2")
        store.mark_index_state("p1", "b", "hash-b")

        pending = store.index_state_rows("p1", status="pending")
        assert sorted(r["identity_key"] for r in pending) == ["a", "b"]
        a_row = next(r for r in pending if r["identity_key"] == "a")
        assert a_row["content_hash"] == "hash-2"

        store.mark_index_state("p1", "a", "hash-2", status="indexed")
        pending = store.index_state_rows("p1", status="pending")
        assert [r["identity_key"] for r in pending] == ["b"]
        indexed = store.index_state_rows("p1", status="indexed")
        assert [r["identity_key"] for r in indexed] == ["a"]

        assert store.clear_index_states("p1") == 2
        assert store.index_state_rows("p1") == []

    # -- project-level --------------------------------------------------------

    def test_delete_project_cascades(self, tmp_path):
        store = self._store(tmp_path)
        store.upsert_concept("p1", _concept("a"))
        store.upsert_concept("p1", _concept("b"))
        store.upsert_concept("p2", _concept("keeper"))
        store.replace_aliases("p1", "a", [_alias("a-alias")])
        store.upsert_relationship("p1", _relationship("a", "b"))
        store.mark_index_state("p1", "a", "h")
        store.upsert_relationship("p2", _relationship("keeper", "keeper"))

        assert store.delete_project("p1") == 2
        assert store.list_concepts("p1") == []
        assert store.counts("p1") == {
            "concepts": 0,
            "aliases": 0,
            "relationships": 0,
            "pending_index": 0,
        }
        # untouched project survives, including its self-relationship
        assert [c["identity_key"] for c in store.list_concepts("p2")] == ["keeper"]
        assert store.counts("p2")["relationships"] == 1

    def test_counts_global_and_per_project(self, tmp_path):
        store = self._store(tmp_path)
        store.upsert_concept("p1", _concept("a"))
        store.upsert_concept("p2", _concept("b"))
        store.replace_aliases("p1", "a", [_alias("x")])
        store.mark_index_state("p1", "a", "h")
        assert store.counts("p1")["concepts"] == 1
        assert store.counts("p1")["aliases"] == 1
        assert store.counts("p1")["pending_index"] == 1
        assert store.counts()["concepts"] == 2

    def test_two_stores_on_separate_files_isolated(self, tmp_path):
        store_a = ConceptStore(db_path=str(tmp_path / "a.db"))
        store_b = ConceptStore(db_path=str(tmp_path / "b.db"))
        store_a.upsert_concept("p1", _concept("only-in-a"))
        assert [c["identity_key"] for c in store_a.list_concepts("p1")] == [
            "only-in-a"
        ]
        assert store_b.list_concepts("p1") == []
        store_b.upsert_concept("p1", _concept("only-in-b"))
        assert [c["identity_key"] for c in store_b.list_concepts("p1")] == [
            "only-in-b"
        ]
        assert [c["identity_key"] for c in store_a.list_concepts("p1")] == [
            "only-in-a"
        ]
