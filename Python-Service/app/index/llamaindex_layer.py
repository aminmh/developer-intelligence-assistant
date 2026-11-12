"""LlamaIndex Property Graph layer: Neo4j graph + Qdrant vectors + hybrid retrieval.

This module ingests PHP code chunks (MCP `chunk_code` output) into:
  * Neo4j        — code symbols as `CodeSymbol` nodes, linked by `CALLS` edges.
  * Qdrant       — embeddable text (signature + code) keyed by the SAME
                   deterministic SHA-1 node id as the graph.

Retrieval is a hybrid vector + graph pipeline:
  1. vector search in Qdrant for semantically similar symbols,
  2. graph traversal to pull related evidence (one-hop neighbours, persisted
     knowledge links, and — when opted in — Tier-4 ``sym_*`` symbol edges),
  3. merge + dedup + rerank.

Graph expansion legs are additive: the default keeps the original direct
``CALLS`` neighbours only; richer legs (``has_knowledge``/``sym``) are enabled
by the caller through ``graph_legs``. Any graph failure degrades gracefully to
vector-only search.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from index.embeddings import EmbeddingProviderError, embed_texts, get_llamaindex_embed_model
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchText,
    MatchValue,
    PointStruct,
    PayloadSchemaType,
    VectorParams,
)

from infra.settings import settings
from infra.settings_store import settings_store
from infra.exception_logger import get_exception_logger
from telemetry.snapshot import Snapshot, identity_metadata, prepare_chunks, resolve_registered_revision
from telemetry.symbols import from_chunk, normalize as canonical_symbol

# Heavy raw attributes kept out of LLM-visible metadata (token bloat), but still
# available for relationship generation during ingestion.
EXCLUDED_LLM_METADATA_KEYS = ("calls", "callers", "project_id")

# Pseudo receivers emitted by the PHP extractor for unresolved/global calls.
# These have no concrete target node, so no CALLS edge is materialised for them.
_PSEUDO_RECEIVERS = ("__unknown", "__function", "__global__")

CODE_METRIC_FIELDS = (
    "metric_schema", "content_hash", "cyclomatic", "max_nesting", "param_count",
    "reads_properties", "writes_properties", "type_checks", "instantiates", "throws",
    "constructor_dependencies",
)
PAYLOAD_INDEX_FIELDS = ("revision_id", "node_type", "file_path", "embed_hash", "embed_profile")


def ensure_payload_indexes(qdrant: Any, collection_name: str) -> None:
    """Create keyword payload indexes used by filtered chunk scans; existing indexes are fine."""
    for field in PAYLOAD_INDEX_FIELDS:
        try:
            qdrant.create_payload_index(
                collection_name=collection_name,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
            )
        except Exception as exc:
            if "already exists" not in str(exc).lower():
                raise RuntimeError(
                    f"Could not create Qdrant payload index {field} on {collection_name}"
                ) from exc

# Named graph-expansion legs hybrid_retrieve can run after vector search. The
# default keeps ONLY CALLS so callers predating the richer legs observe the
# exact pre-Phase-2 results; has_knowledge/sym are explicit opt-ins.
_GRAPH_LEG_CALLS = "calls"
_GRAPH_LEG_HAS_KNOWLEDGE = "has_knowledge"
_GRAPH_LEG_SYM = "sym"
_KNOWN_GRAPH_LEGS = frozenset(
    {_GRAPH_LEG_CALLS, _GRAPH_LEG_HAS_KNOWLEDGE, _GRAPH_LEG_SYM}
)
_DEFAULT_GRAPH_LEGS = [_GRAPH_LEG_CALLS]

# Hard cap on evidence rows a single sym-leg expansion may add to results.
_SYM_MAX_EVIDENCE = 12

_MAX_CODE_CHARS = 2000
_MAX_TEXT_CHARS = 4000
# Qdrant rejects HTTP payloads larger than its configured request limit (32 MiB
# in the service image). Keep a conservative batch cap so large projects are
# ingested through several durable upserts instead of losing the whole index.
_QDRANT_UPSERT_MAX_POINTS = 64
_QDRANT_UPSERT_MAX_BYTES = 8 * 1024 * 1024
_QDRANT_READBACK_BATCH_SIZE = 512
_TRACE_LOCK = threading.Lock()
_TRACE_CACHE: Dict[str, Dict[str, Any]] = {}
_TRACE_CACHE_LIMIT = 200


def _trace_log_path() -> Any:
    from pathlib import Path

    path = Path(settings.log_dir) / "retrieval-traces.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def record_retrieval_trace(trace_id: str, trace: Dict[str, Any]) -> None:
    """Persist lightweight retrieval provenance without affecting RAG execution."""
    if not trace_id:
        return
    payload = dict(trace)
    payload["trace_id"] = trace_id
    payload.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    try:
        with _TRACE_LOCK:
            _TRACE_CACHE[trace_id] = payload
            while len(_TRACE_CACHE) > _TRACE_CACHE_LIMIT:
                _TRACE_CACHE.pop(next(iter(_TRACE_CACHE)))
            with _trace_log_path().open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        get_exception_logger().warning("Retrieval trace write failed: %s", exc)


def get_retrieval_trace(trace_id: str) -> Optional[Dict[str, Any]]:
    with _TRACE_LOCK:
        trace = _TRACE_CACHE.get(trace_id)
        if trace:
            return dict(trace)
        try:
            path = _trace_log_path()
            if not path.exists():
                return None
            for line in reversed(path.read_text(encoding="utf-8").splitlines()):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("trace_id") == trace_id:
                    _TRACE_CACHE[trace_id] = payload
                    return dict(payload)
        except Exception as exc:
            get_exception_logger().warning("Retrieval trace read failed: %s", exc)
        return None

_NODE_UPSERT_CYPHER = """
UNWIND $data AS row
MERGE (e:CodeSymbol {id: row.id})
SET e += row.properties
RETURN count(*) AS count
"""

_REL_UPSERT_CYPHER = """
UNWIND $data AS row
MATCH (s:CodeSymbol {id: row.source_id})
MATCH (t:CodeSymbol {id: row.target_id})
MERGE (s)-[r:CALLS]->(t)
SET r.collection = row.collection, r.origin = row.origin
RETURN count(*) AS count
"""

_STRUCTURE_REL_UPSERT_CYPHER = """
UNWIND $data AS row
MATCH (s:CodeSymbol {id: row.source_id})
MATCH (t:CodeSymbol {id: row.target_id})
MERGE (s)-[r:STRUCTURE {kind: row.kind, collection: row.collection}]->(t)
RETURN count(*) AS count
"""

_NEIGHBOURS_CYPHER = """
UNWIND $ids AS id
MATCH (n:CodeSymbol {id: id})-[r:CALLS]-(m:CodeSymbol)
WHERE $collection = '' OR r.collection IS NULL OR r.collection = $collection
RETURN n.id AS src, m.id AS tgt, type(r) AS rel,
       m.name AS name, m.class_fqn AS class_fqn, m.class AS class,
       m.namespace AS namespace, m.file AS file,
       m.start_line AS start_line, m.end_line AS end_line,
       m.type AS type, m.collection AS collection
"""

# HAS_KNOWLEDGE edges link a project's code chunks (chunk_code_<stem>) to the
# persisted-analysis knowledge nodes (proj_knowledge_<stem>) that describe the
# same file. The undirected match lets either endpoint be the vector hit; the
# linked node's payload is rehydrated from the OTHER collection by the caller.
_HAS_KNOWLEDGE_CYPHER = """
UNWIND $ids AS id
MATCH (n:CodeSymbol {id: id})-[r:HAS_KNOWLEDGE]-(m:CodeSymbol)
WHERE n.id <> m.id
RETURN n.id AS src, m.id AS tgt, type(r) AS rel,
       m.name AS name, m.class_fqn AS class_fqn, m.class AS class,
       m.namespace AS namespace, m.file AS file,
       m.start_line AS start_line, m.end_line AS end_line,
       m.type AS type, m.collection AS collection
"""

# Tier-4 sym leg: resolve vector-hit symbols to PhpSymbol nodes inside
# sym_<stem>, then return one-hop DECLARES/REFERENCES evidence rows. Every row
# is a real graph fact (node props + edge props) — never fabricated source.
_SYM_EVIDENCE_CYPHER = """
UNWIND $candidates AS candidate
MATCH (anchor:PhpSymbol {collection: $collection})
WHERE anchor.fqn = candidate OR anchor.name = candidate
WITH COLLECT(DISTINCT anchor.id) AS anchor_ids
UNWIND anchor_ids AS anchor_id
MATCH (a:PhpSymbol {collection: $collection})-[r:DECLARES|REFERENCES]-(e:PhpSymbol {collection: $collection})
WHERE a.id = anchor_id
RETURN a.id AS src, a.fqn AS src_fqn, a.kind AS src_kind,
       type(r) AS rel, r.kind AS rel_kind,
       r.file AS ref_file, r.line AS ref_line, r.to_member AS to_member,
       e.id AS id, e.kind AS kind, e.name AS name, e.fqn AS fqn,
       e.file AS file, e.line AS line, e.end_line AS end_line, e.owner AS owner
