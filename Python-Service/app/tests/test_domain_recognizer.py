"""Phase 4 tests — domain-concept recognizer (hermetic).

The recognizer talks to the outside world through injectable seams that tests
replace with fakes:
  * ``store``               — a small in-memory ``FakeStore`` implementing only
    ``exact_matches`` / ``alias_matches`` / ``find_by_identity_prefix`` /
    ``related_nodes``,
  * ``qdrant_search``       — a canned callable for the semantic stage,
  * ``recognizer.embed_texts`` — a deterministic vector builder (patched so no
    semantic test touches the real embedding server).

No real Qdrant/embedding/Neo4j resources are used.
"""

from __future__ import annotations

from typing import Any

from projects.domain import recognizer as recognizer_module
from projects.domain.normalizer import ConceptNormalizer
from projects.domain.recognizer import (
    CALIBRATION,
    MAX_RELATIONSHIP_EDGES,
    MAX_SEMANTIC_CANDIDATES,
    recognize,
)

NORMALIZE = ConceptNormalizer().normalize


# --- fakes -------------------------------------------------------------------


class FakeStore:
    """Minimal in-memory store: only the methods ``recognize()`` uses."""

    def __init__(self) -> None:
        self._rows: dict[str, list[dict[str, Any]]] = {}
        self._rels: dict[str, list[dict[str, Any]]] = {}

    def add(self, project_id: str, row: dict[str, Any]) -> dict[str, Any]:
        self._rows.setdefault(project_id, []).append(row)
        return row

    def add_relationship(
        self, project_id: str, relation_row: dict[str, Any]
    ) -> None:
        self._rels.setdefault(project_id, []).append(dict(relation_row))

    # -- lookup surface used by recognize() -----------------------------------

    def exact_matches(
        self, project_id: str, normalized_name: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        return [
            row
            for row in self._rows.get(project_id, [])
            if row.get("normalized_name") == normalized_name
        ][:limit]

    def alias_matches(
        self, project_id: str, normalized_alias: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        # Mirrors ConceptStore.alias_matches: the queried alias is normalized
        # before the equality check, so raw/cased queries still find the row.
        found: list[dict[str, Any]] = []
        for row in self._rows.get(project_id, []):
            for alias in _raw_aliases(row):
                if NORMALIZE(alias) == NORMALIZE(normalized_alias):
                    found.append(row)
                    break
        return found[:limit]

    def find_by_identity_prefix(
        self, project_id: str, prefix: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        return [
            row
            for row in self._rows.get(project_id, [])
            if str(row.get("identity_key") or "").startswith(prefix)
        ][:limit]

    def related_nodes(
        self,
        project_id: str,
        concept_id: str,
        depth: int = 1,
        node_cap: int = 50,
        edge_cap: int = 200,
    ) -> dict[str, Any]:
        rows = self._rows.get(project_id, [])
        by_id = {str(row["id"]): row for row in rows}
        seed = by_id.get(str(concept_id))
        if seed is None:
            return {"nodes": [], "edges": [], "truncated": False}
        edges = [
            rel
            for rel in self._rels.get(project_id, [])
            if str(rel.get("source_concept_id")) == str(concept_id)
            or str(rel.get("target_concept_id")) == str(concept_id)
        ]
        nodes: list[dict[str, Any]] = [dict(seed, distance=0)]
        seen = {str(seed["id"])}
        for rel in edges:
            for side in ("source_concept_id", "target_concept_id"):
                node_id = str(rel.get(side) or "")
                if node_id in by_id and node_id not in seen:
                    seen.add(node_id)
                    nodes.append(dict(by_id[node_id], distance=1))
        return {"nodes": nodes, "edges": [dict(rel) for rel in edges], "truncated": False}


class RaisingStore(FakeStore):
    """Store whose lookup methods blow up (used by the total-behaviour test)."""

    def exact_matches(self, project_id: str, normalized_name: str, limit: int = 50):
        raise RuntimeError("store connection lost")


def _raw_aliases(row: dict[str, Any]) -> list[str]:
    raw: list[str] = []
    for item in row.get("aliases") or []:
        alias = item.get("alias") if isinstance(item, dict) else item
        if isinstance(alias, str) and alias.strip():
            raw.append(alias.strip())
    return raw


def _concept(
    identity: str,
    *,
    concept_type: str = "class",
    canonical: str | None = None,
    aliases: list[str] | None = None,
    roles: list[str] | None = None,
) -> dict[str, Any]:
    name = canonical or identity
    normalized = NORMALIZE(name)
    roles = list(roles or [concept_type])
    return {
        "id": "id-" + identity,
        "project_id": "",
        "identity_key": identity,
        "concept_type": concept_type,
        "canonical_name": name,
        "qualified_name": name,
        "normalized_name": normalized,
        "description": None,
        "roles": roles,
        "metadata": {"roles": roles},
        "aliases": list(aliases or []),
    }


def _relationship(
    relation_type: str,
    source_identity: str,
    target_identity: str,
    confidence: float = 1.0,
) -> dict[str, Any]:
    return {
        "relation_type": relation_type,
        "source_concept_id": "id-" + source_identity,
        "target_concept_id": "id-" + target_identity,
        "source_identity": source_identity,
        "target_identity": target_identity,
        "confidence": confidence,
    }


def _semantic_hit(
    score: float,
    identity: str,
    *,
    concept_type: str = "class",
    canonical: str | None = None,
) -> dict[str, Any]:
    name = canonical or identity
    roles = [concept_type]
    return {
        "score": score,
        "payload": {
            "concept_id": "id-" + identity,
            "project_id": "",
            "identity_key": identity,
            "concept_type": concept_type,
            "canonical_name": name,
            "qualified_name": name,
            "normalized_name": NORMALIZE(name),
            "roles": roles,
            "aliases": [],
        },
    }


def _fake_embed(texts: list[str]) -> list[list[float]]:
    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


# --- exact canonical ----------------------------------------------------------


class TestExactCanonical:
    def test_exact_canonical_match_accepted_at_one(self):
        store = FakeStore()
        store.add("p1", _concept("PaymentService"))

        result = recognize("payment service", "p1", store=store)

        assert result.error is None
        assert result.degraded is False
        assert result.query_note is None
        assert len(result.recognized) == 1
        assert result.ambiguous == []
        assert result.recognized[0]["identity_key"] == "PaymentService"
        assert result.recognized[0]["final_score"] == CALIBRATION["canonical_exact"]
        assert result.recognized[0]["match_kinds"] == ["exact"]
        assert result.recognized[0]["matched_alias"] is None


# --- identity prefix ----------------------------------------------------------


class TestIdentityPrefix:
    def test_identity_key_prefix_hit_accepted(self):
        store = FakeStore()
        store.add("p1", _concept("table:users", canonical="Users"))

        result = recognize("table:users", "p1", store=store)

        assert len(result.recognized) == 1
        assert result.recognized[0]["match_kinds"] == ["identity"]
        assert result.recognized[0]["final_score"] == CALIBRATION["canonical_exact"]

    def test_plain_word_query_never_touches_identity_prefix(self, monkeypatch):
        monkeypatch.setattr(recognizer_module, "embed_texts", _fake_embed)
        store = FakeStore()
        store.add("p1", _concept("PaymentGateway"))
        store.add("p1", _concept("PaymentService"))

        # "pay" is not identity-looking, so the prefix stage must not fire and
        # turn both Payment* identity keys into spurious 1.0 matches.
        result = recognize("pay", "p1", store=store, qdrant_search=lambda **kw: [])

        assert result.recognized == []
        assert result.ambiguous == []


# --- alias --------------------------------------------------------------------


class TestAlias:
    def test_alias_exact_accepted(self):
        store = FakeStore()
        store.add("p1", _concept("OrderBook", aliases=["orders"]))

        result = recognize("orders", "p1", store=store)

        assert len(result.recognized) == 1
        hit = result.recognized[0]
        assert hit["identity_key"] == "OrderBook"
        assert hit["match_kinds"] == ["alias_exact"]
        assert hit["final_score"] == CALIBRATION["alias_exact"]
        assert hit["matched_alias"] == "orders"

    def test_shared_alias_is_ambiguous_not_accepted(self):
        store = FakeStore()
        store.add("p1", _concept("OrdersService", aliases=["orders"]))
        store.add("p1", _concept("OrderHistoryService", aliases=["orders"]))

        result = recognize("orders", "p1", store=store)

        assert result.recognized == []
        assert len(result.ambiguous) == 2
        for entry in result.ambiguous:
            assert entry["match_kinds"] == ["alias_exact"]
            assert entry["final_score"] == CALIBRATION["alias_exact"]
        identities = {entry["identity_key"] for entry in result.ambiguous}
        assert identities == {"OrdersService", "OrderHistoryService"}

    def test_normalized_only_evidence_is_plain_alias_without_matched_alias(
        self,
    ):
        store = FakeStore()
        store.add("p1", _concept("UserService", aliases=["user-svc"]))

        result = recognize("user svc", "p1", store=store)

        assert len(result.recognized) == 1
        hit = result.recognized[0]
        assert hit["match_kinds"] == ["alias"]
        assert hit["final_score"] == 0.8  # raw 0.8 plain-alias band
        assert hit["matched_alias"] is None  # never echoes the raw query

    def test_alias_exact_reports_stored_spelling_not_raw_query(self):
        store = FakeStore()
        store.add("p1", _concept("OrderBook", aliases=["Orders"]))

        result = recognize("orders", "p1", store=store)

        assert len(result.recognized) == 1
        hit = result.recognized[0]
        assert hit["match_kinds"] == ["alias_exact"]
        assert hit["final_score"] == CALIBRATION["alias_exact"]
        assert hit["matched_alias"] == "Orders"  # stored spelling, not "orders"

    def test_stored_alias_spelling_match_scores_alias_exact(self):
        store = FakeStore()
        store.add("p1", _concept("UserService", aliases=["user-svc"]))

        result = recognize("user-svc", "p1", store=store)

        assert len(result.recognized) == 1
        hit = result.recognized[0]
        assert hit["match_kinds"] == ["alias_exact"]
        assert hit["final_score"] == CALIBRATION["alias_exact"]
        assert hit["matched_alias"] == "user-svc"


# --- type-hint boost ----------------------------------------------------------


class TestTypeHint:
    def test_type_hint_boost_flips_below_threshold_semantic_into_accepted(
        self, monkeypatch
    ):
        monkeypatch.setattr(recognizer_module, "embed_texts", _fake_embed)
        hits = [
            _semantic_hit(0.58, "BillingAssistant", concept_type="service"),
            _semantic_hit(0.55, "LedgerView", concept_type="model"),
        ]
        store = FakeStore()

        no_hint = recognize(
            "billing assistant report", "p1", store=store, qdrant_search=lambda **kw: hits
        )
        assert no_hint.recognized == []
        assert len(no_hint.rejected) >= 1

        hinted = recognize(
            "billing assistant report",
            "p1",
            store=store,
            qdrant_search=lambda **kw: hits,
            type_hint="service",
        )
        assert len(hinted.recognized) == 1
        top = hinted.recognized[0]
        assert top["identity_key"] == "BillingAssistant"
        expected = 0.58 + CALIBRATION["type_hint_boost"]
        assert top["final_score"] == expected
        assert top["final_score"] >= CALIBRATION["min_accept"]


# --- merge --------------------------------------------------------------------


class TestMerge:
    def test_same_concept_two_stages_keeps_max_and_both_kinds(self):
        store = FakeStore()
        store.add("p1", _concept("PaymentService", aliases=["payment-service"]))

        result = recognize("payment service", "p1", store=store)

        assert len(result.recognized) == 1
        hit = result.recognized[0]
        assert hit["identity_key"] == "PaymentService"
        assert hit["final_score"] == 1.0  # MAX(1.0 exact, 0.8 alias), not 0.9 avg
        assert hit["match_kinds"] == ["exact", "alias"]
        assert hit["matched_alias"] is None  # normalized-only alias evidence


# --- caps ---------------------------------------------------------------------


class TestCaps:
    def test_many_candidates_produce_bounded_outputs(self):
        store = FakeStore()
        for index in range(16):
            identity = f"WidgetNumber{index:02d}"
            store.add("p1", _concept(identity, aliases=["widget"]))

        result = recognize("widget", "p1", store=store)

        assert result.recognized == []
        # 16 candidates merge down past MAX_MERGE and the same-tier dead heat is
        # ambiguous; every output list stays deterministically capped at five.
        assert len(result.ambiguous) <= 5
        assert len(result.rejected) <= 5
        assert len({entry["identity_key"] for entry in result.ambiguous}) == len(
            result.ambiguous
        )
        assert result.error is None

    def test_semantic_provider_receives_bounded_limit(self, monkeypatch):
        monkeypatch.setattr(recognizer_module, "embed_texts", _fake_embed)
        calls: dict[str, Any] = {"limit": None}
        store = FakeStore()

        def search(**kwargs):
            calls["limit"] = kwargs.get("limit")
            return []

        recognize("no such concept anywhere", "p1", store=store, qdrant_search=search)

        assert calls["limit"] == MAX_SEMANTIC_CANDIDATES


# --- query notes + fuzzy stub -------------------------------------------------


class TestNotes:
    def test_empty_and_short_query_return_notes(self):
        store = FakeStore()
        empty = recognize("   ", "p1", store=store)
        assert empty.recognized == []
        assert empty.query_note == "empty query"
        assert empty.degraded is False

        short = recognize("x", "p1", store=store)
        assert short.recognized == []
        assert short.query_note == "query too short"


class TestFuzzy:
    def test_fuzzy_stub_degrades_with_note(self):
        store = FakeStore()
        store.add("p1", _concept("PaymentService"))

        result = recognize("payment servic", "p1", store=store, fuzzy=True)

        assert result.recognized == []
        assert result.degraded is True
        assert result.query_note == "fuzzy search not supported"
        assert "fuzzy" in result.query_note


# --- isolation + degradation --------------------------------------------------


class TestIsolation:
    def test_cross_project_concepts_do_not_leak(self, monkeypatch):
        monkeypatch.setattr(recognizer_module, "embed_texts", _fake_embed)
        store = FakeStore()
        store.add("p1", _concept("PaymentService"))

        in_project = recognize("payment service", "p1", store=store)
        assert [h["identity_key"] for h in in_project.recognized] == ["PaymentService"]

        out_project = recognize(
            "payment service", "p2", store=store, qdrant_search=lambda **kw: []
        )
        assert out_project.recognized == []
        assert out_project.ambiguous == []


class TestDegraded:
    def test_semantic_failure_degrades_and_deterministic_stages_survive(
        self, monkeypatch
    ):
        monkeypatch.setattr(recognizer_module, "embed_texts", _fake_embed)
        calls = {"count": 0}

        def broken_search(**kwargs):
            calls["count"] += 1
            raise RuntimeError("qdrant unavailable")

        store = FakeStore()
        store.add("p1", _concept("PaymentService"))
        store.add("p1", _concept("OrderBook", aliases=["orders"]))

        # Deterministic exact + alias present: the semantic provider is never
        # invoked and recognition is fully preserved.
        deterministic = recognize(
            "payment service", "p1", store=store, qdrant_search=broken_search
        )
        assert deterministic.degraded is False
        assert [h["identity_key"] for h in deterministic.recognized] == [
            "PaymentService"
        ]
        assert calls["count"] == 0

        # Only when nothing deterministic matched does the semantic stage run,
        # and its failure degrades the result instead of raising.
        degraded = recognize(
            "unknown concept phrase", "p1", store=store, qdrant_search=broken_search
        )
        assert degraded.degraded is True
        assert degraded.recognized == []
        assert degraded.ambiguous == []
        assert calls["count"] == 1


# --- relationships ------------------------------------------------------------


class TestRelationships:
    def test_edges_deduped_across_seeds_and_capped(self):
        store = FakeStore()
        store.add("p1", _concept("Widget"))
        store.add("p1", _concept("Gadget", aliases=["widget"]))
        store.add("p1", _concept("Sprocket"))
        for index in range(30):
            store.add("p1", _concept(f"ExtraNode{index:02d}"))
        # X -> Y appears in the expansion of BOTH accepted seeds.
        store.add_relationship("p1", _relationship("calls", "Widget", "Gadget"))
        for index in range(30):
            store.add_relationship(
                "p1", _relationship("calls", "Widget", f"ExtraNode{index:02d}")
            )

        result = recognize("widget", "p1", store=store)

        assert {h["identity_key"] for h in result.recognized} == {
            "Widget",
            "Gadget",
        }
        assert len(result.relationships) <= MAX_RELATIONSHIP_EDGES
        assert len(result.relationships) == MAX_RELATIONSHIP_EDGES
        widget_calls = [
            rel
            for rel in result.relationships
            if rel["relation_type"] == "calls"
            and rel["source"]["identity_key"] == "Widget"
            and rel["target"]["identity_key"] == "Gadget"
        ]
        assert len(widget_calls) == 1  # deduped even though two seeds hit it


# --- total behaviour ----------------------------------------------------------


class TestTotal:
    def test_never_raises_on_garbage_inputs_and_store_errors(self):
        store = FakeStore()

        assert recognize("", "p1", store=store).query_note == "empty query"
        assert recognize(None, "p1", store=store).query_note == "empty query"
        assert recognize(12345, "p1", store=store).query_note == "empty query"

        broken = RaisingStore()
        result = recognize("payment service", "p1", store=broken)
        assert result.degraded is True
        assert result.error is not None
        assert len(result.error) <= 300
        assert result.recognized == []
