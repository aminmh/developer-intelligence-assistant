"""Read-side symbol-graph queries (Tier-4 surface).

Pure Cypher against the ``Symbol``/``sym_<stem>`` subgraph, scoped by the
indexed collection so concurrent projects never bleed into each other.

Queries mirror the prompt's tool vocabulary:

    resolve_symbol / find_implementations / find_references /
    call_hierarchy (callees + callers)

Row shape returned to callers stays plain-dict (definitions first-class),
never leaks Neo4j internals.
"""

from __future__ import annotations

from typing import Any

from inspection.graph import default_origin, language_for_path, node_key

_MATCH_NODE_BY_ID = """
MATCH (s:Symbol {id: $id})
RETURN s AS n
"""

_MATCH_NODE_BY_FQN = """
MATCH (s:Symbol {collection: $collection, fqn: $fqn})
RETURN s AS n
ORDER BY CASE s.kind WHEN 'class' THEN 0 WHEN 'interface' THEN 1
         WHEN 'trait' THEN 2 WHEN 'enum' THEN 3 WHEN 'method' THEN 4
         WHEN 'function' THEN 5 ELSE 6 END, s.line
"""

# Functions are stored with their full name as `name` (the inspector keys call scopes
# by it), so a short function name matches the end of the full name instead.
_MATCH_FUNCTION_BY_SUFFIX = """
MATCH (s:Symbol {collection: $collection, kind: 'function'})
WHERE s.fqn ENDS WITH $suffix
RETURN s AS n ORDER BY s.file, s.line LIMIT 20
"""

_MATCH_NODE_BY_NAME = """
MATCH (s:Symbol {collection: $collection, name: $name})
RETURN s AS n
ORDER BY CASE s.kind WHEN 'class' THEN 0 WHEN 'interface' THEN 1
         WHEN 'trait' THEN 2 WHEN 'enum' THEN 3 WHEN 'method' THEN 4
         WHEN 'function' THEN 5 ELSE 6 END, s.line
"""

_FIND_IMPLEMENTATIONS = """
MATCH (impl:Symbol {collection: $collection})
WHERE (impl)-[:DECLARES {kind: 'implements'}]->(:Symbol {id: $target_id})
   OR (impl)-[:DECLARES {kind: 'extends'}]->(:Symbol {id: $target_id})
RETURN impl AS n ORDER BY impl.file, impl.line
"""

_FIND_REFERENCES = """
MATCH (t:Symbol {id: $target_id})<-[r:REFERENCES]-(caller:Symbol)
WHERE caller.collection = $collection AND t.collection = $collection
RETURN caller AS n, r.kind AS rkind, r.file AS file, r.line AS line,
       r.to_member AS to_member, r.origin AS origin
ORDER BY caller.file, r.line
"""

_CALLEES = """
MATCH (s:Symbol {id: $source_id})-[r:REFERENCES]->(t:Symbol)
WHERE s.collection = $collection AND t.collection = $collection
RETURN t AS n, r.kind AS rkind, r.file AS file, r.line AS line,
       r.to_member AS to_member, r.origin AS origin
ORDER BY t.file, r.line
"""

_CALLERS = """
MATCH (s:Symbol {id: $source_id})<-[r:REFERENCES]-(c:Symbol)
WHERE s.collection = $collection AND c.collection = $collection
RETURN c AS n, r.kind AS rkind, r.file AS file, r.line AS line,
       r.to_member AS to_member, r.origin AS origin
ORDER BY c.file, r.line
"""

# Runtime call edges (indexr-otel spans, hooks and profiler samples) written by
# telemetry aggregation. An edge counts only for the revision it was recorded on
# (lsp-first plan, task 18j); others are returned flagged so callers report them stale.
_RUNTIME_CALLEES = """
MATCH (s:Symbol {id: $source_id})-[r:RUNTIME_CALLS]->(t:Symbol)
WHERE s.collection = $collection AND t.collection = $collection
RETURN t AS n, r.calls AS calls, r.last_seen AS last_seen, r.sources AS sources,
       coalesce(r.revision_id, '') = coalesce(s.revision_id, '') AND r.revision_id IS NOT NULL AS current
ORDER BY t.file, t.line
"""