LIMIT $cap
"""


def node_id_for(class_fqn: Optional[str], name: Optional[str]) -> str:
    """Deterministic SHA-1 node id, idempotent across indexing runs."""
    fqn = (class_fqn or "__global__").strip() or "__global__"
    method = (name or "__unknown__").strip() or "__unknown__"
    return hashlib.sha1(f"{fqn}::{method}".encode("utf-8")).hexdigest()


def _point_id_for(node_id: str) -> str:
    """Deterministic Qdrant point id (UUID5) derived from the graph node id.

    Qdrant >= 1.15 requires point ids to be unsigned ints or UUIDs, so the raw
    SHA-1 hex string cannot be used directly as a point id.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, node_id))


# Collection that carries PERSISTED analysis knowledge (routes/tables/… facts
# saved analysis runs). Lives as a sibling of the project's ephemeral
# chunk_code_<stem> collection — never merged into it (re-indexing wipes
# chunk_code_* wholesale, knowledge must survive).
_KNOWLEDGE_PREFIX = "proj_knowledge_"
_CODE_PREFIX = "chunk_code_"
# Tier-4 inspection symbol graph (PhpSymbol nodes) — separate from the chunk
# graph; scoped by collection sym_<stem>.
_SYM_PREFIX = "sym_"


def knowledge_collection_for(stem: str) -> str:
    """proj_knowledge_<stem> for a project stem ('' safe)."""
    return f"{_KNOWLEDGE_PREFIX}{stem or 'unnamed'}"


def knowledge_sibling(collection_name: Optional[str]) -> Optional[str]:
    """Map a project's code collection to its knowledge sibling (or None)."""
    if not collection_name or not collection_name.startswith(_CODE_PREFIX):
        return None
    return f"{_KNOWLEDGE_PREFIX}{collection_name[len(_CODE_PREFIX):]}"


def _collection_stem(collection_name: Optional[str]) -> str:
    """Project stem embedded in a collection name ('' when not project-scoped).

    Strips the ``chunk_code_``/``proj_knowledge_``/``sym_`` prefixes so a
    pinned collection always maps back to the same project stem.
    """
    if not collection_name:
        return ""
    for prefix in (_CODE_PREFIX, _KNOWLEDGE_PREFIX, _SYM_PREFIX):
        if collection_name.startswith(prefix):
            return collection_name[len(prefix):].strip() or ""
    return ""


def _counterpart_collection(collection_name: Optional[str]) -> Optional[str]:
    """chunk_code_<stem> ↔ proj_knowledge_<stem> counterpart (either direction)."""
    if not collection_name:
        return None
    for prefix, other_prefix in (
        (_CODE_PREFIX, _KNOWLEDGE_PREFIX),
        (_KNOWLEDGE_PREFIX, _CODE_PREFIX),
    ):
        if collection_name.startswith(prefix):
            rest = collection_name[len(prefix):]
            if rest:
                return f"{other_prefix}{rest}"
    return None


def _sym_collection_for(stem: str) -> str:
    """sym_<stem> — mirrors sym_collection_exists's collection naming."""
    return f"{_SYM_PREFIX}{stem or 'unnamed'}"


def _has_knowledge_leg_active(
    collection_name: str, existing_collections: set[str]
) -> bool:
    """True when a searched collection may participate in the knowledge leg.

    A chunk collection qualifies only when its persisted-knowledge sibling
    actually exists (otherwise there is nothing to link to); a knowledge
    collection always qualifies for the reverse leg (its links point back at
    the project's code chunks).
    """
    if collection_name.startswith(_CODE_PREFIX):
        sibling = knowledge_sibling(collection_name)
        return bool(sibling and sibling in existing_collections)
    return collection_name.startswith(_KNOWLEDGE_PREFIX)


def _resolve_graph_legs(graph_legs: Optional[List[str]]) -> List[str]:
    """Normalise the caller's leg selection (None keeps the default CALLS leg).

    Fails fast on unknown leg names — a misspelled opt-in should never be
    silently ignored.
    """
    legs = list(_DEFAULT_GRAPH_LEGS) if graph_legs is None else list(graph_legs)
    unknown = sorted(set(legs) - set(_KNOWN_GRAPH_LEGS))
    if unknown:
        raise ValueError(
            "Unknown graph leg(s): " + ", ".join(unknown)
            + ". Accepted: " + ", ".join(sorted(_KNOWN_GRAPH_LEGS))
        )
    return legs


def _sym_candidates_from_hits(hit_payloads: List[Dict[str, Any]]) -> List[str]:
    """PHP symbol keys a vector-hit payload can resolve against sym_<stem>.

    Follows the PHP convention: class FQNs look like ``App\\Foo\\Bar`` and a
    member FQN is ``<class_fqn>::<name>``. A bare name (no class context) is
    kept for free functions / name-based resolution.
    """
    candidates: List[str] = []
    for payload in hit_payloads or []:
        fqn = str(payload.get("class_fqn") or "").strip().lstrip("\\")
        name = str(payload.get("name") or "").strip()
        if fqn and name:
            keys = (fqn, f"{fqn}::{name}")
        elif fqn:
            keys = (fqn,)
        elif name:
            keys = (name,)
        else:
            continue
        for key in keys:
            if key and key not in candidates:
                candidates.append(key)
    return candidates


# Module logger for the guarded existence probes below (failures degrade to
# empty/False — they must never raise into index-planning callers).
_LOGGER = logging.getLogger(__name__)


def qdrant_vector_search(client: Any, collection_name: str, vector: List[float], limit: int,
                         query_filter: Optional[Filter] = None) -> list:
    """Nearest-neighbour search across qdrant-client versions.

    qdrant-client 1.16 removed ``search()``; ``query_points()`` returns the same scored points.
    """
    extra = {"query_filter": query_filter} if query_filter is not None else {}
    if hasattr(client, "query_points"):
        return client.query_points(collection_name=collection_name, query=vector, limit=limit,
                                   with_payload=True, **extra).points
    return client.search(collection_name=collection_name, query_vector=vector, limit=limit, **extra)


def list_qdrant_collections() -> list[str]:
    """All Qdrant collection names (stateless, guarded: [] on any failure).

    Builds a throwaway client exactly like ``CodePropertyGraph._get_qdrant``
    (no instance state is held across calls).
    """
    try:
        kwargs = {"url": settings.qdrant_url}
        if settings.qdrant_api_key:
            kwargs["api_key"] = settings.qdrant_api_key
        client = QdrantClient(**kwargs)
        return [c.name for c in client.get_collections().collections]
    except Exception as exc:  # noqa: BLE001 — listing is a best-effort probe
        _LOGGER.warning("Qdrant collection listing failed: %s", exc)
        return []


def qdrant_collection_has_points(collection_name: str) -> bool:
    """True only when a collection exists and contains indexed points."""
    if not collection_name:
        return False
    try:
        kwargs = {"url": settings.qdrant_url}
        if settings.qdrant_api_key:
            kwargs["api_key"] = settings.qdrant_api_key
        client = QdrantClient(**kwargs)
        count = client.count(collection_name=collection_name, exact=True)
        return int(getattr(count, "count", 0) or 0) > 0
    except Exception as exc:  # noqa: BLE001 — readiness is a best-effort probe
        _LOGGER.warning(
            "Qdrant collection readiness probe failed for %s: %s",
            collection_name,
            exc,
        )
        return False


def sym_collection_exists(stem: str) -> bool:
    """True when the graph holds a PhpSymbol for ``sym_<stem>``.

    Mirrors ``analysis_service.link_knowledge_to_graph``: the driver session is
    taken from a fresh ``CodePropertyGraph`` store handle. Guarded — any
    failure (unreachable store, missing driver) reads as False.
    """
    try:
        from llama_index.graph_stores.neo4j import Neo4jPropertyGraphStore

        store = CodePropertyGraph()._get_graph_store()
        if store is None or not isinstance(store, Neo4jPropertyGraphStore):
            return False
        driver = getattr(store, "driver", None) or getattr(store, "_driver", None)
        if driver is None:
            return False
        collection = f"sym_{stem or 'unnamed'}"
        with driver.session() as session:
            result = session.run(
                "MATCH (s:PhpSymbol) WHERE s.collection = $collection "
                "RETURN count(s) AS n",
                collection=collection,
            )
            record = result.single()
            return bool(record and int(record["n"] or 0) > 0)
    except Exception as exc:  # noqa: BLE001 — existence probe is best-effort
        _LOGGER.warning("Sym graph existence probe failed for %s: %s", stem, exc)
        return False


