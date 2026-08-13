"""LlamaIndex Property Graph layer: Neo4j graph + Qdrant vectors + hybrid retrieval.

This module ingests PHP code chunks (MCP `chunk_code` output) into:
  * Neo4j        — code symbols as `CodeSymbol` nodes, linked by `CALLS` edges.
  * Qdrant       — embeddable text (signature + code) keyed by the SAME
                   deterministic SHA-1 node id as the graph.

Retrieval is a hybrid vector + graph pipeline:
  1. vector search in Qdrant for semantically similar symbols,
  2. graph traversal to pull direct callee/caller neighbours,
  3. merge + dedup + rerank.

Any graph failure degrades gracefully to vector-only search.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Dict, List, Optional

from llama_index.embeddings.ollama import OllamaEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from settings import settings
from settings_store import settings_store
from exception_logger import get_exception_logger

# Heavy raw attributes kept out of LLM-visible metadata (token bloat), but still
# available for relationship generation during ingestion.
EXCLUDED_LLM_METADATA_KEYS = ("calls", "callers", "project_id")

# Pseudo receivers emitted by the PHP extractor for unresolved/global calls.
# These have no concrete target node, so no CALLS edge is materialised for them.
_PSEUDO_RECEIVERS = ("__unknown", "__function", "__global__")

_MAX_CODE_CHARS = 2000
_MAX_TEXT_CHARS = 4000

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
RETURN count(*) AS count
"""

