"""Inspection orchestration service.

Two ingestion surfaces, one read surface:

    * Tier-2/3 file reads (skeleton / attributes / method) go straight to the
      PHP exporter — exact source, no indexing cost.
    * Tier-4 queries (resolve / references / implementations / call hierarchy)
      run against the ``sym_<stem>`` symbol graph that ``index()`` builds once
      per project. Graph reads are pure and safe to call repeatedly.

Honesty contract: graph queries return only facts present in the indexed
project. A symbol that was never indexed resolves to an empty result — never a
guess. Failed Neo4j writes degrade to ``indexed: False`` with counts, never an
exception.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from inspection import cli as php_cli
from inspection.graph import InspectionGraph, canonical_definition, node_key, node_properties
from telemetry.snapshot import Snapshot, storage_id
from inspection.queries import SymbolGraphQuery

logger = logging.getLogger(__name__)

_SYM_PREFIX = "sym_"


def sym_collection_for(stem: str) -> str:
    """``sym_<stem>`` — sanitized for use as a Neo4j property value."""
    clean = re.sub(r"[^A-Za-z0-9_]", "_", stem or "unnamed")
    return f"{_SYM_PREFIX}{clean or 'unnamed'}"


class InspectionService:
    """High-level Tier-2..4 inspection facade for one backend + graph."""

    def __init__(self, graph: InspectionGraph | None = None) -> None:
        self._graph = graph or InspectionGraph()
        self._query = SymbolGraphQuery(self._graph)

    # --- project file reads (exporter, no index) ----------------------------

    def skeleton(self, file_path: str) -> dict[str, Any]:
        return php_cli.inspect_skeleton(file_path)

    def attributes(
        self, file_path: str, class_name: str | None = None
    ) -> list[dict[str, Any]]:
        return php_cli.inspect_attributes(file_path, class_name)

    def method(self, file_path: str, method_name: str) -> dict[str, Any] | None:
        return php_cli.inspect_method(file_path, method_name)

    # --- graph indexing -----------------------------------------------------

    def index(
        self,
        project_dir: str,
        stem: str,
        exclude: list[str] | None = None,
        exported: dict[str, list[dict[str, Any]]] | None = None,
        snapshot: Snapshot | None = None,
    ) -> dict[str, Any]:
        """Export defs + refs and materialize the sym_<stem> symbol graph.

        ``exclude`` = extra relative dir segments skipped by the exporter
        (e.g. ``["tests", "coverage", "stubs"]``) — callers decide; the
        exporter always skips vendor/node_modules/.git noise.

        Returns counts; ``indexed`` False when Neo4j is unreachable (export
        already happened — re-callable once the store is back).
        """
        collection = sym_collection_for(stem)
        if snapshot is None and collection.startswith("sym_snapshot_"):
            raise ValueError("Snapshot collections require explicit snapshot indexing")
        if snapshot is not None:
            collection = "sym_" + snapshot.collection
        if exported is not None:
            payload = exported
        else:
            try:
                payload = php_cli.inspect_index(project_dir, exclude)
            except Exception:
                # Keep the older two-command boundary as a compatibility
                # fallback for older PHP images and hermetic callers that
                # provide the legacy exporters directly.
                payload = {
                    "definitions": php_cli.inspect_symbols(project_dir, exclude),
                    "references": php_cli.inspect_references(project_dir, exclude),
                }
        defs = payload.get("definitions", [])
        refs = payload.get("references", [])

        nodes = [self._def_node(d, collection) for d in defs]
        node_rows = [n for n in nodes if n is not None]
        scoped_ids = {}
        if snapshot is not None:
            for record, node in zip(defs, nodes):
                if node is None:
                    continue
                symbol = canonical_definition(record)
                identity = symbol or f"declaration:{record.get('kind')}:{record.get('fqn')}"
                scoped = storage_id(snapshot.project_id, snapshot.commit_sha, identity)
                if scoped in scoped_ids.values() or node["id"] in scoped_ids:
                    raise ValueError("Ambiguous duplicate inspection definition")
                scoped_ids[node["id"]] = scoped
                node["id"] = scoped
                node["properties"].update(snapshot.metadata())
        written = self._graph.upsert_nodes(node_rows) if node_rows else 0
        if snapshot is not None and written != len(node_rows):
            raise RuntimeError("Snapshot inspection nodes incomplete")

        if not written:
            return {
                "collection": collection,
                "indexed": False,
                "definitions": len(defs),
                "references": len(refs),
                "nodes": 0,
                "relations": 0,
                "links": 0,
            }

        # Declared relationships (extends/implements/uses_trait) — link only
        # when both endpoints were defined in this project.
        by_fqn = _def_index(defs)
        decl_rows = []
        for d in defs:
            src = _node_key_for_record(d)
            for rel in d.get("relations") or []:
                target = by_fqn.get(rel.get("target", ""))
                if not target:
                    continue
                decl_rows.append(
                    {
                        "source_id": src,
                        "target_id": _node_key_for_record(target),
                        "kind": rel.get("kind", ""),
                    }
                )
        if snapshot is not None:
            _scope_edges(decl_rows, scoped_ids)
        relations = self._graph.upsert_declaration_relations(decl_rows)
        if snapshot is not None and relations != len(decl_rows):
            raise RuntimeError("Snapshot inspection declarations incomplete")

        # Use-sites → REFERENCES edges (only when both ends are known).
        def_keys = {_node_key_for_record(d) for d in defs}
        ref_rows = []
        linked = 0
        via_counts: dict[str, int] = {}
        for r in refs:
            src = self._source_key(r)
            if src is None or src not in def_keys:
                continue
            dst = self._target_key(r, by_fqn)
            if dst is None or dst not in def_keys:
                continue
            via = r.get("via")
            if r.get("kind") == "method_call" and via:
                via_counts[str(via)] = via_counts.get(str(via), 0) + 1
            ref_rows.append(
                {
                    "source_id": src,
                    "target_id": dst,
                    "kind": r.get("kind", "method_call"),
                    "file": r.get("file", ""),
                    "line": r.get("line", 0),
                    "to_member": r.get("to_member"),
                }
            )
            linked += 1
        if snapshot is not None:
            _scope_edges(ref_rows, scoped_ids)
        links = self._graph.upsert_references(ref_rows)
        if snapshot is not None and links != len(ref_rows):
            raise RuntimeError("Snapshot inspection references incomplete")

        return {
            "collection": collection,
            "indexed": True,
            "definitions": len(defs),
            "references": len(refs),
            "nodes": len(node_rows),
            "relations": relations,
            "links": links,
            "unlinked": len(refs) - linked,
            "via": via_counts,
        }

    # --- graph queries ------------------------------------------------------

    @property
    def query(self) -> SymbolGraphQuery:
        return self._query

    def resolve_symbol(self, collection: str, symbol: str) -> list[dict[str, Any]]:
        """Tier-4 lsp_resolve_symbol stand-in (fqn-first, then by name)."""
        fqn = symbol.strip().removeprefix("\\")
        rows = self._query.resolve_by_fqn(collection, fqn)
        if rows:
            return rows
        return self._query.resolve_by_name(collection, symbol.strip())

    def list_projects(self) -> list[dict[str, Any]]:
        return self._query.list_projects()

    def list_definitions(self, collection: str, **filters: Any) -> dict[str, Any]:
        return self._query.list_definitions(collection, **filters)

    def find_implementations(self, collection: str, fqn: str) -> list[dict[str, Any]]:
        return self._query.find_implementations(collection, fqn)

    def find_references(
        self, collection: str, fqn: str, name: str | None = None
    ) -> list[dict[str, Any]]:
        return self._query.find_references(collection, fqn, name=name)

    def call_hierarchy(
        self, collection: str, fqn: str, name: str | None = None
    ) -> dict[str, Any]:
        return self._query.call_hierarchy(collection, fqn, name=name)

    # --- record mapping helpers --------------------------------------------

    def _def_node(self, record: dict[str, Any], collection: str) -> dict | None:
        kind = record.get("kind")
        if kind not in (
            "class", "interface", "trait", "enum", "method", "property",
            "class_const", "function", "const",
        ):
            return None
        fqn = record.get("fqn", "")
        name = record.get("name", "")
        return {
            "id": node_key(kind, fqn, name),
            "properties": node_properties(record, collection),
        }

    def _source_key(self, ref: dict[str, Any]) -> str | None:
        """Scope string → owning def key (method/function)."""
        scope = ref.get("scope")
        if not scope:
            return None
        if "::" in scope:
            _owner, _, member = scope.rpartition("::")
            return node_key("method", scope, member)
        return node_key("function", scope, scope)

    def _target_key(
        self, ref: dict[str, Any], by_fqn: dict[str, dict[str, Any]]
    ) -> str | None:
        kind = ref.get("kind")
        to_fqn = ref.get("to_fqn")
        member = ref.get("to_member")
        if not to_fqn:
            return None
        to_fqn = str(to_fqn).lstrip("\\")
        if kind in ("static_call", "method_call") and member:
            # Target is a method on a known class.
            return node_key("method", f"{to_fqn}::{member}", member)
        # new / instanceof / catch / class_const / function_call → def record.
        target = by_fqn.get(to_fqn)
        if target is None:
            return None
        return _node_key_for_record(target)


def _scope_edges(rows: list[dict], ids: dict[str, str]) -> None:
    for row in rows:
        row["source_id"] = ids[row["source_id"]]
        row["target_id"] = ids[row["target_id"]]


def _def_index(defs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """First record per fqn (classes first where kinds collide)."""
    order = {"class": 0, "interface": 0, "trait": 0, "enum": 0,
             "function": 1, "const": 1}
    index: dict[str, dict[str, Any]] = {}
    for d in defs:
        fqn = d.get("fqn", "")
        if not fqn:
            continue
        existing = index.get(fqn)
        if existing and order.get(existing.get("kind"), 9) <= order.get(d.get("kind"), 9):
            continue
        index[fqn] = d
    return index


def _node_key_for_record(record: dict[str, Any]) -> str:
    return node_key(
        record.get("kind", ""),
        record.get("fqn", ""),
        record.get("name"),
    )


_inspection_service: InspectionService | None = None


def inspection_service() -> InspectionService:
    """Module-level lazy singleton (safe to import without Neo4j up)."""
    global _inspection_service
    if _inspection_service is None:
        _inspection_service = InspectionService()
    return _inspection_service