_RUNTIME_CALLERS = """
MATCH (s:Symbol {id: $source_id})<-[r:RUNTIME_CALLS]-(c:Symbol)
WHERE s.collection = $collection AND c.collection = $collection
RETURN c AS n, r.calls AS calls, r.last_seen AS last_seen, r.sources AS sources,
       coalesce(r.revision_id, '') = coalesce(s.revision_id, '') AND r.revision_id IS NOT NULL AS current
ORDER BY c.file, c.line
"""

# Everything that depends on a set of symbols, one hop up, in one read (change impact,
# @.archcore/agent-tools/change-impact-analysis.spec.md clause 6): references and calls,
# types that implement or extend a target, and runtime calls recorded on this revision.
_UPSTREAM = """
UNWIND $ids AS tid
MATCH (t:Symbol {id: tid})<-[r]-(c:Symbol)
WHERE t.collection = $collection AND c.collection = $collection
  AND (type(r) = 'REFERENCES'
       OR (type(r) = 'DECLARES' AND r.kind IN ['implements', 'extends'])
       OR (type(r) = 'RUNTIME_CALLS' AND r.revision_id IS NOT NULL
           AND coalesce(r.revision_id, '') = coalesce(t.revision_id, '')))
RETURN tid AS target, c AS n, type(r) AS rtype, r.kind AS rkind, r.file AS file, r.line AS line,
       r.to_member AS to_member, r.origin AS origin
ORDER BY c.file, c.line, r.line
LIMIT $limit
"""

# Callers that reach a changed method through a class that inherits it without overriding it
# (clause 9), or through its own class node with `to_member` (parser edges): rows shaped
# like _UPSTREAM's, plus the class the reference names.
_INHERITED_CALLERS = """
UNWIND $method_ids AS tid
MATCH (m:Symbol {id: tid})
WHERE m.collection = $collection AND m.kind = 'method' AND m.owner IS NOT NULL
MATCH (owner:Symbol {collection: $collection, fqn: m.owner})
MATCH p = (sub:Symbol {collection: $collection})-[:DECLARES*0..5]->(owner)
WHERE all(x IN relationships(p) WHERE x.kind = 'extends')
  AND (sub = owner OR NOT EXISTS {
        MATCH (o:Symbol {collection: $collection, owner: sub.fqn, name: m.name}) WHERE o.kind = 'method' })
MATCH (sub)<-[r:REFERENCES]-(c:Symbol {collection: $collection})
WHERE r.to_member = m.name
RETURN DISTINCT tid AS target, c AS n, 'REFERENCES' AS rtype, r.kind AS rkind, r.file AS file,
       r.line AS line, r.to_member AS to_member, r.origin AS origin, sub.fqn AS via_class
LIMIT $limit
"""

_RESOLVE_BY_SYMBOL_IDS = """
UNWIND $symbol_ids AS sid
MATCH (s:Symbol {collection: $collection, symbol_id: sid})
RETURN s AS n ORDER BY s.file, s.line
"""

# Runtime routes handled by any of the given symbols (clause 10).
_ROUTES_FOR = """
UNWIND $route_ids AS tid
MATCH (route:RuntimeRoute {collection: $collection})-[:HANDLED_BY]->(s:Symbol {id: tid})
WHERE route.revision_id IS NULL OR s.revision_id IS NULL OR route.revision_id = s.revision_id
RETURN tid AS target, route.method AS method, route.route AS route
ORDER BY route.route, route.method
"""

_RUNTIME_OBSERVED = """
MATCH (s:Symbol {collection: $collection})
WHERE s.runtime_window_hours IS NOT NULL
RETURN count(s) > 0 AS observed
"""

