"""Semantic (Qdrant) indexing layer for the domain-concept recognition layer.

Phase 3 mirrors the canonical SQLite concepts (``projects.domain.store``) into a
Qdrant semantic collection so domain facts can be found by meaning, not just by
name. Every public entry is **idempotent** and **total**:

  * ``content_hash`` of a deterministic ``embed_input`` text drives the skip
    logic: an unchanged canonical row is never re-embedded.
  * Retry state is persisted in ``index_state`` — a failed point stays
    ``pending`` with its ``last_error`` and is retried on the next run.
  * ``reconcile_vectors``/``purge_project_vectors`` delete Qdrant points whose
    canonical row no longer exists (or when a project is rebuilt from zero).

None of the public functions raise: risky internals (Qdrant transport, embed
server, the shared store) are guarded and their failures are reported through
the returned dict and the module logger.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from index.embeddings import embed_texts
from infra.settings import settings
from projects.domain.store import ConceptStore, concept_store

# Single semantic collection holds every project's mirrored concepts; the
# ``project_id`` payload key keeps the namespace separable (filters + purge).
CONCEPT_COLLECTION = "domain_concepts"
COHERE_CONCEPT_COLLECTION = "domain_concepts_cohere_v5"

# The only row fields ``embed_input`` records — provenance for what the
# semantic vector may change on. Aliases/descriptions live on the row; roles
# are decoded out of the ``metadata`` JSON column by the store.
_EMBED_INPUT_KEYS = (
    "concept_type",
    "canonical_name",
    "qualified_name",
    "aliases",
    "description",
    "roles",
)

_DIM_PROBE_TEXT = "x"
_MAX_SOURCE_FILES = 5
_STATE_LOOKUP_LIMIT = 5000
_SCROLL_PAGE_SIZE = 512
_RECONCILE_POINT_CAP = 10_000
_DONE = "done"
_PENDING = "pending"

logger = logging.getLogger(__name__)

# Shared canonical store. The module reads it at call time so tests may point
# it at a throwaway database file.
_store = concept_store


# --- deterministic content ---------------------------------------------------


def _alias_names(value: Any) -> list[str]:
    """Normalise an aliases value (list of strings or ``{"alias": ...}``)."""
    names: list[str] = []
    for item in value or []:
        name = item.get("alias") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return sorted(set(names))


def _role_names(row: dict[str, Any]) -> list[str]:
    """Roles decoded at the store boundary; fall back to raw metadata."""
    roles = row.get("roles")
    if roles is None and isinstance(row.get("metadata"), dict):
        roles = row["metadata"].get("roles")
    names: list[str] = []
    if isinstance(roles, list):
        for role in roles:
            if isinstance(role, str) and role.strip():
                names.append(role.strip())
    return sorted(set(names))


def _source_files(row: dict[str, Any]) -> list[str]:
    """Short, deduped list of source files (caller-enriched rows only)."""
    files: list[str] = []
    sources = row.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict):
                path = source.get("source_file") or source.get("file")
            else:
                path = source
            if isinstance(path, str) and path.strip():
                files.append(path.strip())
    unique: list[str] = []
    for path in files:
        if path not in unique:
            unique.append(path)
        if len(unique) >= _MAX_SOURCE_FILES:
            break
    return unique


def embed_input(concept_row: dict[str, Any]) -> str:
    """Deterministic contextual text for one canonical concept row.

    Records only fields whose provenance the store can vouch for
    (``_EMBED_INPUT_KEYS``); aliases and roles are sorted so semantic content
    is stable regardless of discovery order.
    """
    concept_type = str(concept_row.get("concept_type") or "concept").strip()
    canonical_name = str(concept_row.get("canonical_name") or "").strip()
    qualified_name = (
        str(concept_row.get("qualified_name") or "").strip() or canonical_name
    )
    aliases = ", ".join(_alias_names(concept_row.get("aliases")))
    raw_description = concept_row.get("description")
    description = (
        str(raw_description).strip()
        if raw_description is not None and str(raw_description).strip()
        else "—"
    )
    roles = ", ".join(_role_names(concept_row))
    return "\n".join(
        [
            f"Type: {concept_type}",
            f"Name: {canonical_name}",
            f"Qualified name: {qualified_name}",
            f"Aliases: {aliases}",
            f"Description: {description}",
            f"Roles: {roles}",
        ]
    )


def content_hash(text: str) -> str:
    """Stable sha256 digest identifying one concept's embeddable content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- qdrant seam -------------------------------------------------------------


def _qdrant() -> QdrantClient:
    """Fresh, stateless client built from infra settings (never shared)."""
    kwargs = {"url": settings.qdrant_url}
    if settings.qdrant_api_key:
        kwargs["api_key"] = settings.qdrant_api_key
    return QdrantClient(**kwargs)


