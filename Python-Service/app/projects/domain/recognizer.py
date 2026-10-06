"""Recognition entry point for the domain-concept layer (Phase 4).

``recognize()`` maps one free-form query onto the project's indexed domain
concepts and returns **verified hints**: deterministic exact/alias/identity
matches against the canonical SQLite store (``projects.domain.store``) plus
bounded semantic matches against the mirrored Qdrant collection
(``projects.domain.indexer``). The hints tell a caller which indexed concepts a
query touches — they are NOT proof that any downstream answer is correct, and
the recognizer never writes the raw query to logs or error text (privacy).

Pipeline (every stage guarded; the function never raises):

1. strip the query; empty/one-character queries return an empty result with a
   one-line ``query_note`` (never the raw query),
2. exact canonical-name match          -> raw 1.0, kind ``'exact'``,
3. identity-key prefix match           -> raw 1.0, kind ``'identity'`` — only
   for identity-looking queries and only when the exact stage found nothing,
4. alias match                         -> kind ``'alias_exact'`` (raw 0.95) when
   a matched stored alias spelling equals the query, else kind ``'alias'``
   (raw 0.8) for a merely normalized-equivalent spelling; the evidence spelling
   surfaced as ``matched_alias`` is always a stored spelling, never the raw
   query text,
5. semantic Qdrant search (raw = cosine score, clamped 0..1) — attempted ONLY
   when the deterministic stages (exact/identity/alias) found nothing; embed or
   Qdrant failure sets ``degraded`` and skips the stage silently,
6. merge per concept id: final score = MAX of the raw scores (never the mean),
   optional ``type_hint`` boost, cap the merged pool,
7. rank/filter with the gap rule below,
8. relationship expansion for accepted concepts (bounded + deduped).

Ranking + ambiguity rule (implemented precisely)
------------------------------------------------
Each concept's evidence is merged (``final_score = max(raw_scores)``), then the
pool is sorted by ``final_score`` desc, ``canonical_name`` asc. Candidates below
``min_accept`` are rejected outright. Walking the rest from the top, every
candidate is compared ONLY against *same match-tier* rivals within
``ambiguity_gap`` (tier = strongest kind: exact/identity = 3, alias_exact = 2,
alias/semantic = 1):

* if such a rival exists, the whole same-tier cluster within the gap is
  ambiguous — a same-tier dead heat never auto-accepts on score alone,
* otherwise the candidate is accepted.

A lower-tier candidate is never a rival of a higher-tier candidate, so a
definitive hit (``exact``/``alias_exact``/``identity``) always outranks a
semantic near-tie instead of being dragged into ambiguity by it. Accepted,
ambiguous and rejected lists are capped at five entries each,
deterministically, in the same sorted order.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from index.embeddings import embed_texts
from infra.settings import settings
from projects.domain.indexer import CONCEPT_COLLECTION, collection_for_project
from projects.domain.normalizer import ConceptNormalizer
from projects.domain.store import concept_store

# Tunable recognition thresholds.
CALIBRATION: dict[str, float] = {
    "min_accept": 0.60,
    "ambiguity_gap": 0.05,
    "alias_exact": 0.95,
    "canonical_exact": 1.0,
    "type_hint_boost": 0.05,
    "semantic_weight": 1.0,
}

MAX_SEMANTIC_CANDIDATES = 8
MAX_MERGE = 12
MAX_EXPAND_CONCEPTS = 5
MAX_EXPAND_DEPTH = 1
MAX_RELATIONSHIP_EDGES = 20

# Deterministic caps on the ranked output lists (accepted/ambiguous/rejected).
_OUTPUT_CAP = 5

# Plain alias matches (spelling differs from the query) score just below the
# alias_exact band but still far above the semantic threshold.
_ALIAS_PLAIN_SCORE = 0.8

# Strongest-kind rank used to decide which candidates may rival one another.
_TIER_RANK: dict[str, int] = {
    "exact": 3,
    "identity": 3,
    "alias_exact": 2,
    "alias": 1,
    "semantic": 1,
}

# Identity keys carry namespace/kind markers ("table:user", "route:get:/x",
# "App\\Service"); plain word queries never consult the identity prefix stage.
_IDENTITY_MARKERS = frozenset("\\:/._-")

_MAX_ERROR_CHARS = 300

_NORMALIZE = ConceptNormalizer().normalize

logger = logging.getLogger(__name__)


@dataclass
class MatchCandidate:
    """One recognition hypothesis produced by a single pipeline stage."""

    concept: dict[str, Any]
    match_kind: str
    raw_score: float
    final_score: float
    type_hint: bool = False
    matched_alias: str | None = None


@dataclass
class RecognitionResult:
    """Total, never-raising output of one ``recognize()`` call.

    ``recognized``/``ambiguous`` entries are the canonical store rows enriched
    with ``final_score``, ``match_kinds`` and ``matched_alias`` (the store's
    own spelling of the alias that matched — never the raw query); ``rejected``
    entries stay minimal (identity + canonical name + score). ``error`` holds a
    truncated internal message only when the pipeline degraded at the top
    level; ``query_note`` is a one-line status note (never the raw query).
    """

    recognized: list[dict[str, Any]] = field(default_factory=list)
    ambiguous: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    degraded: bool = False
    error: str | None = None
    query_note: str | None = None


__all__ = [
    "CALIBRATION",
    "CONCEPT_COLLECTION",
    "MAX_EXPAND_CONCEPTS",
    "MAX_EXPAND_DEPTH",
    "MAX_MERGE",
    "MAX_RELATIONSHIP_EDGES",
    "MAX_SEMANTIC_CANDIDATES",
    "MatchCandidate",
    "RecognitionResult",
    "recognize",
]


def recognize(
    query: str,
    project_id: str,
    *,
    store: Any | None = None,
    qdrant_search: Callable[..., list[dict[str, Any]]] | None = None,
    type_hint: str | None = None,
    fuzzy: bool = False,
    expand_relationships: bool = True,
) -> RecognitionResult:
    """Recognize which indexed domain concepts a query touches (never raises).

    Returns VERIFIED HINTS — deterministic exact/alias/identity matches plus
    bounded semantic matches against the project's indexed concepts. This is
    not proof that a downstream answer is correct, and the raw query is never
    logged or surfaced in ``error``/``query_note``.

    ``store`` defaults to the shared ``concept_store`` singleton. ``fuzzy`` is a
    stub (off): when true the call returns a degraded result noting that fuzzy
    search is unsupported instead of fabricating matches. ``qdrant_search`` may
    be injected for hermetic tests; its default is a fresh Qdrant client per
    call built from infra settings (never reused, never raised into callers —
    failures degrade the semantic stage silently).
    """
    try:
        return _recognize(
            query=query,
            project_id=project_id,
            store=store,
            qdrant_search=qdrant_search,
            type_hint=type_hint,
            fuzzy=fuzzy,
            expand_relationships=expand_relationships,
        )
    except Exception as exc:  # noqa: BLE001 — recognize() is total by contract
        logger.warning("recognize() degraded: %s", _trim(str(exc)))
        return RecognitionResult(
            rejected=[], degraded=True, error=_trim(str(exc), _MAX_ERROR_CHARS)
        )


# --- main pipeline ------------------------------------------------------------


def _recognize(
    *,
    query: str,
    project_id: str,
    store: Any | None,
    qdrant_search: Callable[..., list[dict[str, Any]]] | None,
    type_hint: str | None,
    fuzzy: bool,
    expand_relationships: bool,
) -> RecognitionResult:
    if fuzzy:
        return RecognitionResult(
            rejected=[],
            degraded=True,
            query_note="fuzzy search not supported",
        )

    cleaned = _clean_query(query)
    if not cleaned:
        return RecognitionResult(query_note="empty query")
    if len(cleaned) < 2:
        return RecognitionResult(query_note="query too short")

    store = store or concept_store
    qdrant_search = qdrant_search or _default_semantic_search

    candidates = _deterministic_candidates(store, project_id, cleaned)

    degraded = False
    if not candidates:
        semantic, degraded = _semantic_candidates(
            project_id, cleaned, qdrant_search
        )
        candidates = semantic

    merged = _merge_candidates(candidates, type_hint)
    if not merged:
        return RecognitionResult(degraded=degraded)

    accepted, ambiguous, rejected = _partition(merged)
    relationships: list[dict[str, Any]] = []
    if expand_relationships and accepted:
        relationships = _expand_relationships(store, project_id, accepted)

    return RecognitionResult(
        recognized=_enrich(accepted),
        ambiguous=_enrich(ambiguous),
        rejected=_minimal_rejected(rejected),
        relationships=relationships,
        degraded=degraded,
        error=None,
        query_note=None,
    )


def _clean_query(query: Any) -> str:
    if not isinstance(query, str):
        return ""
    return query.strip()


def _deterministic_candidates(
    store: Any, project_id: str, cleaned: str
) -> list[MatchCandidate]:
    """Exact canonical + identity prefix + alias stage candidates."""
    normalized = _NORMALIZE(cleaned)
    candidates: list[MatchCandidate] = []

    exact_rows = store.exact_matches(project_id, normalized)
    for row in exact_rows:
        candidates.append(
            MatchCandidate(
                concept=row,
                match_kind="exact",
                raw_score=CALIBRATION["canonical_exact"],
                final_score=CALIBRATION["canonical_exact"],
            )
        )

    # Identity prefix is a rare, separate path: it only fires for queries that
    # look like identity keys ("table:user") and only when no canonical name
    # matched (a canonical exact hit is already authoritative).
    if not exact_rows and _looks_like_identity(cleaned):
        for row in store.find_by_identity_prefix(project_id, cleaned):
            candidates.append(
                MatchCandidate(
                    concept=row,
                    match_kind="identity",
                    raw_score=CALIBRATION["canonical_exact"],
                    final_score=CALIBRATION["canonical_exact"],
                )
            )

    alias_rows = store.alias_matches(project_id, cleaned)
    for row in alias_rows:
        kind, raw, matched_alias = _alias_kind_and_score(row, cleaned)
        candidates.append(
            MatchCandidate(
                concept=row,
                match_kind=kind,
                raw_score=raw,
                final_score=raw,
                matched_alias=matched_alias,
            )
        )

    return candidates


def _looks_like_identity(text: str) -> bool:
    return any(char in _IDENTITY_MARKERS for char in text)


def _alias_kind_and_score(
    row: dict[str, Any], cleaned: str
) -> tuple[str, float, str | None]:
    """Classify an alias hit against the REAL stored alias spelling.

    When a stored spelling equals the cleaned query (case-insensitive, after
    stripping) the hit is ``alias_exact`` (raw 0.95) and ``matched_alias`` is
    THAT stored spelling. A hit supported only by normalized equality is plain
    ``alias`` (raw 0.8); its ``matched_alias`` stays ``None`` — the user query
    is never echoed as alias evidence.
    """
    for spelling in _stored_alias_spellings(row):
        if _same_spelling(spelling, cleaned):
            return "alias_exact", CALIBRATION["alias_exact"], spelling
    return "alias", _ALIAS_PLAIN_SCORE, None


def _stored_alias_spellings(row: dict[str, Any]) -> list[str]:
    """Stored alias spellings carried by a row (matched alias + explicit list).

    ``alias_matches`` attaches the exact stored spelling as ``matched_alias``
    when one equals the queried alias; some rows (semantic payloads, fakes)
    instead carry the concept's explicit ``aliases`` list. Both sources are
    real store spellings, never the raw query text.
    """
    spellings: list[str] = []
    matched = row.get("matched_alias")
    if isinstance(matched, str) and matched.strip():
        spellings.append(matched.strip())
    value = row.get("aliases")
    if not isinstance(value, list):
        return spellings
    for item in value:
        alias = item.get("alias") if isinstance(item, dict) else item
        if isinstance(alias, str) and alias.strip():
            spellings.append(alias.strip())
    return spellings


def _same_spelling(left: str, right: str) -> bool:
    return left.casefold() == right.strip().casefold()


# --- semantic stage -----------------------------------------------------------


def _default_semantic_search(
    project_id: str,
    vector: list[float],
    limit: int = MAX_SEMANTIC_CANDIDATES,
    collection: str = CONCEPT_COLLECTION,
) -> list[dict[str, Any]]:
    """Fresh Qdrant search against the domain-concepts collection.

    The client is built per call from infra settings exactly like the indexer's
    private helper. Transport failures raise into the guarded semantic stage,
    which degrades instead of letting them escape ``recognize()``.
    """
    kwargs = {"url": settings.qdrant_url}
    if settings.qdrant_api_key:
        kwargs["api_key"] = settings.qdrant_api_key
    client = QdrantClient(**kwargs)
    response = client.query_points(
        collection_name=collection,
        query=vector,
        query_filter=Filter(
            must=[
                FieldCondition(
                    key="project_id", match=MatchValue(value=project_id)
                )
            ]
        ),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    points = list(getattr(response, "points", None) or [])
    hits: list[dict[str, Any]] = []
    for point in points:
        hits.append(
            {
                "score": float(getattr(point, "score", 0.0) or 0.0),
                "payload": dict(getattr(point, "payload", None) or {}),
            }
        )
    return hits


def _semantic_candidates(
    project_id: str,
    cleaned: str,
    qdrant_search: Callable[..., list[dict[str, Any]]],
) -> tuple[list[MatchCandidate], bool]:
    """Embed the query and search the mirrored collection (guarded).

    Failure to embed or to reach Qdrant returns no candidates and reports
    ``degraded``; the error is logged without the query text. The injected
    search callable receives ``project_id``, the ``vector`` and the candidate
    ``limit`` and returns ``[{"score": float, "payload": dict}, ...]``.
    """
    try:
        vectors = embed_texts([cleaned], input_type="search_query")
        if not vectors or not isinstance(vectors[0], list) or not vectors[0]:
            logger.warning("semantic recognition stage: empty embedding returned")
            return [], True
        hits = qdrant_search(
            project_id=project_id,
            vector=list(vectors[0]),
            limit=MAX_SEMANTIC_CANDIDATES,
            collection=collection_for_project(project_id),
        )
    except Exception as exc:  # noqa: BLE001 — semantic search degrades
        logger.warning("semantic recognition stage degraded: %s", _trim(str(exc)))
        return [], True

    candidates: list[MatchCandidate] = []
    for hit in hits[:MAX_SEMANTIC_CANDIDATES]:
        row = _payload_row(project_id, _hit_payload(hit))
        if not row.get("id"):
            continue
        raw = _clamp01(_hit_score(hit)) * CALIBRATION["semantic_weight"]
        candidates.append(
            MatchCandidate(
                concept=row,
                match_kind="semantic",
                raw_score=raw,
                final_score=raw,
            )
        )
    return candidates, False


def _hit_payload(hit: Any) -> dict[str, Any]:
    if isinstance(hit, dict):
        payload = hit.get("payload")
        return payload if isinstance(payload, dict) else {}
    return dict(getattr(hit, "payload", None) or {})


def _hit_score(hit: Any) -> float:
    if isinstance(hit, dict):
        value = hit.get("score")
    else:
        value = getattr(hit, "score", None)
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _payload_row(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Turn a Qdrant payload back into a store-shaped concept row."""
    roles = payload.get("roles")
    roles = list(roles) if isinstance(roles, list) else []
    aliases = payload.get("aliases")
    aliases = list(aliases) if isinstance(aliases, list) else []
    canonical = str(
        payload.get("canonical_name") or payload.get("identity_key") or ""
    ).strip()
    return {
        "id": str(payload.get("concept_id") or ""),
        "project_id": str(payload.get("project_id") or project_id),
        "identity_key": str(payload.get("identity_key") or ""),
        "concept_type": str(payload.get("concept_type") or "concept"),
        "canonical_name": canonical,
        "qualified_name": str(payload.get("qualified_name") or "").strip()
        or canonical,
        "normalized_name": str(payload.get("normalized_name") or "").strip(),
        "description": None,
        "roles": list(roles),
        "metadata": {"roles": list(roles)},
        "aliases": list(aliases),
    }