# Producer status per collection (LSP, SCIP), written with the graph (change impact F8).
_PRODUCER_COVERAGE = """
MATCH (c:SymbolCoverage {collection: $collection})
RETURN c.producers AS producers
"""

_RUNTIME_COVERAGE = """
MATCH (s:Symbol {collection: $collection})
WHERE s.kind IN ['method', 'function'] AND s.symbol_id IS NOT NULL
RETURN count(s) AS total,
       sum(CASE WHEN coalesce(s.runtime_count_rolling, 0) > 0 THEN 1 ELSE 0 END) AS seen,
       max(s.runtime_window_hours) AS window_hours, max(s.runtime_last_seen) AS last_seen
"""

_RUNTIME_ROUTES_SEEN = """
MATCH (r:RuntimeRoute {collection: $collection})
RETURN count(r) AS routes
"""

_LIST_PROJECTS = """
MATCH (s:Symbol)
WHERE s.collection STARTS WITH 'sym_'
WITH s.collection AS collection, count(s) AS definitions,
     collect(DISTINCT s.project_id) AS project_ids,
     collect(DISTINCT s.commit_sha) AS commit_shas
RETURN collection, definitions,
       CASE WHEN size(project_ids) = 1 THEN project_ids[0] ELSE null END AS project_id,
       CASE WHEN size(commit_shas) = 1 THEN commit_shas[0] ELSE null END AS commit_sha
ORDER BY collection
"""

_COUNT_DEFINITIONS = """
MATCH (s:Symbol {collection: $collection})
WHERE ($search = '' OR toLower(coalesce(s.name, '')) CONTAINS $search
       OR toLower(coalesce(s.fqn, '')) CONTAINS $search
       OR toLower(coalesce(s.owner, '')) CONTAINS $search
       OR toLower(coalesce(s.file, '')) CONTAINS $search)
  AND ($kind = '' OR s.kind = $kind)
  AND ($file = '' OR toLower(coalesce(s.file, '')) CONTAINS $file)
RETURN count(s) AS total
"""

_LIST_DEFINITIONS = """
MATCH (s:Symbol {collection: $collection})
WHERE ($search = '' OR toLower(coalesce(s.name, '')) CONTAINS $search
       OR toLower(coalesce(s.fqn, '')) CONTAINS $search
       OR toLower(coalesce(s.owner, '')) CONTAINS $search
       OR toLower(coalesce(s.file, '')) CONTAINS $search)
  AND ($kind = '' OR s.kind = $kind)
  AND ($file = '' OR toLower(coalesce(s.file, '')) CONTAINS $file)
RETURN s AS n
ORDER BY s.file, s.line,
         CASE s.kind WHEN 'class' THEN 0 WHEN 'interface' THEN 1
         WHEN 'trait' THEN 2 WHEN 'enum' THEN 3 WHEN 'function' THEN 4
         WHEN 'method' THEN 5 WHEN 'property' THEN 6
         WHEN 'class_const' THEN 7 ELSE 8 END, s.name
SKIP $offset LIMIT $limit
"""


def _props(obj: Any) -> dict[str, Any]:
    """Neo4j Node/Relationship → plain dict across driver/library shapes."""
    if isinstance(obj, dict):
        return obj
    for key in ("_properties",):
        props = getattr(obj, key, None)
        if isinstance(props, dict):
            return props
    if hasattr(obj, "items"):
        try:
            items = obj.items()
            if items is not None:
                return dict(items)
        except TypeError:
            pass
    return {}