def _probe_dimension() -> int:
    """Vector width of the configured embedding model (via a probe text)."""
    vector = embed_texts([_DIM_PROBE_TEXT])[0]
    if not isinstance(vector, list) or not vector:
        raise RuntimeError("embedding probe returned an empty vector")
    return len(vector)


def _collection_size(client: QdrantClient, collection: str) -> int | None:
    info = client.get_collection(collection)
    vectors = getattr(getattr(info.config, "params", None), "vectors", None)
    return getattr(vectors, "size", None)


def _project_filter(project_id: str) -> Filter:
    return Filter(
        must=[
            FieldCondition(
                key="project_id", match=MatchValue(value=project_id)
            )
        ]
    )


def collection_for_project(project_id: str) -> str:
    """Resolve a collection in the selected provider's vector space."""
    if settings.embedding_provider != "cohere":
        return CONCEPT_COLLECTION
    from telemetry.registry import default_registry

    saved = default_registry().get_runtime_setting("cohere_embedding_generations") or {}
    domains = (saved.get("value") or {}).get("domains", {})
    if isinstance(domains, dict) and domains.get(project_id):
        return str(domains[project_id])
    return COHERE_CONCEPT_COLLECTION


def ensure_collection(
    collection: str | None = None,
) -> dict[str, Any]:
    """Create the semantic collection + project payload index if missing.

    The vector dimension is derived from a probe embedding so the collection
    always matches the configured model. An existing collection whose width
    differs is reported honestly (``ok: False``) — Qdrant cannot resize it.
    """
    try:
        if collection is None:
            collection = (
                COHERE_CONCEPT_COLLECTION
                if settings.embedding_provider == "cohere"
                else CONCEPT_COLLECTION
            )
        client = _qdrant()
        dimension = _probe_dimension()
        existing = [c.name for c in client.get_collections().collections]
        if collection in existing:
            current = _collection_size(client, collection)
            if current is not None and current != dimension:
                return {
                    "ok": False,
                    "count": 0,
                    "payload_index": False,
                    "error": (
                        "dimension mismatch: collection "
                        f"'{collection}' holds {current} dims but the "
                        f"embedding model produces {dimension}"
                    ),
                }
            count = int(getattr(client.get_collection(collection), "points_count", 0) or 0)
        else:
            client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(
                    size=dimension, distance=Distance.COSINE
                ),
            )
            count = 0
        payload_index = True
        try:
            client.create_payload_index(
                collection_name=collection,
                field_name="project_id",
                field_schema=PayloadSchemaType.KEYWORD,
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001 — index is an optimisation
            logger.warning(
                "payload index on %s.project_id unavailable: %s", collection, exc
            )
            payload_index = False
        return {
            "ok": True,
            "count": count,
            "payload_index": payload_index,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 — collection setup degrades
        logger.warning("ensure_collection(%s) failed: %s", collection, exc)
        return {"ok": False, "count": 0, "payload_index": False, "error": str(exc)[:200]}


# --- helpers shared by the idempotent paths ----------------------------------


def _empty_index_result(collection: str) -> dict[str, Any]:
    return {
        "ok": True,
        "collection": collection,
        "indexed": 0,
        "skipped_unmodified": 0,
        "failed": 0,
        "pending": 0,
        "error": None,
    }


def _valid_concepts(concepts: Any) -> list[dict[str, Any]]:
    if not isinstance(concepts, list):
        return []
    return [
        c
        for c in concepts
        if isinstance(c, dict) and c.get("id") and c.get("identity_key")
    ]


def _record_pending(
    store: ConceptStore,
    project_id: str,
    identity_key: str,
    content_hash_value: str,
    error: str,
) -> None:
    """Best-effort retry-state write; never raises into indexing callers."""
    try:
        store.mark_index_state(
            project_id,
            identity_key,
            content_hash_value,
            status=_PENDING,
            last_error=str(error)[:200],
        )
    except Exception as exc:  # noqa: BLE001 — state bookkeeping is best effort
        logger.warning(
            "marking index_state pending for %s failed: %s", identity_key, exc
        )


def _previous_states(
    store: ConceptStore, project_id: str
) -> dict[str, dict[str, Any]]:
    try:
        rows = store.index_state_rows(
            project_id=project_id, status=None, limit=_STATE_LOOKUP_LIMIT
        )
    except Exception as exc:  # noqa: BLE001 — skip check degrades to re-index
        logger.warning("reading index_state for %s failed: %s", project_id, exc)
        rows = []
    return {
        str(row.get("identity_key") or ""): row
        for row in rows
        if row.get("identity_key")
    }


def _point_payload(
    project_id: str,
    row: dict[str, Any],
    content_hash_value: str,
) -> dict[str, Any]:
    """Qdrant payload for one mirrored concept (deterministic keys)."""
    canonical_name = str(row.get("canonical_name") or "").strip()
    payload: dict[str, Any] = {
        "project_id": project_id,
        "concept_id": str(row.get("id")),
        "identity_key": str(row.get("identity_key") or ""),
        "concept_type": str(row.get("concept_type") or "concept"),
        "canonical_name": canonical_name,
        "qualified_name": str(row.get("qualified_name") or "").strip()
        or canonical_name,
        "normalized_name": str(row.get("normalized_name") or "").strip(),
        "aliases": _alias_names(row.get("aliases")),
        "roles": _role_names(row),
        "content_hash": content_hash_value,
    }
    source_files = _source_files(row)
    if source_files:
        payload["source_files"] = source_files
    return payload


# --- public entry points -----------------------------------------------------


def index_project_concepts(
    project_id: str,
    concepts: list[dict[str, Any]],
    collection: str | None = None,
    progress_callback=None,
    force_reembed: bool = False,
) -> dict[str, Any]:
    """Mirror canonical concept rows into the semantic collection.

    Callers pass the already-persisted rows (``ConceptStore.list_concepts``
    shape, optionally enriched with ``aliases``/``sources``). Rows whose
    ``content_hash`` already matches a previously ``done`` index state are
    skipped without re-embedding; failures are recorded ``pending`` with their
    error and never raise. When the collection cannot be guaranteed every row
    is recorded pending and the result reports ``ok: False``.
    """
    collection = collection or collection_for_project(project_id)
    rows = _valid_concepts(concepts)
    if not rows:
        return _empty_index_result(collection)
    rows = sorted(rows, key=lambda row: str(row.get("identity_key") or ""))

    try:
        ensured = ensure_collection(collection)
    except Exception as exc:  # noqa: BLE001 — collection setup must not raise
        logger.warning("ensure_collection raised: %s", exc)
        ensured = {"ok": False, "error": str(exc)[:200]}
    if not ensured.get("ok"):
        error = str(ensured.get("error") or "semantic collection unavailable")
        for row in rows:
            _record_pending(
                store=_store,
                project_id=project_id,
                identity_key=str(row["identity_key"]),
                content_hash_value=content_hash(embed_input(row)),
                error=error,
            )
        return {
            "ok": False,
            "collection": collection,
            "indexed": 0,
            "skipped_unmodified": 0,
            "failed": 0,
            "pending": len(rows),
            "error": error,
        }

    try:
        client = _qdrant()
    except Exception as exc:  # noqa: BLE001 — degraded path below
        error = f"qdrant client unavailable: {exc}"
        logger.warning("index_project_concepts(%s): %s", project_id, error)
        for row in rows:
            _record_pending(
                store=_store,
                project_id=project_id,
                identity_key=str(row["identity_key"]),
                content_hash_value=content_hash(embed_input(row)),
                error=error,
            )
        return {
            "ok": False,
            "collection": collection,
            "indexed": 0,
            "skipped_unmodified": 0,
            "failed": 0,
            "pending": len(rows),
            "error": error,
        }

    previous = _previous_states(_store, project_id)
    indexed = 0
    skipped_unmodified = 0
    failed = 0
    processed = 0

    for row in rows:
        identity_key = str(row["identity_key"])
        state_identity_key = (
            f"{identity_key}::cohere-v5"
            if settings.embedding_provider == "cohere" else identity_key
        )
        try:
            text = embed_input(row)
            hash_value = content_hash(text)
            prior = previous.get(state_identity_key) or {}
            if (not force_reembed and prior.get("status") == _DONE
                    and prior.get("content_hash") == hash_value):
                skipped_unmodified += 1
                processed += 1
                if progress_callback:
                    progress_callback(processed, len(rows))
                continue

            try:
                vector = embed_texts([text])[0]
            except Exception as exc:  # noqa: BLE001 — per-concept embed failure
                logger.warning(
                    "embedding domain concept %s failed: %s", identity_key, exc
                )
                _record_pending(_store, project_id, state_identity_key, hash_value, f"embed failed: {exc}")
                failed += 1
                processed += 1
                if progress_callback:
                    progress_callback(processed, len(rows))
                continue

            point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(row["id"])))
            try:
                client.upsert(
                    collection_name=collection,
                    wait=True,
                    points=[
                        PointStruct(
                            id=point_id,
                            vector=vector,
                            payload=_point_payload(project_id, row, hash_value),
                        )
                    ],
                )
            except Exception as exc:  # noqa: BLE001 — upsert failure is retried
                logger.warning(
                    "upserting domain concept %s failed: %s", identity_key, exc
                )
                _record_pending(
                    _store,
                    project_id,
                    identity_key,
                    hash_value,
                    f"upsert failed: {exc}",
                )
                failed += 1
                processed += 1
                if progress_callback:
                    progress_callback(processed, len(rows))
                continue

            try:
                _store.mark_index_state(
                    project_id, state_identity_key, hash_value, status=_DONE
                )
            except Exception as exc:  # noqa: BLE001 — state write is best effort
                logger.warning(
                    "marking index_state done for %s failed: %s", identity_key, exc
                )
            indexed += 1
            processed += 1
            if progress_callback:
                progress_callback(processed, len(rows))
        except Exception as exc:  # noqa: BLE001 — total: a bad row must not abort
            logger.warning("indexing domain concept %s failed: %s", identity_key, exc)
            _record_pending(
                _store, project_id, identity_key, "unknown", f"index failed: {exc}"
            )
            failed += 1
            processed += 1
            if progress_callback:
                progress_callback(processed, len(rows))

    return {
        "ok": True,
        "collection": collection,
        "indexed": indexed,
        "skipped_unmodified": skipped_unmodified,
        "failed": failed,
        "pending": 0,
        "error": None,
    }