def _clamp01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


# --- merge + type-hint boost --------------------------------------------------


def _merge_candidates(
    candidates: list[MatchCandidate], type_hint: str | None
) -> list[dict[str, Any]]:
    """Merge stage hypotheses per concept id: final = MAX of raw scores."""
    merged: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        row = candidate.concept
        concept_id = row.get("id")
        if not concept_id:
            continue
        entry = merged.setdefault(
            concept_id,
            {
                "concept": row,
                "kinds": [],
                "raw_scores": [],
                "matched_alias": None,
                "final_score": 0.0,
            },
        )
        if candidate.match_kind not in entry["kinds"]:
            entry["kinds"].append(candidate.match_kind)
        entry["raw_scores"].append(candidate.raw_score)
        entry["final_score"] = max(entry["raw_scores"])
        if candidate.matched_alias and not entry["matched_alias"]:
            entry["matched_alias"] = candidate.matched_alias

    pool = list(merged.values())
    for entry in pool:
        if (
            type_hint
            and entry["final_score"] < CALIBRATION["canonical_exact"]
            and _type_hint_matches(entry["concept"], type_hint)
        ):
            entry["final_score"] += CALIBRATION["type_hint_boost"]
            entry["boosted"] = True

    pool.sort(
        key=lambda entry: (
            -entry["final_score"],
            str(entry["concept"].get("canonical_name") or ""),
        )
    )
    return pool[:MAX_MERGE]