_NEIGHBOURS_CYPHER = """
UNWIND $ids AS id
MATCH (n:CodeSymbol {id: id})-[r:CALLS]-(m:CodeSymbol)
RETURN n.id AS src, m.id AS tgt, type(r) AS rel,
       m.name AS name, m.class_fqn AS class_fqn, m.class AS class,
       m.namespace AS namespace, m.file AS file,
       m.start_line AS start_line, m.end_line AS end_line,
       m.type AS type, m.collection AS collection
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


def _chunk_node_id(chunk: Dict[str, Any]) -> str:
    fqn = chunk.get("class_fqn") or chunk.get("class")
    name = chunk.get("name")
    return node_id_for(fqn, name)


def build_embeddable_text(chunk: Dict[str, Any]) -> str:
    """Build the LLM-facing text for a chunk (mirrors CodeChunk.toEmbeddableText)."""
    parts: List[str] = []
    docblock = chunk.get("docblock")
    if docblock:
        parts.append(str(docblock))
    if chunk.get("class"):
        parts.append(f"Class: {chunk['class']}")
    if chunk.get("name"):
        sig = [str(chunk["name"])]
        params = chunk.get("parameters", [])
        if params:
            sig.append("(" + _flatten_params(params) + ")")
        if chunk.get("return_type"):
            sig.append(": " + str(chunk["return_type"]))
        parts.append("Signature: " + "".join(sig))

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


def _strip_none(mapping: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in mapping.items() if v is not None}


def _graph_properties(chunk: Dict[str, Any], collection_name: str) -> Dict[str, Any]:
    """Graph node properties — structure only, no code body."""
    return _strip_none(
        {
            "name": chunk.get("name", ""),
            "method_name": chunk.get("name", ""),
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
            "collection": collection_name,
        }
    )


def _payload(chunk: Dict[str, Any], collection_name: str) -> Dict[str, Any]:
    """Qdrant point payload — full retrieval context including code body."""
    return _strip_none(
        {
            "node_id": _chunk_node_id(chunk),
            "type": chunk.get("type", ""),
            "name": chunk.get("name", ""),
            "method_name": chunk.get("name", ""),
            "class": chunk.get("class", ""),
            "class_fqn": chunk.get("class_fqn", ""),
            "namespace": chunk.get("namespace", ""),
            "file": chunk.get("file", ""),
            "start_line": chunk.get("start_line", 0),
            "end_line": chunk.get("end_line", 0),
            "modifiers": chunk.get("modifiers", []) or [],
            "parameters": _flatten_params(chunk.get("parameters")),
            "return_type": chunk.get("return_type", ""),
            "has_docblock": chunk.get("has_docblock", False),
            "docblock": chunk.get("docblock", ""),
            "code": chunk.get("code", ""),
            "collection": collection_name,
        }
    )


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

    def _get_embed_model(self) -> OllamaEmbedding:
        return OllamaEmbedding(
            model_name=settings.ollama_embedding_model,
            base_url=settings.ollama_base_url,
        )

    # --- ingestion ----------------------------------------------------------

    def ingest(self, chunks: List[dict], collection_name: str) -> Dict[str, Any]:
        """Ingest code chunks into the Neo4j graph and Qdrant vectors."""
        if not isinstance(chunks, list):
            chunks = []

        # Dedup by deterministic node id (idempotent re-runs).
        unique: Dict[str, Dict[str, Any]] = {}
        for c in chunks:
            if not isinstance(c, dict):
                continue
            nid = _chunk_node_id(c)
            unique.setdefault(nid, c)
        deduped = list(unique.values())

        if not deduped:
            return {
                "collection_name": collection_name,
                "ingested": 0,
                "chunk_count": 0,
                "graph_nodes": 0,
                "graph_relations": 0,
            }

        graph_nodes, graph_relations = self._ingest_graph(deduped, collection_name)
        ingested, embed_fails = self._ingest_vectors(deduped, collection_name)

        return {
            "collection_name": collection_name,
            "ingested": ingested,
            "chunk_count": len(deduped),
            "graph_nodes": graph_nodes,
            "graph_relations": graph_relations,
            "embed_fails": embed_fails,
        }

    def _ingest_graph(
        self, chunks: List[Dict[str, Any]], collection_name: str
    ) -> tuple[int, int]:
        store = self._get_graph_store()
        if store is None:
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
        seen_rels: set[tuple[str, str]] = set()
        for c in chunks:
            src = _chunk_node_id(c)
            for target in c.get("calls", []) or []:
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
                    {"source_id": src, "target_id": dst, "label": "CALLS"}
                )

        try:
            store.structured_query(_NODE_UPSERT_CYPHER, param_map={"data": node_rows})
        except Exception as exc:
            get_exception_logger().error(
                "Neo4j node upsert failed for collection %s: %s",
                collection_name,
                exc,
            )
            return 0, 0

        if rel_rows:
            try:
                store.structured_query(
                    _REL_UPSERT_CYPHER, param_map={"data": rel_rows}
                )
            except Exception as exc:
                get_exception_logger().error(
                    "Neo4j relation upsert failed for collection %s: %s",
                    collection_name,
                    exc,
                )
                return len(node_rows), 0

        return len(node_rows), len(rel_rows)

    def _ingest_vectors(
        self, chunks: List[Dict[str, Any]], collection_name: str
    ) -> tuple[int, int]:
        qdrant = self._get_qdrant()
        embed_model = self._get_embed_model()

        try:
            qdrant.delete_collection(collection_name)
        except Exception:
            pass

        points: List[PointStruct] = []
        embed_fails = 0
        vector_size: Optional[int] = None

        for c in chunks:
            text = build_embeddable_text(c)
            if not text.strip():
                continue

            try:
                embedding = embed_model.get_text_embedding(text)
            except Exception:
                embed_fails += 1
                continue

            if vector_size is None:
                vector_size = len(embedding)
                try:
                    qdrant.create_collection(
                        collection_name=collection_name,
                        vectors_config=VectorParams(
                            size=vector_size, distance=Distance.COSINE
                        ),
                    )
                except Exception:
                    pass

            points.append(
                PointStruct(
                    id=_point_id_for(_chunk_node_id(c)),
                    vector=embedding,
                    payload=_payload(c, collection_name),
                )
            )

        if points:
            try:
                qdrant.upsert(collection_name=collection_name, wait=True, points=points)
            except Exception as exc:
                get_exception_logger().error(
                    "Qdrant upsert failed for collection %s: %s",
                    collection_name,
                    exc,
                )
                return 0, embed_fails

        return len(points), embed_fails

    # --- retrieval ----------------------------------------------------------

    def hybrid_retrieve(
        self,
        query: str,
        collection_name: Optional[str] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """Hybrid vector + graph retrieval with graceful vector-only fallback."""
        qdrant = self._get_qdrant()

        try:
            collections = qdrant.get_collections().collections
        except Exception:
            return {"results": [], "searched_collections": []}

        target_collections: List[str] = []
        for col in collections:
            if col.name in ("code_symbols", "developer_assistant_rag"):
                continue
            if collection_name and col.name != collection_name:
                continue
            target_collections.append(col.name)

        if not target_collections:
            return {"results": [], "searched_collections": []}

        try:
            query_vec = self._get_embed_model().get_text_embedding(query)
        except Exception:
            return {"results": [], "searched_collections": target_collections}

        store = self._get_graph_store()

        all_results: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()

        for col_name in target_collections:
            try:
                hits = qdrant.search(
                    collection_name=col_name,
                    query_vector=query_vec,
                    limit=min(5, max(1, limit)),
                )
            except Exception:
                continue

            vector_ids = []
            for h in hits:
                payload = dict(h.payload or {})
                payload["collection"] = col_name
                node_id = payload.get("node_id") or str(h.id)
                seen_ids.add(node_id)
                vector_ids.append(node_id)
                all_results.append(
                    {
                        "collection": col_name,
                        "score": float(h.score or 0.0),
                        "payload": payload,
                        "via_graph": False,
                    }
                )

            # Graph expansion: fetch direct callee/caller neighbours.
            if store is None or not vector_ids:
                continue
            neighbours = self._traverse_neighbours(store, vector_ids)
            if not neighbours:
                continue

            neighbour_ids = [n["tgt"] for n in neighbours if n["tgt"] not in seen_ids]
            if not neighbour_ids:
                continue

            fetched: Dict[str, Dict[str, Any]] = {}
            try:
                point_ids = [_point_id_for(nid) for nid in neighbour_ids]
                records = qdrant.retrieve(
                    collection_name=col_name, ids=point_ids
                )
                for rec in records:
                    p = dict(rec.payload or {})
                    if p.get("node_id"):
                        fetched[p["node_id"]] = p
            except Exception:
                fetched = {}

            for n in neighbours:
                tgt = n["tgt"]
                if tgt in seen_ids or tgt == n["src"]:
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
                            "collection": n.get("collection", col_name),
                        }
                    ),
                )
                payload["collection"] = payload.get("collection", col_name)
                all_results.append(
                    {
                        "collection": payload.get("collection", col_name),
                        "score": self._graph_neighbour_score(all_results),
                        "payload": payload,
                        "via_graph": True,
                    }
                )

        all_results.sort(key=lambda x: x["score"], reverse=True)
        return {
            "results": all_results[:limit],
            "searched_collections": target_collections,
        }

    @staticmethod
    def _graph_neighbour_score(results: List[Dict[str, Any]]) -> float:
        """Graph neighbours rank just below the weakest vector hit."""
        vector_scores = [r["score"] for r in results if not r.get("via_graph")]
        if not vector_scores:
            return 0.0
        return max(0.0, min(vector_scores) - 0.01)

    @staticmethod
    def _traverse_neighbours(store: Any, node_ids: List[Any]) -> List[Dict[str, Any]]:
        try:
            rows = store.structured_query(
                _NEIGHBOURS_CYPHER,
                param_map={"ids": [str(i) for i in node_ids]},
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            get_exception_logger().error(
                "Neo4j neighbour traversal failed: %s", exc
            )
            return []