def _flatten_params(parameters: Any) -> str:
    if not isinstance(parameters, list):
        return ""
    parts: List[str] = []
    for p in parameters:
        if isinstance(p, dict):
            p_type = p.get("type") or ""
            p_name = p.get("name") or ""
            parts.append(f"{p_type} {p_name}".strip())
        else:
            parts.append(str(p))
    return ", ".join(parts)


def _flatten_call_values(calls: Any) -> List[str]:
    """Normalize legacy call maps/lists into ``Receiver::method`` strings."""
    if isinstance(calls, list):
        return [str(item) for item in calls if isinstance(item, (str, int, float))]
    if not isinstance(calls, dict):
        return []
    values: List[str] = []
    for owner, methods in calls.items():
        if isinstance(methods, list):
            values.extend(f"{owner}::{method}" for method in methods)
        elif isinstance(methods, str):
            values.append(f"{owner}::{methods}")
    return list(dict.fromkeys(values))


def _chunk_node_id(chunk: Dict[str, Any]) -> str:
    if chunk.get("storage_id"):
        return chunk["storage_id"]
    if (chunk.get("node_type") or chunk.get("type")) in ("closure", "arrow_function"):
        # Closure chunks are new in Phase 0 and have no legacy persisted IDs.
        # Using the old class/name hash would collapse every {closure}.
        return hashlib.sha1(from_chunk(chunk).encode("utf-8")).hexdigest()
    fqn = chunk.get("class_fqn") or chunk.get("class")
    name = chunk.get("name")
    return node_id_for(fqn, name)


def enrich_chunks_with_symbol_index(
    chunks: List[dict],
    definitions: List[Dict[str, Any]],
    references: List[Dict[str, Any]],
) -> List[dict]:
    """Attach precise exporter facts to their owning AST chunks.

    The exporter uses ``scope`` for the definition that owns a reference. A
    chunk keeps its original shape and source code; only normalized retrieval
    metadata is added. Unresolved rows are retained as evidence metadata and
    are never converted into graph edges here.
    """
    if not definitions and not references:
        return chunks

    definitions_by_fqn: Dict[str, Dict[str, Any]] = {}
    for definition in definitions:
        if not isinstance(definition, dict):
            continue
        fqn = str(definition.get("fqn") or "").strip().lstrip("\\")
        if fqn and fqn not in definitions_by_fqn:
            definitions_by_fqn[fqn] = definition

    references_by_scope: Dict[str, List[Dict[str, Any]]] = {}
    for reference in references:
        if not isinstance(reference, dict):
            continue
        scope = str(reference.get("scope") or "").strip().lstrip("\\")
        if scope:
            references_by_scope.setdefault(scope, []).append(reference)

    enriched: List[dict] = []
    for original in chunks:
        if not isinstance(original, dict):
            continue
        chunk = dict(original)
        identity = str(
            chunk.get("fq_symbol_name")
            or chunk.get("class_fqn")
            or chunk.get("name")
            or ""
        ).strip().lstrip("\\")
        definition = definitions_by_fqn.get(identity)
        if definition:
            chunk["symbol_identity"] = identity
            chunk["definition_kind"] = definition.get("kind")

        scoped_refs = references_by_scope.get(identity, [])
        referenced = list(chunk.get("referenced_symbols") or [])
        reference_kinds = set(chunk.get("reference_kinds") or [])
        evidence = list(chunk.get("reference_evidence") or [])
        unresolved = int(chunk.get("unresolved_reference_count") or 0)
        for reference in scoped_refs:
            kind = str(reference.get("kind") or "").strip()
            if kind:
                reference_kinds.add(kind)
            if reference.get("unresolved"):
                unresolved += 1
            to_fqn = str(reference.get("to_fqn") or "").strip().lstrip("\\")
            member = str(reference.get("to_member") or "").strip()
            target = f"{to_fqn}::{member}" if to_fqn and member else to_fqn
            if target and target not in referenced:
                referenced.append(target)
            evidence.append(
                {
                    "kind": kind,
                    "file": reference.get("file"),
                    "line": reference.get("line"),
                    "to_fqn": reference.get("to_fqn"),
                    "to_member": reference.get("to_member"),
                    "unresolved": bool(reference.get("unresolved")),
                    "via": reference.get("via"),
                }
            )

        chunk["referenced_symbols"] = referenced
        chunk["reference_kinds"] = sorted(reference_kinds)
        chunk["reference_evidence"] = evidence
        chunk["unresolved_reference_count"] = unresolved
        enriched.append(chunk)

    return enriched


# Bump when build_embeddable_text changes what it produces: vectors made from the
# old text must not be reused for the new one.
EMBEDDABLE_TEXT_FORMAT = "1"
# How many of a project's newest ready revisions a new revision may copy vectors from.
REUSE_REVISION_LIMIT = 3


def embed_profile_key() -> str:
    """Identity of the vector space: provider, model, dimensions, input type, and text format.

    Vectors are reused only between points with the same key, so a provider, model,
    or embeddable-text change can never mix vectors from different spaces.
    """
    from index.embeddings import embedding_profile

    profile = {**embedding_profile("search_document"), "input_type": "search_document",
               "text_format": EMBEDDABLE_TEXT_FORMAT, "max_code_chars": _MAX_CODE_CHARS,
               "max_text_chars": _MAX_TEXT_CHARS}
    raw = json.dumps(profile, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def embed_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_embeddable_text(chunk: Dict[str, Any]) -> str:
    """Build the LLM-facing text for a chunk (mirrors CodeChunk.toEmbeddableText)."""
    parts: List[str] = []
    if chunk.get("file_path") or chunk.get("file"):
        parts.append(f"File: {chunk.get('file_path') or chunk.get('file')}")
    if chunk.get("namespace"):
        parts.append(f"Namespace: {chunk['namespace']}")
    docblock = chunk.get("docblock")
    if docblock:
        parts.append(str(docblock))
    if chunk.get("class"):
        parts.append(f"Class: {chunk['class']}")
    if chunk.get("method_name"):
        parts.append(f"Method: {chunk['method_name']}")
    elif chunk.get("function_name"):
        parts.append(f"Function: {chunk['function_name']}")
    if chunk.get("name"):
        sig = [str(chunk["name"])]
        params = chunk.get("parameters", [])
        if params:
            sig.append("(" + _flatten_params(params) + ")")
        if chunk.get("return_type"):
            sig.append(": " + str(chunk["return_type"]))
        parts.append("Signature: " + "".join(sig))
    if chunk.get("signature") and not any(part.startswith("Signature:") for part in parts):
        parts.append(f"Signature: {chunk['signature']}")
    if chunk.get("summary"):
        parts.append(f"Summary: {chunk['summary']}")
    calls = chunk.get("called_symbols") or _flatten_call_values(chunk.get("calls"))
    if calls:
        parts.append("Calls: " + ", ".join(calls))
    references = chunk.get("referenced_symbols") or []
    if references:
        parts.append("References: " + ", ".join(str(item) for item in references))

    code = chunk.get("code", "")
    if code:
        code = str(code)
        if len(code) > _MAX_CODE_CHARS:
            code = code[:_MAX_CODE_CHARS] + "\n..."
        parts.append(code)

    text = "\n".join(parts) if parts else str(chunk.get("name") or "unnamed")
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS] + "\n..."
    return text


_ORIGIN_BY_LANGUAGE = {"javascript": "tree-sitter", "typescript": "tree-sitter", "python": "python-ast"}


def _edge_origin(chunk: Dict[str, Any]) -> str:
    """Which extractor found a call edge: the chunk's own origin, else by language (PHP parser by default)."""
    return str(chunk.get("origin") or _ORIGIN_BY_LANGUAGE.get(str(chunk.get("language") or ""), "php-parser"))


def _strip_none(mapping: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in mapping.items() if v is not None}


