"""Change impact analysis over the revision's Symbol graph.

Contract: @.archcore/agent-tools/change-impact-analysis.spec.md. Given changed symbols, walk
upstream (callers, references, implementers, runtime callers) breadth-first and report what
depends on the change, with the depth, one shortest path back to a change, and the edge
producer of each dependent. Reads only stored graph edges: nothing here runs project code or
talks to a language server, and an edge the graph does not hold is never reported.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from inspection.graph import node_key

DEFAULT_DEPTH = 3
MAX_DEPTH = 6
DEFAULT_MAX_NODES = 300
MAX_NODES = 2_000
TIME_LIMIT_S = 10.0
# Rows one batched upstream read may return; bounds a single hop on hub symbols.
UPSTREAM_ROW_LIMIT = 50_000

# Lower is more precise (clause 8): scip > lsp > parsers > runtime.
_ORIGIN_RANK = {"scip": 0, "lsp": 1, "php-parser": 2, "tree-sitter": 2, "python-ast": 2, "runtime": 3}


class ImpactError(ValueError):
    """The request cannot be answered (revision not ready, bad input)."""


@dataclass(frozen=True)
class RevisionRef:
    """One authorized revision and the graph collection that holds its symbols."""

    project_id: str
    revision_id: str
    collection: str
    ready: bool = True


def _origin_rank(origin: str | None) -> int:
    return _ORIGIN_RANK.get(origin or "", 2)


def _symbol_id(row: dict[str, Any]) -> str:
    return row.get("id") or node_key(row.get("kind", ""), row.get("fqn", ""), row.get("name"))


def _clamp(value: int | None, default: int, maximum: int) -> int:
    if value is None:
        return default
    return max(1, min(int(value), maximum))


def _resolve(query: Any, collection: str, name: str) -> list[dict[str, Any]]:
    """FQN first, then short name, then a function's trailing name (clause 3)."""
    for lookup in (query.resolve_by_fqn, query.resolve_by_name):
        rows = lookup(collection, name)
        if rows:
            return rows
    if "\\" not in name and "::" not in name and "." not in name:
        return query.resolve_function_by_suffix(collection, "\\" + name) or []
    return []


def _changed_row(row: dict[str, Any]) -> dict[str, Any]:
    return {"symbol_id": _symbol_id(row), "fqn": row.get("fqn", ""), "kind": row.get("kind", ""),
            "language": row.get("language"), "path": row.get("file", ""), "line": row.get("line"),
            "change": "named"}


def _empty_report(revision: RevisionRef, depth: int, max_nodes: int, coverage: str) -> dict[str, Any]:
    return {"revision_id": revision.revision_id, "changed": [], "ambiguous": [], "impacted": [],
            "routes": [], "tests": [], "unresolved": [], "truncated": False, "limit_reached": None,
            "coverage": coverage, "depth": depth, "max_nodes": max_nodes}


