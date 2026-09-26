"""Neo4j symbol-graph writer for inspection exports.

Separate label/collection from the conceptual-search graph on purpose:

    * CodeSymbol + ``chunk_code_<stem>`` / ``proj_knowledge_<stem>`` — hybrid
      retrieval index (built from code chunks / persisted analysis facts).
    * PhpSymbol  + ``sym_<stem>`` — precise Tier-4 symbol graph (one node per
      class/method/property/function; typed relation edges).

Id scheme (deterministic, disjoint from the chunk graph's sha1(class::name))::

    node_id = sha1(f"{kind}|{fqn}|{name}")

Members carry ``owner`` (class fqn). Method call edges materialize only when
the target definition exists in the same indexed set — no dangling nodes.
"""

from __future__ import annotations

import hashlib
from typing import Any

from infra.exception_logger import get_exception_logger
from infra.settings import settings
from telemetry.symbols import normalize

_NODE_UPSERT_CYPHER = """
UNWIND $data AS row
MERGE (s:PhpSymbol {id: row.id})
SET s += row.properties
RETURN count(*) AS count
"""

_REL_UPSERT_CYPHER = """
UNWIND $data AS row
MATCH (s:PhpSymbol {id: row.source_id})
MATCH (t:PhpSymbol {id: row.target_id})
MERGE (s)-[r:REFERENCES {kind: row.kind}]->(t)
SET r.file = row.file, r.line = row.line, r.to_member = row.to_member
RETURN count(*) AS count
"""

# Relation edges derived from definition records (extends/implements/trait use).
_DEF_REL_UPSERT_CYPHER = """
UNWIND $data AS row
MATCH (s:PhpSymbol {id: row.source_id})
MATCH (t:PhpSymbol {id: row.target_id})
MERGE (s)-[r:DECLARES {kind: row.kind}]->(t)
RETURN count(*) AS count
"""

_EXISTS_CYPHER = """
UNWIND $ids AS id
MATCH (s:PhpSymbol {id: id})
RETURN collect(s.id) AS ids
"""


def node_key(kind: str, fqn: str, name: str | None = None) -> str:
    """Deterministic node id — stable across re-index runs."""
    raw = f"{kind}|{fqn}|{name or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class InspectionGraph:
    """Lazy Neo4j writer over the inspection symbol graph."""

    def __init__(self) -> None:
        self._store: Any = None
        self._store_failed = False

    def _get_store(self) -> Any:
        if self._store is None and not self._store_failed:
            try:
                from llama_index.graph_stores.neo4j import Neo4jPropertyGraphStore

                self._store = Neo4jPropertyGraphStore(
                    username=settings.neo4j_user,
                    password=settings.neo4j_password,
                    url=settings.neo4j_url,
                    database=settings.neo4j_database,
                    refresh_schema=False,
                    create_indexes=False,
                    sanitize_query_output=False,
                )
            except Exception as exc:  # noqa: BLE001 — connectivity failure is reported, not fatal
                get_exception_logger().error(
                    "Inspection graph: Neo4j connect failed: %s", exc
                )
                self._store_failed = True
                self._store = None
        return self._store

    @property
    def available(self) -> bool:
        return self._get_store() is not None

    def upsert_nodes(self, rows: list[dict[str, Any]]) -> int:
        store = self._get_store()
        if store is None or not rows:
            return 0
        try:
            result = store.structured_query(
                _NODE_UPSERT_CYPHER, param_map={"data": rows}
            )
            return _first_count(result)
        except Exception as exc:  # noqa: BLE001 — graph writes degrade to counts
            get_exception_logger().error("Inspection graph node upsert failed: %s", exc)
            return 0

    def upsert_references(self, rows: list[dict[str, Any]]) -> int:
        store = self._get_store()
        if store is None or not rows:
            return 0
        try:
            result = store.structured_query(
                _REL_UPSERT_CYPHER, param_map={"data": rows}
            )
            return _first_count(result)
        except Exception as exc:  # noqa: BLE001 — graph writes degrade to counts
            get_exception_logger().error("Inspection graph ref upsert failed: %s", exc)
            return 0

    def upsert_declaration_relations(self, rows: list[dict[str, Any]]) -> int:
        store = self._get_store()
        if store is None or not rows:
            return 0
        try:
            result = store.structured_query(
                _DEF_REL_UPSERT_CYPHER, param_map={"data": rows}
            )
            return _first_count(result)
        except Exception as exc:  # noqa: BLE001 — graph writes degrade to counts
            get_exception_logger().error(
                "Inspection graph declaration upsert failed: %s", exc
            )
            return 0

    def existing_ids(self, ids: list[str]) -> set[str]:
        store = self._get_store()
        if store is None or not ids:
            return set()
        try:
            result = store.structured_query(
                _EXISTS_CYPHER, param_map={"ids": ids}
            )
            found = result[0]["ids"] if result else []
            return set(found or [])
        except Exception as exc:  # noqa: BLE001 — probes degrade to empty
            get_exception_logger().warning(
                "Inspection graph existence probe failed: %s", exc
            )
            return set()


def _first_count(result: Any) -> int:
    """structured_query returns list[dict]; count(*) is the first column."""
    try:
        if not result:
            return 0
        row = result[0] if isinstance(result, list) else result
        for value in (row or {}).values():
            if isinstance(value, int):
                return value
        return int(str(row.get("count", 0) or 0)) if isinstance(row, dict) else 0
    except Exception:  # noqa: BLE001 — any shape mismatch means "count unknown"
        return 0


def _strip_none(mapping: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in mapping.items() if v is not None}


def node_properties(
    record: dict[str, Any], collection: str
) -> dict[str, Any]:
    """Map an exporter definition record → PhpSymbol node properties."""
    props = _strip_none(
        {
            "kind": record.get("kind", ""),
            "name": record.get("name", ""),
            "fqn": record.get("fqn", ""),
            "file": record.get("file", ""),
            "line": record.get("line"),
            "end_line": record.get("end_line"),
            "visibility": record.get("visibility"),
            "static": record.get("static"),
            "abstract": record.get("abstract"),
            "owner": record.get("owner"),
            "collection": collection,
        }
    )
    props.setdefault("file", "")
    try:
        canonical = canonical_definition(record)
        if canonical is not None:
            props["symbol_id"] = canonical
    except ValueError:
        props["symbol_identity_error"] = "invalid_symbol_identity"
    return props


def canonical_definition(record: dict[str, Any]) -> str | None:
    if record.get("kind") == "method":
        owner, _, method = str(record.get("fqn") or "").rpartition("::")
        return normalize({"kind": "method", "class": owner, "method": method})
    if record.get("kind") == "function":
        return normalize({"kind": "function", "name": record.get("fqn")})
    return None