def _node_row(node: Any) -> dict[str, Any]:
    """Neo4j record node → plain definition dict."""
    props = _props(node)
    return {
        **({"id": props["id"], "symbol_id": props.get("symbol_id"),
            "project_id": props["project_id"], "commit_sha": props.get("commit_sha")}
           if props.get("project_id") and props.get("id") else {}),
        "kind": props.get("kind", ""),
        "name": props.get("name", ""),
        "fqn": props.get("fqn", ""),
        "file": props.get("file", ""),
        "line": props.get("line"),
        "end_line": props.get("end_line"),
        "visibility": props.get("visibility"),
        "static": props.get("static"),
        "abstract": props.get("abstract"),
        "owner": props.get("owner"),
        "language": props.get("language") or language_for_path(props.get("file")),
        **({"runtime": {"count": props.get("runtime_count_rolling"), "last_seen": props.get("runtime_last_seen"),
                        "window_hours": props.get("runtime_window_hours")}}
           if props.get("runtime_window_hours") is not None else {}),
        # Nodes written before origins existed came only from the PHP parser.
        "origin": props.get("origin") or default_origin(props.get("language") or language_for_path(props.get("file"))),
    }


def _edge_ref(row: dict[str, Any]) -> dict[str, Any]:
    """Reference edge facts — RETURNed as columns (rel props are stripped by
    the property-graph store's triple mapping, so they never live on `r`)."""
    return {
        "kind": row.get("rkind", ""),
        "file": row.get("file", ""),
        "line": row.get("line"),
        "to_member": row.get("to_member"),
        # Edges written before origins existed came only from the PHP parser.
        "origin": row.get("origin") or "php-parser",
    }


def _upstream_rows(rows: list[Any]) -> list[dict[str, Any]]:
    """Upstream query rows -> dependents with the edge that reaches each target."""
    out = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("target"):
            continue
        item = _node_row(row.get("n") or {})
        if not item.get("id"):
            item["id"] = node_key(item["kind"], item["fqn"], item["name"])
        rtype = row.get("rtype") or "REFERENCES"
        if rtype == "RUNTIME_CALLS":
            ref = {"kind": "runtime_call", "file": item.get("file", ""), "line": item.get("line"),
                   "to_member": None, "origin": "runtime"}
        else:
            ref = _edge_ref(row)
        item["ref"] = {**ref, "relation": rtype, **({"via_class": row["via_class"]} if row.get("via_class") else {})}
        item["target"] = row["target"]
        out.append(item)
    return out


