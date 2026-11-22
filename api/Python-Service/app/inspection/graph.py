"""Neo4j symbol-graph writer for inspection exports.

Separate label/collection from the conceptual-search graph on purpose:

    * CodeSymbol + ``chunk_code_<stem>`` / ``proj_knowledge_<stem>`` — hybrid
      retrieval index (built from code chunks / persisted analysis facts).
    * Symbol     + ``sym_<stem>`` — precise Tier-4 symbol graph (one node per
      class/method/property/function; typed relation edges). Language-neutral:
      each node carries ``language`` and ``origin`` (the producer: php-parser,
      tree-sitter, python-ast, scip or lsp). Graphs written before 2026-11-15
      used the label ``PhpSymbol``; ``migrate_legacy_labels`` relabels them.

Id scheme (deterministic, disjoint from the chunk graph's sha1(class::name))::

    node_id = sha1(f"{kind}|{fqn}|{name}")

Members carry ``owner`` (class fqn). Method call edges materialize only when
the target definition exists in the same indexed set — no dangling nodes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from infra.exception_logger import get_exception_logger
from infra.settings import settings
from telemetry.symbols import normalize

_NODE_UPSERT_CYPHER = """
UNWIND $data AS row
MERGE (s:Symbol {id: row.id})
SET s += row.properties
RETURN count(*) AS count
"""

_REL_UPSERT_CYPHER = """
UNWIND $data AS row
MATCH (s:Symbol {id: row.source_id})
MATCH (t:Symbol {id: row.target_id})
MERGE (s)-[r:REFERENCES {kind: row.kind}]->(t)
SET r.file = row.file, r.line = row.line, r.to_member = row.to_member, r.origin = row.origin
RETURN count(*) AS count
"""

# Relation edges derived from definition records (extends/implements/trait use).
_DEF_REL_UPSERT_CYPHER = """
UNWIND $data AS row
MATCH (s:Symbol {id: row.source_id})
MATCH (t:Symbol {id: row.target_id})
MERGE (s)-[r:DECLARES {kind: row.kind}]->(t)
RETURN count(*) AS count
"""

_EXISTS_CYPHER = """
UNWIND $ids AS id
MATCH (s:Symbol {id: id})
RETURN collect(s.id) AS ids
"""

_STATIC_CALL_EDGES_CYPHER = """
MATCH (caller:Symbol {collection: $collection})-[r:REFERENCES {kind: 'method_call'}]->(callee:Symbol {collection: $collection})
WHERE caller.symbol_id IS NOT NULL AND callee.symbol_id IS NOT NULL
RETURN caller.symbol_id AS caller_symbol_id, callee.symbol_id AS symbol_id
LIMIT 50000
"""

# Call edges for Code Quality flow tracing (lsp-first plan, Phase 3a): project-internal
# calls only, with the producer that found each one.
_CALL_EDGES_CYPHER = """
MATCH (caller:Symbol {collection: $collection})-[r:REFERENCES]->(callee:Symbol {collection: $collection})
WHERE r.kind IN ['method_call', 'static_call', 'function_call']
RETURN caller.fqn AS caller, callee.fqn AS callee, r.kind AS kind, r.origin AS origin,
       r.file AS file, r.line AS line
ORDER BY caller, callee, line
LIMIT $limit
"""

# Calls observed at runtime on this same revision (task 18j); origin `runtime`.
_RUNTIME_CALL_EDGES_CYPHER = """
MATCH (caller:Symbol {collection: $collection})-[r:RUNTIME_CALLS]->(callee:Symbol {collection: $collection})
WHERE r.revision_id IS NOT NULL AND r.revision_id = caller.revision_id
RETURN caller.fqn AS caller, callee.fqn AS callee, 'runtime_call' AS kind, 'runtime' AS origin,
       null AS file, null AS line
ORDER BY caller, callee
LIMIT $limit
"""

# One node per collection; producers is a JSON string (Neo4j properties cannot hold maps).
_COVERAGE_UPSERT_CYPHER = """
MERGE (c:SymbolCoverage {collection: $collection})
SET c.producers = $producers
RETURN count(c) AS count
"""

_RUNTIME_SYMBOLS_CYPHER = """
UNWIND $data AS row
MATCH (s:Symbol {collection: $collection, symbol_id: row.symbol_id})
SET s.runtime_count_rolling = row.count, s.runtime_p95_ms = row.p95_ms,
    s.runtime_error_rate = row.error_rate, s.runtime_last_seen = row.last_seen,
    s.runtime_window_hours = row.window_hours