def _graph_properties(chunk: Dict[str, Any], collection_name: str) -> Dict[str, Any]:
    """Graph node properties — structure only, no code body."""
    return _strip_none(
        {
            **identity_metadata(chunk),
            "schema_version": chunk.get("schema_version", "ast-chunk-v1"),
            "chunk_id": chunk.get("chunk_id"),
            "language": chunk.get("language", "php"),
            "node_type": chunk.get("node_type") or chunk.get("type", ""),
            "symbol_name": chunk.get("symbol_name") or chunk.get("name", ""),
            "fq_symbol_name": chunk.get("fq_symbol_name") or chunk.get("class_fqn") or chunk.get("name", ""),
            "file_path": chunk.get("file_path") or chunk.get("file", ""),
            "name": chunk.get("name", ""),
            "method_name": chunk.get("method_name"),
            "class": chunk.get("class", ""),
            "class_fqn": chunk.get("class_fqn", ""),
            "namespace": chunk.get("namespace", ""),
            "file": chunk.get("file", ""),
            "type": chunk.get("type", ""),
            "start_line": chunk.get("start_line", 0),
            "end_line": chunk.get("end_line", 0),
            "return_type": chunk.get("return_type", ""),
            "modifiers": chunk.get("modifiers", []) or [],
            "parameters": _flatten_params(chunk.get("parameters")),
            "signature": chunk.get("signature", ""),
            "parent_symbol": chunk.get("parent_symbol"),
            "imports": chunk.get("imports", []) or [],
            "extends": chunk.get("extends", []) or [],
            "implements": chunk.get("implements", []) or [],
            "traits": chunk.get("traits", []) or [],
            "visibility": chunk.get("visibility"),
            "is_static": chunk.get("is_static", False),
            "loc": chunk.get("loc", max(0, int(chunk.get("end_line", 0) or 0) - int(chunk.get("start_line", 0) or 0) + 1)),
            "summary": chunk.get("summary", ""),
            "symbol_identity": chunk.get("symbol_identity"),
            "definition_kind": chunk.get("definition_kind"),
            "reference_kinds": chunk.get("reference_kinds", []) or [],
            "unresolved_reference_count": chunk.get("unresolved_reference_count", 0),
            "collection": collection_name,
        }
    )


def _payload(chunk: Dict[str, Any], collection_name: str) -> Dict[str, Any]:
    """Qdrant point payload — full retrieval context including code body."""
    return _strip_none(
        {
            **identity_metadata(chunk),
            "schema_version": chunk.get("schema_version", "ast-chunk-v1"),
            "chunk_id": chunk.get("chunk_id"),
            "language": chunk.get("language", "php"),
            "node_type": chunk.get("node_type") or chunk.get("type", ""),
            "symbol_name": chunk.get("symbol_name") or chunk.get("name", ""),
            "fq_symbol_name": chunk.get("fq_symbol_name") or chunk.get("class_fqn") or chunk.get("name", ""),
            "file_path": chunk.get("file_path") or chunk.get("file", ""),
            "node_id": _chunk_node_id(chunk),
            "type": chunk.get("type", ""),
            "name": chunk.get("name", ""),
            "method_name": chunk.get("method_name"),
            "class": chunk.get("class", ""),
            "class_fqn": chunk.get("class_fqn", ""),
            "namespace": chunk.get("namespace", ""),
            "file": chunk.get("file", ""),
            "start_line": chunk.get("start_line", 0),
            "end_line": chunk.get("end_line", 0),
            "modifiers": chunk.get("modifiers", []) or [],
            "parameters": _flatten_params(chunk.get("parameters")),
            "signature": chunk.get("signature", ""),
            "parent_symbol": chunk.get("parent_symbol"),
            "imports": chunk.get("imports", []) or [],
            "extends": chunk.get("extends", []) or [],
            "implements": chunk.get("implements", []) or [],
            "traits": chunk.get("traits", []) or [],
            "visibility": chunk.get("visibility"),
            "is_static": chunk.get("is_static", False),
            "loc": chunk.get("loc", max(0, int(chunk.get("end_line", 0) or 0) - int(chunk.get("start_line", 0) or 0) + 1)),
            "called_symbols": chunk.get("called_symbols") or _flatten_call_values(chunk.get("calls")),
            "referenced_symbols": chunk.get("referenced_symbols", []) or [],
            "reference_kinds": chunk.get("reference_kinds", []) or [],
            "reference_evidence": chunk.get("reference_evidence", []) or [],
            "unresolved_reference_count": chunk.get("unresolved_reference_count", 0),
            "symbol_identity": chunk.get("symbol_identity"),
            "definition_kind": chunk.get("definition_kind"),
            "literals": chunk.get("literals", []) or [],
            "annotations": chunk.get("annotations", []) or [],
            "framework_hints": chunk.get("framework_hints", []) or [],
            "domain_tags": chunk.get("domain_tags", []) or [],
            "low_information": chunk.get("low_information", False),
            "summary": chunk.get("summary", ""),
            "return_type": chunk.get("return_type", ""),
            "has_docblock": chunk.get("has_docblock", False),
            "docblock": chunk.get("docblock", ""),
            "docstring": chunk.get("docstring") or chunk.get("docblock", ""),
            "code": chunk.get("code", ""),
            "calls": chunk.get("calls", []) or [],
            "collection": collection_name,
            **{field: chunk[field] for field in CODE_METRIC_FIELDS if chunk.get(field) is not None},
        }
    )


def _build_query_filter(
    class_name: Optional[str] = None,
    file: Optional[str] = None,
) -> Optional[Filter]:
    """Build a Qdrant metadata filter from extracted intent entities."""
    conditions = []
    if class_name:
        conditions.append(
            FieldCondition(key="class", match=MatchValue(value=class_name))
        )
    if file:
        conditions.append(FieldCondition(key="file", match=MatchText(text=file)))
    if not conditions:
        return None
    return Filter(must=conditions)


