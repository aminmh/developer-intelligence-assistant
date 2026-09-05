"""Read-side symbol-graph queries (Tier-4 surface).

Pure Cypher against the ``PhpSymbol``/``sym_<stem>`` subgraph, scoped by the
indexed collection so concurrent projects never bleed into each other.

Queries mirror the prompt's tool vocabulary:

    resolve_symbol / find_implementations / find_references /
    call_hierarchy (callees + callers)

Row shape returned to callers stays plain-dict (definitions first-class),
never leaks Neo4j internals.
"""

from __future__ import annotations

from typing import Any

from inspection.graph import node_key

_MATCH_NODE_BY_ID = """
MATCH (s:PhpSymbol {id: $id})
RETURN s AS n
"""

_MATCH_NODE_BY_FQN = """
MATCH (s:PhpSymbol {collection: $collection, fqn: $fqn})
RETURN s AS n
ORDER BY CASE s.kind WHEN 'class' THEN 0 WHEN 'interface' THEN 1
         WHEN 'trait' THEN 2 WHEN 'enum' THEN 3 WHEN 'method' THEN 4
         WHEN 'function' THEN 5 ELSE 6 END, s.line
"""

_MATCH_NODE_BY_NAME = """
MATCH (s:PhpSymbol {collection: $collection, name: $name})
RETURN s AS n
ORDER BY CASE s.kind WHEN 'class' THEN 0 WHEN 'interface' THEN 1
         WHEN 'trait' THEN 2 WHEN 'enum' THEN 3 WHEN 'method' THEN 4
         WHEN 'function' THEN 5 ELSE 6 END, s.line
"""

_FIND_IMPLEMENTATIONS = """
MATCH (impl:PhpSymbol {collection: $collection})
WHERE (impl)-[:DECLARES {kind: 'implements'}]->(:PhpSymbol {id: $target_id})
   OR (impl)-[:DECLARES {kind: 'extends'}]->(:PhpSymbol {id: $target_id})
RETURN impl AS n ORDER BY impl.file, impl.line
"""

_FIND_REFERENCES = """
MATCH (t:PhpSymbol {id: $target_id})<-[r:REFERENCES]-(caller:PhpSymbol)
WHERE caller.collection = $collection AND t.collection = $collection
RETURN caller AS n, r.kind AS rkind, r.file AS file, r.line AS line,
       r.to_member AS to_member
ORDER BY caller.file, r.line
"""

_CALLEES = """
MATCH (s:PhpSymbol {id: $source_id})-[r:REFERENCES]->(t:PhpSymbol)
WHERE s.collection = $collection AND t.collection = $collection
RETURN t AS n, r.kind AS rkind, r.file AS file, r.line AS line,
       r.to_member AS to_member
ORDER BY t.file, r.line
"""

_CALLERS = """
MATCH (s:PhpSymbol {id: $source_id})<-[r:REFERENCES]-(c:PhpSymbol)
WHERE s.collection = $collection AND c.collection = $collection
RETURN c AS n, r.kind AS rkind, r.file AS file, r.line AS line,
       r.to_member AS to_member
ORDER BY c.file, r.line
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
    }


def _edge_ref(row: dict[str, Any]) -> dict[str, Any]:
    """Reference edge facts — RETURNed as columns (rel props are stripped by
    the property-graph store's triple mapping, so they never live on `r`)."""
    return {
        "kind": row.get("rkind", ""),
        "file": row.get("file", ""),
        "line": row.get("line"),
        "to_member": row.get("to_member"),
    }


class SymbolGraphQuery:
    """Read-side wrapper over the inspection graph store."""

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    def _run(self, cypher: str, params: dict[str, Any]) -> list[Any]:
        store = self._graph._get_store()
        if store is None:
            return []
        try:
            return store.structured_query(cypher, param_map=params) or []
        except Exception as exc:  # noqa: BLE001 — read failures degrade to empty
            from infra.exception_logger import get_exception_logger

            get_exception_logger().warning("Symbol query failed: %s", exc)
            return []

    def resolve_by_fqn(self, collection: str, fqn: str) -> list[dict[str, Any]]:
        rows = self._run(_MATCH_NODE_BY_FQN, {"collection": collection, "fqn": fqn})
        return [_node_row(row["n"]) for row in rows if row.get("n")]

    def resolve_by_name(self, collection: str, name: str) -> list[dict[str, Any]]:
        rows = self._run(_MATCH_NODE_BY_NAME, {"collection": collection, "name": name})
        return [_node_row(row["n"]) for row in rows if row.get("n")]

    def find_implementations(
        self, collection: str, fqn: str
    ) -> list[dict[str, Any]]:
        target = self.resolve_by_fqn(collection, fqn)
        if not target:
            return []
        rows = self._run(
            _FIND_IMPLEMENTATIONS,
            {"collection": collection, "target_id": node_key(
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
            {"collection": collection, "target_id": node_key(
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
        key = node_key(t["kind"], t["fqn"], t["name"])
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
        return {"callees": callees, "callers": callers}