RETURN count(*) AS count
"""

_RUNTIME_CALLS_CYPHER = """
OPTIONAL MATCH (caller:Symbol {collection: $collection})-[old:RUNTIME_CALLS]->(callee:Symbol {collection: $collection})
WHERE NOT any(row IN $data WHERE row.caller_symbol_id = caller.symbol_id AND row.symbol_id = callee.symbol_id)
WITH collect(old) AS stale
FOREACH (edge IN stale | DELETE edge)
WITH 1 AS ready
UNWIND $data AS row
MATCH (caller:Symbol {collection: $collection, symbol_id: row.caller_symbol_id})
MATCH (callee:Symbol {collection: $collection, symbol_id: row.symbol_id})
MERGE (caller)-[r:RUNTIME_CALLS]->(callee)
SET r.calls = row.calls, r.last_seen = row.last_seen, r.revision_id = $revision_id,
    r.hook_calls = row.hook_calls, r.span_edges = row.span_edges,
    r.sample_weight = row.sample_weight, r.hot_path_share = row.hot_path_share,
    r.sources = row.sources, r.window_hours = row.window_hours
RETURN count(*) AS count
"""

_RUNTIME_ROUTES_CYPHER = """
OPTIONAL MATCH (route:RuntimeRoute {collection: $collection})-[old:HANDLED_BY]->(method:Symbol {collection: $collection})
WHERE NOT any(row IN $data WHERE row.id = route.id AND row.symbol_id = method.symbol_id)
WITH collect(old) AS stale_edges
FOREACH (edge IN stale_edges | DELETE edge)
WITH 1 AS ready
OPTIONAL MATCH (stale:RuntimeRoute {collection: $collection})
WHERE NOT stale.id IN [row IN $data | row.id]
WITH collect(stale) AS stale_routes
FOREACH (route IN stale_routes | DETACH DELETE route)
WITH 1 AS ready
UNWIND $data AS row
MERGE (route:RuntimeRoute {id: row.id})
SET route += row.properties
WITH route, row
MATCH (method:Symbol {collection: $collection, symbol_id: row.symbol_id})
MERGE (route)-[r:HANDLED_BY]->(method)
SET r.calls = row.calls, r.p95_ms = row.p95_ms,
    r.error_rate = row.error_rate, r.last_seen = row.last_seen,
    r.window_hours = row.window_hours
RETURN count(*) AS count
"""

_RUNTIME_TABLES_CYPHER = """
OPTIONAL MATCH (method:Symbol {collection: $collection})-[old:QUERIES]->(table:RuntimeTable {collection: $collection})
WHERE NOT any(row IN $data WHERE row.id = table.id AND row.symbol_id = method.symbol_id)
WITH collect(old) AS stale_edges
FOREACH (edge IN stale_edges | DELETE edge)
WITH 1 AS ready
OPTIONAL MATCH (stale:RuntimeTable {collection: $collection})
WHERE NOT stale.id IN [row IN $data | row.id]
WITH collect(stale) AS stale_tables
FOREACH (table IN stale_tables | DETACH DELETE table)
WITH 1 AS ready
UNWIND $data AS row
MERGE (table:RuntimeTable {id: row.id})
SET table += row.properties
WITH table, row
MATCH (method:Symbol {collection: $collection, symbol_id: row.symbol_id})
MERGE (method)-[r:QUERIES]->(table)
SET r.operations = row.operations, r.operation = row.operation,
    r.last_seen = row.last_seen, r.window_hours = row.window_hours