def _scroll_project_point_ids(
    client: QdrantClient, project_id: str, collection: str
) -> tuple[list[Any], list[dict[str, Any]]]:
    """All Qdrant point ids + payloads for a project (bounded pagination)."""
    point_ids: list[Any] = []
    payloads: list[dict[str, Any]] = []
    offset: Any = None
    while len(point_ids) < _RECONCILE_POINT_CAP:
        records, offset = client.scroll(
            collection_name=collection,
            scroll_filter=_project_filter(project_id),
            limit=_SCROLL_PAGE_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for record in records:
            if len(point_ids) >= _RECONCILE_POINT_CAP:
                break
            point_ids.append(record.id)
            payloads.append(dict(record.payload or {}))
        if not records or offset is None:
            break
    return point_ids, payloads


def _delete_points(
    client: QdrantClient, collection: str, point_ids: list[Any]
) -> int:
    if not point_ids:
        return 0
    client.delete(
        collection_name=collection,
        points_selector=point_ids,
        wait=True,
    )
    return len(point_ids)


def reconcile_vectors(
    project_id: str,
    active_identity_keys: set[str],
    collection: str | None = None,
) -> dict[str, Any]:
    """Delete Qdrant points whose identity_key left the canonical set.

    Used by the full-project rebuild path: the canonical SQLite identity set is
    the source of truth and any semantic point for a concept no longer in it is
    stale. Never raises.
    """
    collection = collection or collection_for_project(project_id)
    try:
        client = _qdrant()
    except Exception as exc:  # noqa: BLE001 — reconcile degrades
        logger.warning("reconcile_vectors(%s) client failed: %s", project_id, exc)
        return {"ok": False, "deleted": 0, "error": f"qdrant client unavailable: {exc}"}
    try:
        point_ids, payloads = _scroll_project_point_ids(client, project_id, collection)
    except Exception as exc:  # noqa: BLE001 — reconcile degrades
        logger.warning("reconcile_vectors(%s) scroll failed: %s", project_id, exc)
        return {"ok": False, "deleted": 0, "error": str(exc)[:200]}

    stale = [
        point_id
        for point_id, payload in zip(point_ids, payloads)
        if str(payload.get("identity_key") or "") not in active_identity_keys
    ]
    try:
        deleted = _delete_points(client, collection, stale)
    except Exception as exc:  # noqa: BLE001 — reconcile degrades
        logger.warning("reconcile_vectors(%s) delete failed: %s", project_id, exc)
        return {"ok": False, "deleted": 0, "error": str(exc)[:200]}
    return {"ok": True, "deleted": deleted, "error": None}


def purge_project_vectors(
    project_id: str,
    collection: str | None = None,
) -> dict[str, Any]:
    """Delete every semantic point for a project (rebuild-from-zero)."""
    collection = collection or collection_for_project(project_id)
    try:
        client = _qdrant()
    except Exception as exc:  # noqa: BLE001 — purge degrades
        logger.warning("purge_project_vectors(%s) client failed: %s", project_id, exc)
        return {"ok": False, "deleted": 0, "error": f"qdrant client unavailable: {exc}"}
    try:
        point_ids = _scroll_project_point_ids(client, project_id, collection)[0]
    except Exception as exc:  # noqa: BLE001 — purge degrades
        logger.warning("purge_project_vectors(%s) scroll failed: %s", project_id, exc)
        return {"ok": False, "deleted": 0, "error": str(exc)[:200]}
    try:
        deleted = _delete_points(client, collection, point_ids)
    except Exception as exc:  # noqa: BLE001 — purge degrades
        logger.warning("purge_project_vectors(%s) delete failed: %s", project_id, exc)
        return {"ok": False, "deleted": 0, "error": str(exc)[:200]}
    return {"ok": True, "deleted": deleted, "error": None}