def _type_hint_matches(row: dict[str, Any], type_hint: str) -> bool:
    """Does the concept belong to the requested type/role hint (non-strict)?"""
    hint = str(type_hint).strip().casefold()
    if not hint:
        return False
    concept_type = str(row.get("concept_type") or "").strip().casefold()
    if concept_type and concept_type == hint:
        return True

    values: list[str] = []
    roles = row.get("roles")
    if isinstance(roles, list):
        values.extend(str(role) for role in roles)
    metadata = row.get("metadata")
    if isinstance(metadata, dict):
        for key in ("roles", "role", "type_hint"):
            value = metadata.get(key)
            if isinstance(value, list):
                values.extend(str(item) for item in value)
            elif isinstance(value, str):
                values.append(value)
    return hint in {value.strip().casefold() for value in values if value.strip()}


# --- ranking + ambiguity ------------------------------------------------------


def _partition(
    pool: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split merged candidates into accepted / ambiguous / rejected.

    Implements the rule documented in the module docstring: same-tier rivals
    within ``ambiguity_gap`` of the top candidate form an ambiguous cluster;
    a candidate with no such rival is accepted. Output lists are capped at
    ``_OUTPUT_CAP`` each, in deterministic (score desc, canonical asc) order.
    """
    accepted: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    qualifying = [
        entry
        for entry in pool
        if entry["final_score"] >= CALIBRATION["min_accept"]
    ]
    remaining = list(qualifying)

    while remaining:
        head = remaining[0]
        rivals = [
            entry
            for entry in remaining[1:]
            if _tier_of(entry) == _tier_of(head)
            and head["final_score"] - entry["final_score"]
            < CALIBRATION["ambiguity_gap"]
        ]
        if rivals:
            group = [head, *rivals]
            ambiguous.extend(group)
            group_ids = {entry["concept"].get("id") for entry in group}
            remaining = [
                entry
                for entry in remaining
                if entry["concept"].get("id") not in group_ids
            ]
        else:
            accepted.append(head)
            remaining = remaining[1:]

    accepted = _sorted_entries(accepted)[:_OUTPUT_CAP]
    ambiguous = _sorted_entries(ambiguous)[:_OUTPUT_CAP]
    accepted_ids = {entry["concept"].get("id") for entry in accepted}
    ambiguous_ids = {entry["concept"].get("id") for entry in ambiguous}
    rejected = [
        entry
        for entry in pool
        if entry["concept"].get("id") not in accepted_ids
        and entry["concept"].get("id") not in ambiguous_ids
    ]
    rejected = _sorted_entries(rejected)[:_OUTPUT_CAP]
    return accepted, ambiguous, rejected


def _tier_of(entry: dict[str, Any]) -> int:
    return max(_TIER_RANK.get(kind, 1) for kind in entry["kinds"])


def _sorted_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        entries,
        key=lambda entry: (
            -entry["final_score"],
            str(entry["concept"].get("canonical_name") or ""),
        ),
    )


# --- output shaping -----------------------------------------------------------


def _enrich(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Store rows plus the recognition evidence that placed them."""
    enriched: list[dict[str, Any]] = []
    for entry in entries:
        row = dict(entry["concept"])
        row["final_score"] = round(float(entry["final_score"]), 6)
        row["match_kinds"] = list(entry["kinds"])
        row["matched_alias"] = entry["matched_alias"]
        enriched.append(row)
    return enriched


def _minimal_rejected(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "identity_key": str(entry["concept"].get("identity_key") or ""),
            "canonical_name": str(entry["concept"].get("canonical_name") or ""),
            "final_score": round(float(entry["final_score"]), 6),
        }
        for entry in entries
    ]