RETURN count(*) AS count
"""


SYMBOL_LABEL = "Symbol"
LEGACY_SYMBOL_LABEL = "PhpSymbol"
_MIGRATION_BATCH = 10_000
_MIGRATION_MAX_BATCHES = 1_000


def _language_by_suffix() -> dict[str, str]:
    from projects.structure_profile import LANG_EXTENSIONS

    return {suffix: language for language, suffixes in LANG_EXTENSIONS.items() for suffix in suffixes}


def language_for_path(path: Any) -> str | None:
    """Language of a source path by its suffix; None when unknown."""
    text = str(path or "").lower()
    dot = text.rfind(".")
    return _language_by_suffix().get(text[dot:]) if dot >= 0 else None


def default_origin(language: str | None) -> str:
    """Producer for records that name none. Before origins existed only the PHP
    parser wrote this graph, so PHP falls back to it; anything else is unknown."""
    return "php-parser" if language in (None, "php") else "unknown"


def stamp_origin(payload: dict[str, list[dict[str, Any]]], origin: str) -> dict[str, list[dict[str, Any]]]:
    """Set ``origin`` (and ``language`` from the path) on an export's records that lack them."""
    for key in ("definitions", "references"):
        for record in payload.get(key) or []:
            if isinstance(record, dict):
                record.setdefault("origin", origin)
                if key == "definitions" and not record.get("language"):
                    language = language_for_path(record.get("file"))
                    if language:
                        record["language"] = language
    return payload


def _legacy_migration_cypher() -> str:
    """Relabel one batch of legacy ``PhpSymbol`` nodes to ``Symbol`` and set ``language``."""
    cases = "\n".join(
        f"         WHEN toLower(s.file) ENDS WITH '{suffix}' THEN '{language}'"
        for suffix, language in sorted(_language_by_suffix().items())
    )
    return (
        f"MATCH (s:{LEGACY_SYMBOL_LABEL})\n"
        f"WITH s LIMIT {_MIGRATION_BATCH}\n"
        f"SET s:{SYMBOL_LABEL}, s.language = coalesce(s.language, CASE\n{cases}\n         ELSE null END)\n"
        f"REMOVE s:{LEGACY_SYMBOL_LABEL}\n"
        "RETURN count(s) AS count"
    )


def node_key(kind: str, fqn: str, name: str | None = None) -> str:
    """Deterministic node id — stable across re-index runs."""
    raw = f"{kind}|{fqn}|{name or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# Node upserts, reference/call edges and every inspection query match Symbol by id;
# without this index each lookup scans every Symbol node.
_SYMBOL_INDEX_CYPHER = "CREATE RANGE INDEX symbol_id IF NOT EXISTS FOR (n:Symbol) ON (n.id)"


def _ensure_symbol_index(store: Any) -> None:
    """Create the Symbol id index. Best-effort: graph writes still work without it."""
    try:
        store.structured_query(_SYMBOL_INDEX_CYPHER, param_map={})
    except Exception as exc:  # noqa: BLE001 — a missing index only slows lookups
        get_exception_logger().warning("Symbol graph: id index could not be created: %s", exc)


def migrate_legacy_labels(store: Any) -> int:
    """Relabel every legacy ``PhpSymbol`` node to ``Symbol`` in bounded batches.

    Idempotent: once no legacy node is left, it costs one empty match. Edges and
    ids are untouched, so existing collections keep working without a re-index.
    """
    statement = _legacy_migration_cypher()
    moved = 0
    for _ in range(_MIGRATION_MAX_BATCHES):
        count = _first_count(store.structured_query(statement, param_map={}))
        moved += count
        if count < _MIGRATION_BATCH:
            break
    return moved