def analyze_symbols(query: Any, revision: RevisionRef, names: list[str], *, depth: int | None = None,
                    max_nodes: int | None = None, time_limit: float = TIME_LIMIT_S,
                    clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Impact of changing the named symbols on ``revision``."""
    if not revision.ready:
        raise ImpactError(f"Revision {revision.revision_id} is not ready.")
    if not revision.collection:
        raise ImpactError(f"Revision {revision.revision_id} has no symbol graph.")
    depth = _clamp(depth, DEFAULT_DEPTH, MAX_DEPTH)
    max_nodes = _clamp(max_nodes, DEFAULT_MAX_NODES, MAX_NODES)
    report = _empty_report(revision, depth, max_nodes, "complete")
    if not query.available():
        report["coverage"] = "unavailable"
        return report

    changed: dict[str, dict[str, Any]] = {}
    for raw in names:
        name = (raw or "").strip()
        if not name:
            report["unresolved"].append({"input": "", "reason": "empty"})
            continue
        matches = _resolve(query, revision.collection, name)
        if not matches:
            report["unresolved"].append({"input": name, "reason": "not_found"})
            continue
        if len(matches) > 1:
            report["ambiguous"].append({"input": name, "matches": len(matches)})
        for row in matches:
            changed.setdefault(_symbol_id(row), row)
    report["changed"] = [_changed_row(row) for row in changed.values()]
    if not changed:
        return report
    return _walk(query, revision, changed, report, depth, max_nodes, time_limit, clock)


def _best_edges(rows: list[dict[str, Any]], order: dict[str, int]) -> dict[str, dict[str, Any]]:
    """One row per dependent: most precise origin, then the earliest-reached target, then position."""
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _symbol_id(row)
        rank = (_origin_rank(row["ref"].get("origin")), order.get(row["target"], 1 << 30),
                row["ref"].get("file") or "", row["ref"].get("line") or 0)
        if key not in best or rank < best[key][0]:
            best[key] = (rank, row)
    return {key: value[1] for key, value in best.items()}


def _walk(query: Any, revision: RevisionRef, changed: dict[str, dict[str, Any]], report: dict[str, Any],
          depth: int, max_nodes: int, time_limit: float, clock: Callable[[], float]) -> dict[str, Any]:
    started = clock()
    parents: dict[str, str | None] = {key: None for key in changed}
    fqns: dict[str, str] = {key: row.get("fqn", "") for key, row in changed.items()}
    reached: dict[str, dict[str, Any]] = {}
    frontier = sorted(changed, key=lambda key: (changed[key].get("file") or "", changed[key].get("line") or 0, key))
    level = 0
    while frontier:
        if clock() - started > time_limit:
            report["limit_reached"] = "time"
            break
        rows = query.upstream(revision.collection, frontier, UPSTREAM_ROW_LIMIT)
        if rows is None:
            report.update(impacted=[], coverage="unavailable", truncated=False, limit_reached=None)
            return report
        order = {key: index for index, key in enumerate(frontier)}
        fresh = {key: row for key, row in _best_edges(rows, order).items() if key not in parents}
        if not fresh:
            break
        if level == depth:
            report["limit_reached"] = "depth"   # more dependents exist past the depth limit
            break
        level += 1
        ordered = sorted(fresh.items(), key=lambda item: (
            item[1].get("file") or "", item[1].get("line") or 0, item[1].get("fqn") or "", item[0]))
        room = max_nodes - len(reached)
        if len(ordered) > room:
            ordered = ordered[:room]
            report["limit_reached"] = "nodes"
        for key, row in ordered:
            parents[key] = row["target"]
            fqns[key] = row.get("fqn", "")
            reached[key] = {**row, "_depth": level}
        if report["limit_reached"]:
            break
        frontier = [key for key, _ in ordered]

    report["impacted"] = sorted((_impacted_row(key, row, parents, fqns) for key, row in reached.items()),
                                key=_impact_order)
    if report["limit_reached"]:
        report["truncated"] = True
        report["coverage"] = "partial"
    return report


def _via(key: str, parents: dict[str, str | None], fqns: dict[str, str]) -> list[str]:
    path, seen = [], set()
    current: str | None = key
    while current is not None and current not in seen:
        seen.add(current)
        path.append(fqns.get(current, ""))
        current = parents.get(current)
    return path


def _impacted_row(key: str, row: dict[str, Any], parents: dict[str, str | None],
                  fqns: dict[str, str]) -> dict[str, Any]:
    ref = row["ref"]
    origin = ref.get("origin") or "php-parser"
    return {
        "symbol_id": key, "fqn": row.get("fqn", ""), "kind": row.get("kind", ""),
        "language": row.get("language"), "path": row.get("file", ""), "line": row.get("line"),
        "depth": row["_depth"], "via": _via(key, parents, fqns), "origin": origin,
        "edge": {"kind": ref.get("kind"), "relation": ref.get("relation"), "origin": origin,
                 "path": ref.get("file") or row.get("file", ""), "line": ref.get("line")},
        # A runtime edge means the call was seen running; static edges are marked in Phase 2 (clause 12).
        "confirmed": True if origin == "runtime" else None,
        "test": False,
    }


def _impact_order(item: dict[str, Any]) -> tuple:
    """Clause 13: depth, then confirmed first, then path and line."""
    return (item["depth"], 0 if item["confirmed"] else 1, item["path"] or "", item["line"] or 0, item["fqn"])