class CodePropertyGraph:
    """Neo4j-backed property graph with Qdrant vectors and hybrid retrieval."""

    def __init__(self) -> None:
        self._graph_store: Any = None
        self._graph_store_failed = False
        self._qdrant: Optional[QdrantClient] = None
        self._qdrant_revision = -1

    # --- connection helpers -------------------------------------------------

    def _get_graph_store(self) -> Any:
        if self._graph_store is None and not self._graph_store_failed:
            try:
                from llama_index.graph_stores.neo4j import Neo4jPropertyGraphStore

                self._graph_store = Neo4jPropertyGraphStore(
                    username=settings.neo4j_user,
                    password=settings.neo4j_password,
                    url=settings.neo4j_url,
                    database=settings.neo4j_database,
                    refresh_schema=False,
                    create_indexes=False,
                    sanitize_query_output=False,
                )
            except Exception as exc:
                get_exception_logger().error(
                    "Failed to connect to Neo4j graph store: %s", exc
                )
                self._graph_store_failed = True
                self._graph_store = None
        return self._graph_store

    def _get_qdrant(self) -> QdrantClient:
        revision = settings_store.revision
        if self._qdrant is None or self._qdrant_revision != revision:
            kwargs = {"url": settings.qdrant_url}
            if settings.qdrant_api_key:
                kwargs["api_key"] = settings.qdrant_api_key
            self._qdrant = QdrantClient(**kwargs)
            self._qdrant_revision = revision
        return self._qdrant

    def _get_embed_model(self):
        """Embedding model for the configured server (Ollama or OpenAI-compatible)."""
        return get_llamaindex_embed_model()

    # --- ingestion ----------------------------------------------------------

    def ingest(
        self,
        chunks: List[dict],
        collection_name: str,
        symbol_definitions: Optional[List[Dict[str, Any]]] = None,
        symbol_references: Optional[List[Dict[str, Any]]] = None,
        snapshot: Snapshot | None = None,
        progress_callback=None,
        vector_collection_name: str | None = None,
        force_reindex: bool = False,
        reuse: bool = True,
    ) -> Dict[str, Any]:
        """Ingest code chunks into the Neo4j graph and Qdrant vectors.

        ``symbol_definitions`` and ``symbol_references`` are optional facts
        from the precise PHP exporter. They enrich chunk payloads without
        taking ownership of source code or embeddings away from AST chunks.
        """
        if not isinstance(chunks, list):
            chunks = []
        if snapshot is None and collection_name.startswith("snapshot_"):
            raise ValueError("Snapshot collections require explicit snapshot indexing")

        chunks = enrich_chunks_with_symbol_index(
            chunks,
            symbol_definitions or [],
            symbol_references or [],
        )
        if snapshot is not None:
            if collection_name != snapshot.collection:
                raise ValueError("Snapshot collection does not match its scope")
            snapshot = resolve_registered_revision(snapshot)
            chunks = prepare_chunks(chunks, snapshot)

        # Dedup by deterministic node id (idempotent re-runs).
        unique: Dict[str, Dict[str, Any]] = {}
        for c in chunks:
            if not isinstance(c, dict):
                continue
            nid = _chunk_node_id(c)
            if nid in unique and (c.get("node_type") or c.get("type")) in ("closure", "arrow_function"):
                raise ValueError("Ambiguous duplicate closure identity")
            unique.setdefault(nid, c)
        deduped = list(unique.values())

        if not deduped:
            raise ValueError("Project index has no indexable source chunks")

        persisted_points: Optional[int] = None
        if snapshot is not None:
            if not deduped:
                raise ValueError("Snapshot has no indexable source")
            graph_nodes, graph_relations = self._ingest_graph(deduped, collection_name, strict=True)
            ingested, embed_fails = self._ingest_vectors(
                deduped, vector_collection_name or collection_name,
                replace=force_reindex, progress_callback=progress_callback,
                reuse_from=self._reuse_sources(snapshot) if reuse else None,
            )
            if graph_nodes != len(deduped) or ingested != len(deduped) or embed_fails:
                raise RuntimeError(
                    "Snapshot persistence is incomplete "
                    f"(chunks={len(deduped)}, graph_nodes={graph_nodes}, "
                    f"vectors={ingested}, embedding_failures={embed_fails}, "
                    f"embedding_error={getattr(self, '_last_embedding_error', None) or 'unknown'})"
                )
            vector_collection = vector_collection_name or collection_name
            try:
                persisted_points = self._get_qdrant().count(
                    collection_name=vector_collection,
                    exact=True,
                ).count
                expected_identity = identity_metadata(snapshot.metadata())
                point_ids = [
                    _point_id_for(_chunk_node_id(chunk)) for chunk in deduped
                ]
                retrieved_ids: set[str] = set()
                qdrant = self._get_qdrant()
                for offset in range(0, len(point_ids), _QDRANT_READBACK_BATCH_SIZE):
                    records = qdrant.retrieve(
                        collection_name=vector_collection,
                        ids=point_ids[offset:offset + _QDRANT_READBACK_BATCH_SIZE],
                        with_payload=list(expected_identity),
                        with_vectors=False,
                    )
                    for record in records:
                        payload = record.payload or {}
                        if any(payload.get(key) != value for key, value in expected_identity.items()):
                            continue
                        retrieved_ids.add(str(record.id))
            except Exception as exc:
                get_exception_logger().warning(
                    "Snapshot vector read-back failed for project=%s revision=%s error_type=%s",
                    snapshot.project_id,
                    snapshot.revision_id,
                    type(exc).__name__,
                )
                raise RuntimeError("Snapshot vector index verification failed") from exc
            if persisted_points != len(deduped) or len(retrieved_ids) != len(point_ids):
                raise RuntimeError(
                    "Snapshot vector index verification failed "
                    f"(expected={len(deduped)}, persisted={persisted_points}, "
                    f"retrieved={len(retrieved_ids)})"
                )
        else:
            graph_nodes, graph_relations = self._ingest_graph(
                deduped, collection_name, strict=True
            )
            if graph_nodes != len(deduped):
                raise RuntimeError(
                    "Project graph persistence is incomplete "
                    f"(chunks={len(deduped)}, graph_nodes={graph_nodes})"
                )
            ingested, embed_fails = self._ingest_vectors(
                deduped, vector_collection_name or collection_name,
                progress_callback=progress_callback,
            )

        # Refresh the typo-correction domain dictionary so newly indexed
        # class/method names become correctable (best-effort).
        if ingested and snapshot is None:
            try:
                from rag.query_normalizer import regenerate_domain_dictionary

                regenerate_domain_dictionary(settings.qdrant_url)
            except Exception:
                pass

        return {
            "collection_name": collection_name,
            "ingested": ingested,
            "chunk_count": len(deduped),
            "graph_nodes": graph_nodes,
            "graph_relations": graph_relations,
            "embed_fails": embed_fails,
            "vector_point_count": persisted_points,
            "embedding_error": getattr(self, "_last_embedding_error", None),
            **getattr(self, "_last_vector_stats", {"embedded": 0, "reused": 0}),
            "symbol_definitions": len(symbol_definitions or []),
            "symbol_references": len(symbol_references or []),
        }

    def _ingest_graph(
        self, chunks: List[Dict[str, Any]], collection_name: str, *, strict: bool = False
    ) -> tuple[int, int]:
        store = self._get_graph_store()
        if store is None:
            if strict:
                raise RuntimeError("Neo4j graph store unavailable")
            return 0, 0

        known_ids = {_chunk_node_id(c) for c in chunks}

        node_rows = [
            {
                "id": _chunk_node_id(c),
                "properties": _graph_properties(c, collection_name),
            }
            for c in chunks
        ]

        # Resolve CALLS edges: only materialise relations whose target symbol
        # actually exists in this batch (no dangling Neo4j nodes).
        rel_rows: List[Dict[str, str]] = []
        structure_rows: List[Dict[str, str]] = []
        seen_rels: set[tuple[str, str]] = set()
        symbol_ids = {
            str(c.get("fq_symbol_name") or c.get("class_fqn") or c.get("name") or ""): _chunk_node_id(c)
            for c in chunks
        }
        canonical_ids = {}
        for chunk in chunks:
            symbol_id = chunk.get("symbol_id")
            if not symbol_id:
                try:
                    symbol_id = from_chunk(chunk)
                except ValueError:
                    symbol_id = None
            if symbol_id:
                canonical_ids[str(symbol_id)] = _chunk_node_id(chunk)
        for c in chunks:
            src = _chunk_node_id(c)
            for target in _flatten_call_values(c.get("calls")):
                if strict:
                    try:
                        if "::" in target:
                            owner, method = target.split("::", 1)
                            kind = "function" if owner in ("__function", "__global__") else "method"
                            symbol = canonical_symbol({"kind": kind, "class": owner,
                                                       "method": method, "name": method})
                        else:
                            symbol = canonical_symbol({"kind": "function", "name": target})
                    except ValueError:
                        continue
                    dst = canonical_ids.get(symbol)
                    if dst is not None and (src, dst) not in seen_rels:
                        seen_rels.add((src, dst))
                        rel_rows.append({"source_id": src, "target_id": dst,
                                         "label": "CALLS", "collection": collection_name,
                                         "origin": _edge_origin(c)})
                    continue
                if not isinstance(target, str) or "::" not in target:
                    continue
                t_fqn, t_method = target.split("::", 1)
                if t_fqn in _PSEUDO_RECEIVERS:
                    continue
                dst = node_id_for(t_fqn, t_method)
                if dst not in known_ids or dst == src:
                    continue
                if (src, dst) in seen_rels:
                    continue
                seen_rels.add((src, dst))
                rel_rows.append(
                    {
                        "source_id": src,
                        "target_id": dst,
                        "label": "CALLS",
                        # Stored on the edge so retrieval can scope neighbour
                        # traversal to the caller's project collection (legacy
                        # edges without the prop keep matching via IS NULL).
                        "collection": collection_name,
                        "origin": _edge_origin(c),
                    }
                )

            parent = c.get("parent_symbol")
            if parent:
                parent_name = str(parent).rsplit("\\", 1)[-1]
                parent_id = symbol_ids.get(str(parent)) or node_id_for(str(parent), parent_name)
                if parent_id in known_ids:
                    structure_rows.append({"source_id": parent_id, "target_id": src, "kind": "contains", "collection": collection_name})

            for field, kind in (("extends", "extends"), ("implements", "implements"), ("traits", "uses_trait")):
                for target in c.get(field, []) or []:
                    target_name = str(target).rsplit("\\", 1)[-1]
                    target_id = symbol_ids.get(str(target)) or node_id_for(str(target), target_name)
                    if target_id in known_ids and target_id != src:
                        structure_rows.append({"source_id": src, "target_id": target_id, "kind": kind, "collection": collection_name})

        try:
            result = store.structured_query(_NODE_UPSERT_CYPHER, param_map={"data": node_rows})
            if strict and (not result or result[0].get("count") != len(node_rows)):
                raise RuntimeError("Snapshot graph node count mismatch")
        except Exception as exc:
            if strict:
                raise
            get_exception_logger().error(
                "Neo4j node upsert failed for collection %s: %s",
                collection_name,
                exc,
            )
            return 0, 0

        if rel_rows:
            try:
                result = store.structured_query(
                    _REL_UPSERT_CYPHER, param_map={"data": rel_rows}
                )
                if strict and (not result or result[0].get("count") != len(rel_rows)):
                    raise RuntimeError("Snapshot graph call count mismatch")
            except Exception as exc:
                if strict:
                    raise
                get_exception_logger().error(
                    "Neo4j relation upsert failed for collection %s: %s",
                    collection_name,
                    exc,
                )
                return len(node_rows), 0

        if structure_rows:
            try:
                result = store.structured_query(
                    _STRUCTURE_REL_UPSERT_CYPHER,
                    param_map={"data": structure_rows},
                )
                if strict and (not result or result[0].get("count") != len(structure_rows)):
                    raise RuntimeError("Snapshot graph structure count mismatch")
            except Exception as exc:
                if strict:
                    raise
                get_exception_logger().error(
                    "Neo4j structure relation upsert failed for collection %s: %s",
                    collection_name,
                    exc,
                )

        return len(node_rows), len(rel_rows) + len(structure_rows)

    @staticmethod
    def _reusable_vectors(qdrant: Any, collections: List[str], profile: str, wanted: set,
                          target: str) -> Dict[str, List[float]]:
        """Vectors by embed hash from earlier revisions' collections, for this profile only. Best-effort."""
        from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

        found: Dict[str, List[float]] = {}
        for source in collections:
            remaining = sorted(wanted - set(found))
            if not remaining:
                break
            if source == target:
                continue
            try:
                for offset in range(0, len(remaining), 256):
                    batch = remaining[offset:offset + 256]
                    records, _ = qdrant.scroll(
                        collection_name=source,
                        scroll_filter=Filter(must=[
                            FieldCondition(key="embed_profile", match=MatchValue(value=profile)),
                            FieldCondition(key="embed_hash", match=MatchAny(any=batch)),
                        ]),
                        limit=len(batch) * 4, with_payload=["embed_hash"], with_vectors=True,
                    )
                    for record in records:
                        key = (record.payload or {}).get("embed_hash")
                        vector = record.vector
                        if isinstance(vector, dict):  # named vectors are not used here
                            continue
                        if isinstance(key, str) and key in wanted and vector and key not in found:
                            found[key] = list(vector)
            except Exception as exc:  # a missing or old collection only costs a re-embed
                get_exception_logger().warning("Vector reuse lookup skipped for %s: %s", source, type(exc).__name__)
        if found:
            sizes = {len(vector) for vector in found.values()}
            if len(sizes) != 1:  # mixed dimensions would mean a profile collision; never mix them
                return {}
        return found

    def _reuse_sources(self, snapshot: Snapshot) -> List[str]:
        """Vector collections of the project's newest ready revisions, excluding this one."""
        try:
            from index.embedding_rebuild import active_vector_collection
            from telemetry.registry import default_registry

            sources = []
            for revision in default_registry().project_revisions(snapshot.project_id):
                if revision.get("state") != "ready" or revision.get("revision_id") == snapshot.revision_id:
                    continue
                collection = revision.get("collection_name")
                active = active_vector_collection(collection) if collection else None
                if active:
                    sources.append(active)
                if len(sources) >= REUSE_REVISION_LIMIT:
                    break
            return sources
        except Exception as exc:  # reuse is an optimization; indexing proceeds without it
            get_exception_logger().warning("Vector reuse sources unavailable: %s", type(exc).__name__)
            return []

    def _ingest_vectors(
        self, chunks: List[Dict[str, Any]], collection_name: str, *, replace: bool = True,
        progress_callback=None, reuse_from: Optional[List[str]] = None,
    ) -> tuple[int, int]:
        """Embed and upsert chunk vectors; ``reuse_from`` lists collections to copy unchanged vectors from.

        A chunk whose embeddable text and embedding profile match a point in one of those
        collections gets that point's vector instead of a provider call.
        """
        qdrant = self._get_qdrant()
        self._last_embedding_error: str | None = None
        self._last_vector_stats = {"embedded": 0, "reused": 0}

        if replace:
            try:
                qdrant.delete_collection(collection_name)
            except Exception:
                pass

        points: List[PointStruct] = []
        embed_fails = 0
        vector_size: Optional[int] = None

        embeddable = [(chunk, build_embeddable_text(chunk)) for chunk in chunks]
        embeddable = [(chunk, text) for chunk, text in embeddable if text.strip()]
        profile = embed_profile_key()
        hashes = {id(chunk): embed_hash(text) for chunk, text in embeddable}
        reused = self._reusable_vectors(qdrant, reuse_from or [], profile, set(hashes.values()), collection_name)

        def add_point(chunk: Dict[str, Any], embedding: List[float]) -> None:
            nonlocal vector_size
            if vector_size is None:
                vector_size = len(embedding)
                try:
                    qdrant.create_collection(
                        collection_name=collection_name,
                        vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
                    )
                except Exception:
                    pass
                ensure_payload_indexes(qdrant, collection_name)
            points.append(PointStruct(
                id=_point_id_for(_chunk_node_id(chunk)),
                vector=embedding,
                payload={**_payload(chunk, collection_name), "embed_hash": hashes[id(chunk)],
                         "embed_profile": profile},
            ))

        missing = []
        for chunk, text in embeddable:
            vector = reused.get(hashes[id(chunk)])
            if vector is not None:
                add_point(chunk, vector)
            else:
                missing.append((chunk, text))
        self._last_vector_stats["reused"] = len(embeddable) - len(missing)
        if progress_callback and len(missing) < len(embeddable):
            progress_callback("embedding", len(embeddable) - len(missing), len(embeddable))
        completed = len(embeddable) - len(missing)
        embeddable_total = len(embeddable)
        embeddable = missing
        batch_size = 96
        for offset in range(0, len(embeddable), batch_size):
            batch = embeddable[offset:offset + batch_size]

            def report_embedding_progress(amount: int) -> None:
                nonlocal completed
                completed += amount
                if progress_callback:
                    progress_callback("embedding", completed, embeddable_total)

            if progress_callback:
                # Outside the try: a callback may stop ingest (e.g. a superseded working tree).
                progress_callback("embedding", completed, embeddable_total)
            try:
                vectors = embed_texts(
                    [text for _, text in batch],
                    input_type="search_document",
                    progress=report_embedding_progress,
                )
                if len(vectors) != len(batch):
                    raise RuntimeError("Embedding provider returned an incomplete batch")
            except EmbeddingProviderError:
                raise
            except Exception as exc:
                embed_fails += len(batch)
                if self._last_embedding_error is None:
                    status = getattr(exc, "code", None)
                    self._last_embedding_error = (
                        f"{type(exc).__name__}:{status}"
                        if isinstance(status, int)
                        else type(exc).__name__
                    )
                continue

            for (chunk, _), embedding in zip(batch, vectors):
                add_point(chunk, embedding)
                self._last_vector_stats["embedded"] += 1

        if progress_callback:
            progress_callback("upserting", completed, embeddable_total)

        if points:
            ingested = 0
            batch: List[PointStruct] = []
            batch_bytes = 0

            def flush_batch() -> bool:
                nonlocal ingested, batch, batch_bytes
                if not batch:
                    return True
                try:
                    qdrant.upsert(collection_name=collection_name, wait=True, points=batch)
                except Exception as exc:
                    get_exception_logger().error(
                        "Qdrant upsert failed for collection %s (batch_size=%s): %s",
                        collection_name,
                        len(batch),
                        exc,
                    )
                    return False
                ingested += len(batch)
                batch = []
                batch_bytes = 0
                return True

            for point in points:
                # Payload size dominates these requests; the vector estimate
                # keeps the cap safe without depending on pydantic internals.
                point_bytes = len(json.dumps(point.payload or {}, ensure_ascii=False, default=str).encode("utf-8"))
                point_bytes += len(point.vector) * 8 + 512
                if batch and (
                    len(batch) >= _QDRANT_UPSERT_MAX_POINTS
                    or batch_bytes + point_bytes > _QDRANT_UPSERT_MAX_BYTES
                ):
                    if not flush_batch():
                        return ingested, embed_fails
                batch.append(point)
                batch_bytes += point_bytes

            if not flush_batch():
                return ingested, embed_fails
            return ingested, embed_fails

        return len(points), embed_fails

    # --- retrieval ----------------------------------------------------------

    @staticmethod
    def _search(
        qdrant: QdrantClient,
        col_name: str,
        query_vec: List[float],
        limit: int,
        query_filter: Optional[Filter],
    ) -> tuple[list, bool]:
        try:
            return qdrant_vector_search(qdrant, col_name, query_vec, max(1, limit), query_filter), False
        except Exception as exc:
            get_exception_logger().warning(
                "Qdrant search failed for collection=%s error_type=%s",
                col_name,
                type(exc).__name__,
            )
            return [], True

    def hybrid_retrieve(
        self,
        query: str,
        collection_name: Optional[str] = None,
        limit: int = 10,
        class_name: Optional[str] = None,
        file: Optional[str] = None,
        trace_id: Optional[str] = None,
        graph_legs: Optional[List[str]] = None,
        join_sibling: bool = True,
        apply_rerank: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Hybrid vector + graph retrieval with graceful vector-only fallback.

        ``graph_legs`` selects which graph-expansion legs run after vector
        search (``calls``/``has_knowledge``/``sym``). ``None`` keeps the
        default CALLS-only leg so pre-existing callers observe unchanged
        results; richer legs are opt-in. Unknown leg names raise immediately.

        ``join_sibling`` controls the auto-join of a pinned ``chunk_code_<stem>``
        with its persisted-knowledge sibling ``proj_knowledge_<stem>``. Default
        True keeps single-call callers (``/search-all``, legacy pinned) seeing
        knowledge vectors; index-aware callers that already enumerated the
        sibling as its own plan collection pass False so each collection is
        searched exactly once.

        ``apply_rerank`` lets callers that merge multiple collection results
        defer reranking until the complete candidate pool is available.
        """
        legs = _resolve_graph_legs(graph_legs)
        qdrant = self._get_qdrant()
        trace: Dict[str, Any] = {
            "query_hash": hashlib.sha1(query.encode("utf-8")).hexdigest()[:12],
            "mode": "hybrid",
            "collection": collection_name,
            "searched_collections": [],
            "vector_result_count": 0,
            "graph_neighbor_count": 0,
            "graph_context_count": 0,
            "graph_context_with_code_count": 0,
            "graph_available": False,
            "graph_fallback": False,
            "sources": [],
            "reranker": {
                "enabled": bool(settings.reranker_enabled),
                "model": settings.reranker_model,
                "candidate_count": 0,
                "reranked_count": 0,
                "latency_ms": 0,
                "fallback_reason": None,
            },
        }

        try:
            collections = qdrant.get_collections().collections
        except Exception as exc:
            get_exception_logger().warning(
                "Qdrant collection discovery failed error_type=%s",
                type(exc).__name__,
            )
            trace["mode"] = "vector_unavailable"
            trace["failure_code"] = "qdrant_unavailable"
            record_retrieval_trace(trace_id or "", trace)
            return {"results": [], "searched_collections": [], "mode": trace["mode"],
                    "failure_code": trace["failure_code"]}

        collection_names = {c.name for c in collections}
        target_collections: List[str] = []
        graph_collection_name = collection_name
        if collection_name:
            if collection_name.startswith("snapshot_"):
                from telemetry.registry import default_registry
                if not default_registry().is_ready_collection(collection_name):
                    raise ValueError("Snapshot is not ready for retrieval")
                if settings.embedding_provider == "cohere":
                    from index.embedding_rebuild import active_vector_collection
                    generation = active_vector_collection(collection_name)
                    if not generation:
                        raise ValueError(
                            "This revision needs a Cohere embedding rebuild before retrieval"
                        )
                    collection_name = generation
            if collection_name in collection_names:
                target_collections.append(collection_name)
            # Persisted analysis knowledge rides in a SIBLING collection
            # (proj_knowledge_<stem>) — join it whenever the pinned collection
            # is the project's chunk_code_<stem>. Opt-out callers that already
            # enumerate the sibling as their own plan collection pass
            # ``join_sibling=False`` to avoid a double scan.
            sibling = knowledge_sibling(collection_name)
            if (
                join_sibling
                and sibling
                and sibling in collection_names
                and sibling not in target_collections
            ):
                target_collections.append(sibling)
        else:
            for col in collections:
                # Explicit snapshot consumers must first resolve registry readiness.
                # Never expose staging/failed snapshots through legacy broad search.
                if col.name.startswith("snapshot_"):
                    continue
                if col.name in ("code_symbols", "developer_assistant_rag"):
                    continue
                target_collections.append(col.name)
        trace["searched_collections"] = target_collections

        if not target_collections:
            trace["mode"] = "no_results"
            trace["failure_code"] = "collection_missing"
            record_retrieval_trace(trace_id or "", trace)
            return {"results": [], "searched_collections": [], "mode": trace["mode"],
                    "failure_code": trace["failure_code"]}

        try:
            query_vec = self._get_embed_model().get_query_embedding(query)
        except Exception as exc:
            get_exception_logger().warning(
                "Query embedding failed error_type=%s",
                type(exc).__name__,
            )
            failure_detail = str(exc).strip()[:300] or type(exc).__name__
            trace["mode"] = "vector_unavailable"
            trace["failure_code"] = "embedding_unavailable"
            trace["failure_detail"] = failure_detail
            record_retrieval_trace(trace_id or "", trace)
            return {"results": [], "searched_collections": target_collections,
                    "mode": trace["mode"], "failure_code": trace["failure_code"],
                    "failure_detail": failure_detail}

        # Optional metadata filter (from extracted intent entities).
        query_filter = _build_query_filter(class_name=class_name, file=file)

        store = self._get_graph_store()
        trace["graph_available"] = store is not None
        if store is None:
            trace["graph_fallback"] = True
            trace["mode"] = "vector_only"

        # --- graph legs -----------------------------------------------------
        legs_active = store is not None and bool(legs)
        if legs_active:
            # Additive provenance: present only when graph expansion ran.
            trace["legs"] = list(legs)
        stem = _collection_stem(graph_collection_name)
        sym_collection = _sym_collection_for(stem) if stem else ""
        sym_active = (
            legs_active
            and _GRAPH_LEG_SYM in legs
            and bool(sym_collection)
            and sym_collection_exists(stem)
        )
        if sym_active:
            trace["sym_count"] = 0
        knowledge_active = (
            legs_active
            and _GRAPH_LEG_HAS_KNOWLEDGE in legs
            and any(
                _has_knowledge_leg_active(col, collection_names)
                for col in target_collections
            )
        )
        if knowledge_active:
            trace["has_knowledge_count"] = 0

        final_limit = max(1, int(limit))
        if settings.reranker_enabled:
            candidate_limit = min(
                final_limit * 4,
                max(final_limit, int(settings.reranker_max_candidates)),
            )
        else:
            # Keep the pre-reranker retrieval shape unchanged when disabled.
            candidate_limit = min(5, final_limit)
        all_results: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_sym: set[tuple[Any, ...]] = set()
        vector_hit_payloads: List[Dict[str, Any]] = []
        failed_searches = 0

        for col_name in target_collections:
            hits, search_failed = self._search(qdrant, col_name, query_vec, candidate_limit, query_filter)
            if not hits and query_filter is not None:
                # Filtered search found nothing — fall back to unfiltered.
                hits, fallback_failed = self._search(qdrant, col_name, query_vec, candidate_limit, None)
                search_failed = search_failed and fallback_failed
            failed_searches += int(search_failed)

            vector_ids = []
            for h in hits:
                payload = dict(h.payload or {})
                payload["collection"] = col_name
                node_id = payload.get("node_id") or str(h.id)
                seen_ids.add(node_id)
                vector_ids.append(node_id)
                vector_hit_payloads.append(payload)
                all_results.append(
                    {
                        "collection": col_name,
                        "score": float(h.score or 0.0),
                        "payload": payload,
                        "via_graph": False,
                    }
                )
                trace["vector_result_count"] += 1

            if store is None or not vector_ids:
                continue

            # CALLS leg (default): direct callee/caller neighbours scoped to the
            # searched collection (legacy collection-less edges still match).
            if _GRAPH_LEG_CALLS in legs:
                neighbours = self._traverse_neighbours(store, vector_ids, graph_collection_name)
                if neighbours:
                    trace["graph_neighbor_count"] += len(neighbours)
                    self._append_graph_neighbours(
                        qdrant,
                        all_results,
                        seen_ids,
                        trace,
                        neighbours,
                        graph_leg=_GRAPH_LEG_CALLS,
                        rehydrate_collection=col_name,
                    )

            # HAS_KNOWLEDGE leg (opt-in): surface the persisted-analysis facts
            # attached to a code hit — and the code behind a knowledge hit.
            if knowledge_active and _has_knowledge_leg_active(col_name, collection_names):
                linked = self._traverse_has_knowledge(store, col_name, vector_ids)
                if linked:
                    trace["graph_neighbor_count"] += len(linked)
                    before = len(all_results)
                    self._append_graph_neighbours(
                        qdrant,
                        all_results,
                        seen_ids,
                        trace,
                        linked,
                        graph_leg=_GRAPH_LEG_HAS_KNOWLEDGE,
                        rehydrate_collection=_counterpart_collection(col_name) or col_name,
                    )
                    trace["has_knowledge_count"] += len(all_results) - before

        # SYM leg (opt-in, Neo4j-only): one-hop DECLARES/REFERENCES evidence for
        # PHP symbols the vector hits resolved inside sym_<stem>.
        if sym_active and vector_hit_payloads:
            evidence = self._traverse_sym(store, stem, vector_hit_payloads)
            if evidence:
                before = len(all_results)
                self._append_sym_evidence(
                    all_results, seen_sym, trace, evidence, sym_collection
                )
                trace["sym_count"] += len(all_results) - before

        # Rerank the merged vector + graph candidate pool. The pre-rerank sort
        # keeps the candidate cap deterministic while retaining the strongest
        # vector/graph evidence when many graph rows are present.
        all_results.sort(key=lambda x: x["score"], reverse=True)
        all_results = all_results[:candidate_limit]
        should_rerank = settings.reranker_enabled if apply_rerank is None else apply_rerank
        if should_rerank:
            from rag.reranker import rerank

            trace["reranker"] = rerank(query, all_results)
        if trace["reranker"].get("reranked_count"):
            all_results.sort(
                key=lambda x: (
                    float(x.get("rerank_score", float("-inf"))),
                    float(x.get("score") or 0.0),
                ),
                reverse=True,
            )
        if trace["graph_context_count"] > 0:
            trace["mode"] = "hybrid"
        elif trace["mode"] == "hybrid":
            trace["mode"] = "vector_only"
        if failed_searches == len(target_collections):
            trace["failure_code"] = "qdrant_search_failed"
        trace["sources"] = [
            {
                "file": (r.get("payload") or {}).get("file", ""),
                "class_name": (r.get("payload") or {}).get("class") or (r.get("payload") or {}).get("class_fqn", ""),
                "name": (r.get("payload") or {}).get("name", ""),
                "via_graph": bool(r.get("via_graph")),
                "score": round(float(r.get("score", 0.0)), 4),
            }
            for r in all_results[:final_limit]
        ]
        record_retrieval_trace(trace_id or "", trace)
        return {
            "results": all_results[:final_limit],
            "searched_collections": target_collections,
            "mode": trace["mode"],
            "failure_code": trace.get("failure_code"),
            "candidate_count": trace["vector_result_count"],
        }

    @staticmethod
    def _graph_neighbour_score(results: List[Dict[str, Any]]) -> float:
        """Graph neighbours rank just below the weakest vector hit."""
        vector_scores = [r["score"] for r in results if not r.get("via_graph")]
        if not vector_scores:
            return 0.0
        return max(0.0, min(vector_scores) - 0.01)

    @staticmethod
    def _traverse_neighbours(
        store: Any,
        node_ids: List[Any],
        collection_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """One-hop CALLS neighbours. ``collection_name`` scopes the edges to a
        project; an empty value keeps the legacy unscoped behaviour."""
        try:
            rows = store.structured_query(
                _NEIGHBOURS_CYPHER,
                param_map={
                    "ids": [str(i) for i in node_ids],
                    "collection": collection_name or "",
                },
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            get_exception_logger().error(
                "Neo4j neighbour traversal failed: %s", exc
            )
            return []

    @staticmethod
    def _traverse_has_knowledge(
        store: Any,
        collection_name: str,
        hit_ids: List[Any],
    ) -> List[Dict[str, Any]]:
        """CodeSymbols linked to a collection's vector hits via HAS_KNOWLEDGE.

        Undirected: for a ``chunk_code_*`` hit this returns the knowledge node
        it describes; for a ``proj_knowledge_*`` hit it returns the code chunk
        the fact was attached to. Payload rehydration happens in the caller
        against the *other* collection of the pair.
        """
        try:
            rows = store.structured_query(
                _HAS_KNOWLEDGE_CYPHER,
                param_map={"ids": [str(i) for i in hit_ids]},
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            get_exception_logger().error(
                "Neo4j has-knowledge traversal failed: %s", exc
            )
            return []

    @staticmethod
    def _traverse_sym(
        store: Any,
        stem: str,
        hit_records: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """One-hop DECLARES/REFERENCES evidence for vector-hit PHP symbols.

        Neo4j-only: sym_<stem> nodes have no qdrant payloads, so every returned
        row is a graph fact (node props + edge props) capped at the evidence
        limit.
        """
        collection = _sym_collection_for(stem)
        candidates = _sym_candidates_from_hits(hit_records)
        if not candidates:
            return []
        try:
            rows = store.structured_query(
                _SYM_EVIDENCE_CYPHER,
                param_map={
                    "candidates": candidates,
                    "collection": collection,
                    "cap": _SYM_MAX_EVIDENCE,
                },
            )
            return [dict(r) for r in (rows or [])][:_SYM_MAX_EVIDENCE]
        except Exception as exc:
            get_exception_logger().error(
                "Neo4j symbol-leg traversal failed: %s", exc
            )
            return []

    @staticmethod
    def _fetch_payloads(
        qdrant: QdrantClient,
        collection_name: str,
        node_ids: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        """Best-effort qdrant payload lookup keyed by graph node id (guarded)."""
        try:
            point_ids = [_point_id_for(nid) for nid in node_ids]
            records = qdrant.retrieve(collection_name=collection_name, ids=point_ids)
            fetched: Dict[str, Dict[str, Any]] = {}
            for rec in records:
                p = dict(rec.payload or {})
                if p.get("node_id"):
                    fetched[p["node_id"]] = p
            return fetched
        except Exception:
            return {}

    def _append_graph_neighbours(
        self,
        qdrant: QdrantClient,
        all_results: List[Dict[str, Any]],
        seen_ids: set[str],
        trace: Dict[str, Any],
        rows: List[Dict[str, Any]],
        graph_leg: str,
        rehydrate_collection: str,
    ) -> int:
        """Rehydrate and append one-hop neighbour results from a graph leg.

        Payloads come from qdrant when available; otherwise the traversal row's
        node metadata stands in (honest metadata-only context). Returns how many
        results were actually appended.
        """
        neighbour_ids = [n["tgt"] for n in rows if n["tgt"] not in seen_ids]
        fetched = (
            self._fetch_payloads(qdrant, rehydrate_collection, neighbour_ids)
            if neighbour_ids
            else {}
        )
        appended = 0
        for n in rows:
            tgt = n["tgt"]
            if tgt in seen_ids or tgt == n.get("src"):
                continue
            seen_ids.add(tgt)
            payload = fetched.get(
                tgt,
                _strip_none(
                    {
                        "name": n.get("name", ""),
                        "method_name": n.get("name", ""),
                        "class": n.get("class", ""),
                        "class_fqn": n.get("class_fqn", ""),
                        "namespace": n.get("namespace", ""),
                        "file": n.get("file", ""),
                        "type": n.get("type", ""),
                        "start_line": n.get("start_line"),
                        "end_line": n.get("end_line"),
                        "collection": n.get("collection") or rehydrate_collection,
                    }
                ),
            )
            payload["collection"] = payload.get("collection") or rehydrate_collection
            # Stable cross-call identity for graph rows: vector/rehydrated rows
            # carry ``node_id`` (used first by the answer-path merge), but the
            # metadata-only fallback built above does not — so every graph
            # neighbour also carries a deterministic ``graph_key`` derived from
            # its graph leg, owning collection and target node id. The same
            # neighbour surfaced by two per-collection calls collapses in the
            # merge instead of surviving as a duplicate.
            payload["graph_key"] = (
                f"{graph_leg}|{payload.get('collection') or rehydrate_collection}|{tgt}"
            )
            all_results.append(
                {
                    "collection": payload.get("collection", rehydrate_collection),
                    "score": self._graph_neighbour_score(all_results),
                    "payload": payload,
                    "via_graph": True,
                    "graph_leg": graph_leg,
                }
            )
            appended += 1
            trace["graph_context_count"] += 1
            if payload.get("code"):
                trace["graph_context_with_code_count"] += 1
        return appended

    def _append_sym_evidence(
        self,
        all_results: List[Dict[str, Any]],
        seen_sym: set[tuple[Any, ...]],
        trace: Dict[str, Any],
        rows: List[Dict[str, Any]],
        collection: str,
    ) -> int:
        """Append sym-leg evidence rows as graph results (deduped, capped)."""
        appended = 0
        for row in rows:
            payload = _strip_none(
                {
                    "graph_sym": True,
                    "via_graph": True,
                    "collection": collection,
                    "kind": row.get("kind", ""),
                    "name": row.get("name", ""),
                    "fqn": row.get("fqn", ""),
                    "file": row.get("ref_file") or row.get("file") or "",
                    "line": (
                        row.get("ref_line")
                        if row.get("ref_line") is not None
                        else row.get("line")
                    ),
                    "to_fqn": row.get("src_fqn", ""),
                    "to_member": row.get("to_member"),
                    "rel": row.get("rel", ""),
                    "rel_kind": row.get("rel_kind", ""),
                }
            )
            # Sym rows are Neo4j-only: no qdrant ``node_id``. The graph_key is
            # the stable identity two per-collection calls share for the same
            # evidence row, so the answer-path merge collapses cross-call
            # duplicates instead of keeping both.
            payload["graph_key"] = "|".join(
                [
                    "sym",
                    str(payload.get("kind") or ""),
                    str(payload.get("fqn") or ""),
                    str(payload.get("rel") or ""),
                    str(payload.get("rel_kind") or ""),
                    str(payload.get("to_fqn") or ""),
                    str(payload.get("to_member") or ""),
                    str(payload.get("file") or ""),
                    str(payload.get("line") if payload.get("line") is not None else ""),
                ]
            )
            key = (
                payload.get("fqn"),
                payload.get("kind"),
                payload.get("file"),
                payload.get("line"),
                payload.get("rel_kind"),
                payload.get("to_member"),
                payload.get("to_fqn"),
            )
            if key in seen_sym or appended >= _SYM_MAX_EVIDENCE:
                continue
            seen_sym.add(key)
            all_results.append(
                {
                    "collection": collection,
                    "score": self._graph_neighbour_score(all_results),
                    "payload": payload,
                    "via_graph": True,
                    "graph_leg": _GRAPH_LEG_SYM,
                }
            )
            appended += 1
            trace["graph_context_count"] += 1
        return appended