class InspectionGraph:
    """Lazy Neo4j writer over the inspection symbol graph."""

    _legacy_checked = False  # once per process: relabel PhpSymbol → Symbol

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
                self._migrate_legacy(self._store)
                _ensure_symbol_index(self._store)
            except Exception as exc:  # noqa: BLE001 — connectivity failure is reported, not fatal
                get_exception_logger().error(
                    "Inspection graph: Neo4j connect failed: %s", exc
                )
                self._store_failed = True
                self._store = None
        return self._store

    @classmethod
    def _migrate_legacy(cls, store: Any) -> None:
        if cls._legacy_checked:
            return
        try:
            moved = migrate_legacy_labels(store)
            cls._legacy_checked = True
            if moved:
                get_exception_logger().warning("Symbol graph: relabelled %d legacy PhpSymbol nodes", moved)
        except Exception as exc:  # noqa: BLE001 — retried on the next connection; reads of old nodes miss until then
            get_exception_logger().error("Symbol graph: legacy label migration failed: %s", exc)

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

    def upsert_coverage(self, collection: str, producers: dict[str, Any]) -> int:
        """Record each precise producer's status for ``collection`` (change impact F8)."""
        store = self._get_store()
        if store is None or not producers:
            return 0
        try:
            result = store.structured_query(_COVERAGE_UPSERT_CYPHER, param_map={
                "collection": collection, "producers": json.dumps(producers, sort_keys=True)})
            return _first_count(result)
        except Exception as exc:  # noqa: BLE001 — coverage is advisory; the graph stands without it
            get_exception_logger().warning("Symbol coverage upsert failed: %s", exc)
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

    def upsert_runtime_facts(self, collection: str, facts: dict[str, list[dict[str, Any]]],
                             revision_id: str | None = None) -> dict[str, int]:
        """Upsert rolling facts into the commit-scoped Symbol snapshot graph."""
        store = self._get_store()
        if store is None:
            return {"symbols": 0, "calls": 0, "routes": 0, "tables": 0}
        counts = {"symbols": 0, "calls": 0, "routes": 0, "tables": 0}
        statements = {
            "symbols": _RUNTIME_SYMBOLS_CYPHER,
            "calls": _RUNTIME_CALLS_CYPHER,
            "routes": _RUNTIME_ROUTES_CYPHER,
            "tables": _RUNTIME_TABLES_CYPHER,
        }
        for name, statement in statements.items():
            rows = facts.get(name, [])
            if not rows and name == "symbols":
                continue
            try:
                result = store.structured_query(
                    statement, param_map={"collection": collection, "data": rows, "revision_id": revision_id})
                counts[name] = _first_count(result)
            except Exception as exc:  # noqa: BLE001 — runtime graph projection is retryable
                get_exception_logger().error("Runtime graph %s upsert failed: %s", name, exc)
        return counts

    def call_edges(self, collection: str, limit: int = 60_000) -> list[dict[str, Any]] | None:
        """Project-internal call edges of one collection; None when the graph cannot be read."""
        store = self._get_store()
        if store is None:
            return None
        try:
            rows = store.structured_query(_CALL_EDGES_CYPHER, param_map={"collection": collection, "limit": limit})
        except Exception as exc:  # noqa: BLE001 — the tracer falls back to chunk calls
            get_exception_logger().error("Call-edge read failed: %s", exc)
            return None
        try:
            rows = [*(rows or []), *(store.structured_query(
                _RUNTIME_CALL_EDGES_CYPHER, param_map={"collection": collection, "limit": limit}) or [])]
        except Exception as exc:  # noqa: BLE001 — static edges stand without runtime ones
            get_exception_logger().warning("Runtime call-edge read failed: %s", exc)
        return [
            {"caller": row["caller"], "callee": row["callee"], "kind": row.get("kind"),
             "origin": row.get("origin") or "php-parser", "file": row.get("file"), "line": row.get("line")}
            for row in rows or []
            if isinstance(row, dict) and isinstance(row.get("caller"), str) and isinstance(row.get("callee"), str)
        ]

    def static_call_edges(self, collection: str) -> list[dict[str, str]]:
        """Read bounded method-call edges from the immutable source graph."""
        store = self._get_store()
        if store is None:
            return []
        try:
            rows = store.structured_query(_STATIC_CALL_EDGES_CYPHER, param_map={"collection": collection})
            return [
                {"caller_symbol_id": row["caller_symbol_id"], "symbol_id": row["symbol_id"]}
                for row in rows or []
                if isinstance(row, dict) and isinstance(row.get("caller_symbol_id"), str)
                and isinstance(row.get("symbol_id"), str)
            ][:50000]
        except Exception as exc:  # noqa: BLE001 — missing static graph leaves coverage unavailable
            get_exception_logger().error("Static method-call read failed: %s", exc)
            return []


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
    """Map an exporter definition record → Symbol node properties."""
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
            "language": record.get("language") or language_for_path(record.get("file")),
            # Which producer wrote the definition (php-parser, tree-sitter, python-ast, scip, lsp).
            "origin": record.get("origin") or default_origin(record.get("language") or language_for_path(record.get("file"))),
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
        # Anonymous migration classes expose up()/down() AST chunks but have
        # no stable class identity to use in the runtime symbol graph.
        if not owner:
            return None
        return normalize({"kind": "method", "class": owner, "method": method})
    if record.get("kind") == "function":
        return normalize({"kind": "function", "name": record.get("fqn")})
    return None