class SymbolGraphQuery:
    """Read-side wrapper over the inspection graph store."""

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    def _run(self, cypher: str, params: dict[str, Any]) -> list[Any]:
        collection = params.get("collection", "")
        if collection.startswith("sym_snapshot_"):
            from telemetry.registry import default_registry
            if not default_registry().is_ready_collection(collection.removeprefix("sym_")):
                return []
        store = self._graph._get_store()
        if store is None:
            return []
        try:
            return store.structured_query(cypher, param_map=params) or []
        except Exception as exc:  # noqa: BLE001 — read failures degrade to empty
            from infra.exception_logger import get_exception_logger

            get_exception_logger().warning("Symbol query failed: %s", exc)
            return []

    def available(self) -> bool:
        """Whether the graph store can be reached at all."""
        return self._graph._get_store() is not None

    def upstream(self, collection: str, ids: list[str], limit: int = 50_000) -> list[dict[str, Any]] | None:
        """Symbols one hop upstream of ``ids``; None when the graph cannot be read.

        Unlike the other queries a read failure is not folded into an empty result,
        so change impact can report ``coverage: unavailable`` instead of "no dependents".
        """
        rows = self._run_strict(_UPSTREAM, {"collection": collection, "ids": list(ids), "limit": int(limit)},
                                bool(ids))
        return None if rows is None else _upstream_rows(rows)

    def inherited_callers(self, collection: str, method_ids: list[str],
                          limit: int = 50_000) -> list[dict[str, Any]] | None:
        """Callers of methods through non-overriding subclasses; None when the graph cannot be read."""
        rows = self._run_strict(_INHERITED_CALLERS, {"collection": collection, "method_ids": list(method_ids),
                                                     "limit": int(limit)}, bool(method_ids))
        return None if rows is None else _upstream_rows(rows)

    def resolve_by_symbol_ids(self, collection: str, symbol_ids: list[str]) -> list[dict[str, Any]]:
        if not symbol_ids:
            return []
        rows = self._run(_RESOLVE_BY_SYMBOL_IDS, {"collection": collection, "symbol_ids": list(symbol_ids)})
        return [_node_row(row["n"]) for row in rows if row.get("n")]

    def routes_for(self, collection: str, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        rows = self._run(_ROUTES_FOR, {"collection": collection, "route_ids": list(ids)})
        return [{"target": row["target"], "method": row.get("method") or "", "route": row.get("route") or ""}
                for row in rows if isinstance(row, dict) and row.get("target")]

    def runtime_observed(self, collection: str) -> bool:
        rows = self._run(_RUNTIME_OBSERVED, {"collection": collection})
        return bool(rows and rows[0].get("observed"))

    def producer_coverage(self, collection: str) -> dict[str, Any] | None:
        """``{producer: {status, languages}}`` recorded at index time, or None when never recorded."""
        rows = self._run(_PRODUCER_COVERAGE, {"collection": collection})
        value = rows[0].get("producers") if rows else None
        if isinstance(value, str):
            import json

            try:
                value = json.loads(value)
            except ValueError:
                return None
        return value if isinstance(value, dict) else None

    def _run_strict(self, cypher: str, params: dict[str, Any], wanted: bool) -> list[Any] | None:
        """Like ``_run`` but a read failure is None, not an empty result."""
        store = self._graph._get_store()
        if store is None:
            return None
        if not wanted:
            return []
        try:
            return store.structured_query(cypher, param_map=params) or []
        except Exception as exc:  # noqa: BLE001 — reported as unavailable coverage
            from infra.exception_logger import get_exception_logger

            get_exception_logger().warning("Impact graph query failed: %s", exc)
            return None

    def resolve_by_fqn(self, collection: str, fqn: str) -> list[dict[str, Any]]:
        rows = self._run(_MATCH_NODE_BY_FQN, {"collection": collection, "fqn": fqn})
        return [_node_row(row["n"]) for row in rows if row.get("n")]

    def list_projects(self) -> list[dict[str, Any]]:
        rows = self._run(_LIST_PROJECTS, {})
        from telemetry.registry import default_registry

        return [
            {
                "collection": row.get("collection", ""),
                "stem": str(row.get("collection", "")).removeprefix("sym_"),
                "definitions": int(row.get("definitions", 0) or 0),
                "project_id": row.get("project_id"),
                "commit_sha": row.get("commit_sha"),
            }
            for row in rows
            if row.get("collection")
            and (
                not str(row.get("collection", "")).startswith("sym_snapshot_")
                or default_registry().is_ready_collection(
                    str(row["collection"]).removeprefix("sym_")
                )
            )
        ]

    def list_definitions(
        self,
        collection: str,
        *,
        search: str = "",
        kind: str = "",
        file: str = "",
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        params = {
            "collection": collection,
            "search": search.strip().lower(),
            "kind": kind.strip(),
            "file": file.strip().lower(),
            "offset": max(0, int(offset)),
            "limit": min(200, max(1, int(limit))),
        }
        total_rows = self._run(_COUNT_DEFINITIONS, params)
        rows = self._run(_LIST_DEFINITIONS, params)
        return {
            "items": [_node_row(row.get("n") or {}) for row in rows if row.get("n")],
            "total": int((total_rows[0] if total_rows else {}).get("total", 0) or 0),
            "offset": params["offset"],
            "limit": params["limit"],
        }

    def resolve_by_name(self, collection: str, name: str) -> list[dict[str, Any]]:
        rows = self._run(_MATCH_NODE_BY_NAME, {"collection": collection, "name": name})
        return [_node_row(row["n"]) for row in rows if row.get("n")]

    def resolve_function_by_suffix(self, collection: str, name: str) -> list[dict[str, Any]]:
        """Functions whose full name ends with ``\\name`` (Tree-sitter and SCIP store full names)."""
        rows = self._run(_MATCH_FUNCTION_BY_SUFFIX, {"collection": collection, "suffix": "\\" + name})
        return [_node_row(row["n"]) for row in rows if row.get("n")]

    def find_implementations(
        self, collection: str, fqn: str
    ) -> list[dict[str, Any]]:
        target = self.resolve_by_fqn(collection, fqn)
        if not target:
            return []
        rows = self._run(
            _FIND_IMPLEMENTATIONS,
            {"collection": collection, "target_id": target[0].get("id") or node_key(
                target[0]["kind"], target[0]["fqn"], target[0]["name"]
            )},
        )
        return [_node_row(row["n"]) for row in rows if row.get("n")]

    def find_references(
        self,
        collection: str,
        fqn: str,
        name: str | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        target = self.resolve_by_fqn(collection, fqn)
        if not target:
            return []
        rows = self._run(
            _FIND_REFERENCES,
            {"collection": collection, "target_id": target[0].get("id") or node_key(
                target[0]["kind"], target[0]["fqn"], target[0]["name"]
            )},
        )
        out = []
        for row in rows:
            caller = _node_row(row.get("n") or {})
            ref = _edge_ref(row)
            if kind and ref.get("kind") != kind:
                continue
            caller["ref"] = ref
            out.append(caller)
        return out

    def call_hierarchy(
        self, collection: str, fqn: str, name: str | None = None
    ) -> dict[str, Any]:
        """Callees (down) and callers (up) of one method/function symbol."""
        target = self.resolve_by_fqn(collection, fqn)
        if not target:
            return {"callees": [], "callers": []}
        t = target[0]
        key = t.get("id") or node_key(t["kind"], t["fqn"], t["name"])
        callee_rows = self._run(_CALLEES, {"collection": collection, "source_id": key})
        caller_rows = self._run(_CALLERS, {"collection": collection, "source_id": key})
        callees = []
        for row in callee_rows:
            item = _node_row(row.get("n") or {})
            item["ref"] = _edge_ref(row)
            callees.append(item)
        callers = []
        for row in caller_rows:
            item = _node_row(row.get("n") or {})
            item["ref"] = _edge_ref(row)
            callers.append(item)
        runtime_callees, stale_down = self._runtime_rows(_RUNTIME_CALLEES, collection, key)
        runtime_callers, stale_up = self._runtime_rows(_RUNTIME_CALLERS, collection, key)
        return {"callees": callees, "callers": callers,
                "runtime_callees": runtime_callees, "runtime_callers": runtime_callers,
                "runtime_stale_edges": stale_down + stale_up,
                # The target's own runtime window: present once aggregation covered this revision.
                "runtime_observed": t.get("runtime") is not None}

    def _runtime_rows(self, cypher: str, collection: str, key: str) -> tuple[list[dict[str, Any]], int]:
        out, stale = [], 0
        for row in self._run(cypher, {"collection": collection, "source_id": key}):
            if not row.get("current"):
                stale += 1
                continue
            item = _node_row(row.get("n") or {})
            item["ref"] = {"kind": "runtime_call", "file": item.get("file", ""), "line": item.get("line"),
                           "to_member": None, "origin": "runtime", "count": row.get("calls"),
                           "last_seen": row.get("last_seen"),
                           "sources": list(row.get("sources") or []) if isinstance(row.get("sources"), list) else []}
            out.append(item)
        return out, stale

    def runtime_coverage(self, collection: str) -> dict[str, Any]:
        """Share of indexed methods seen running in the aggregation window, and routes observed."""
        rows = self._run(_RUNTIME_COVERAGE, {"collection": collection})
        row = rows[0] if rows else {}
        total = int(row.get("total") or 0)
        seen = int(row.get("seen") or 0)
        routes = self._run(_RUNTIME_ROUTES_SEEN, {"collection": collection})
        observed = row.get("window_hours") is not None
        return {"observed": observed, "methods_seen": seen, "methods_total": total,
                "percent": round(100 * seen / total, 1) if observed and total else None,
                "routes_seen": int((routes[0] if routes else {}).get("routes") or 0),
                "window_hours": row.get("window_hours"), "last_seen": row.get("last_seen")}