def _expand_relationships(
    store: Any, project_id: str, accepted: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Verified neighbourhood edges around accepted concepts (bounded, deduped).

    For up to ``MAX_EXPAND_CONCEPTS`` accepted concepts the store's bounded
    traversal is called; edges are deduped by (relation_type, source identity,
    target identity) and capped at ``MAX_RELATIONSHIP_EDGES``. A store failure
    degrades only this bonus section — recognized concepts are kept.
    """
    canonical_by_identity: dict[str, str] = {}
    for entry in accepted[:MAX_EXPAND_CONCEPTS]:
        row = entry["concept"]
        identity = row.get("identity_key")
        canonical = row.get("canonical_name")
        if identity:
            canonical_by_identity.setdefault(
                str(identity), str(canonical or identity)
            )

    collected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for entry in accepted[:MAX_EXPAND_CONCEPTS]:
        try:
            result = store.related_nodes(
                project_id,
                entry["concept"].get("id"),
                depth=MAX_EXPAND_DEPTH,
                node_cap=50,
                edge_cap=200,
            )
        except Exception as exc:  # noqa: BLE001 — relationships are a bonus
            logger.warning(
                "relationship expansion for %s degraded: %s",
                entry["concept"].get("identity_key"),
                _trim(str(exc)),
            )
            continue
        for node in result.get("nodes") or []:
            identity = node.get("identity_key")
            if identity:
                canonical_by_identity.setdefault(
                    str(identity),
                    str(node.get("canonical_name") or identity),
                )
        for edge in result.get("edges") or []:
            if len(collected) >= MAX_RELATIONSHIP_EDGES:
                break
            relation_type = edge.get("relation_type")
            source_identity = edge.get("source_identity")
            target_identity = edge.get("target_identity")
            if not relation_type or not source_identity or not target_identity:
                continue
            dedupe_key = (
                str(relation_type),
                str(source_identity),
                str(target_identity),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            collected.append(
                {
                    "relation_type": str(relation_type),
                    "source": {
                        "identity_key": str(source_identity),
                        "canonical_name": canonical_by_identity.get(
                            str(source_identity), str(source_identity)
                        ),
                    },
                    "target": {
                        "identity_key": str(target_identity),
                        "canonical_name": canonical_by_identity.get(
                            str(target_identity), str(target_identity)
                        ),
                    },
                    "confidence": _edge_confidence(edge),
                }
            )
    return collected


def _edge_confidence(edge: dict[str, Any]) -> float:
    try:
        return round(float(edge.get("confidence") or 0.0), 6)
    except (TypeError, ValueError):
        return 0.0


def _trim(text: str, limit: int = _MAX_ERROR_CHARS) -> str:
    value = str(text).strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "..."
