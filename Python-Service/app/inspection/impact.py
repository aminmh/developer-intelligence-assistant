"""Change impact analysis over the revision's Symbol graph.

Contract: @.archcore/agent-tools/change-impact-analysis.spec.md. Given changed symbols (named,
taken from a unified diff, or found by comparing two revisions), walk upstream (callers,
references, implementers, callers through non-overriding subclasses, runtime callers)
breadth-first and report what depends on the change: each dependent's depth, one shortest path
back to a change, the edge producer, whether the call was seen running, the routes reached and
the tests reached. Reads only stored graph edges: nothing here runs project code or talks to a
language server, and an edge the graph does not hold is never reported.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from inspection.graph import node_key

DEFAULT_DEPTH = 3
MAX_DEPTH = 6
DEFAULT_MAX_NODES = 300
MAX_NODES = 2_000
TIME_LIMIT_S = 10.0
MAX_DIFF_BYTES = 200 * 1024
# Rows one batched upstream read may return; bounds a single hop on hub symbols.
UPSTREAM_ROW_LIMIT = 50_000

# Lower is more precise (clause 8): scip > lsp > parsers > runtime.
_ORIGIN_RANK = {"scip": 0, "lsp": 1, "php-parser": 2, "tree-sitter": 2, "python-ast": 2, "runtime": 3}

# Test files (spec Constraints) [assumption: conventional layouts of the supported languages].
_TEST_DIRS = {"tests", "test", "spec", "__tests__"}
_TEST_FILE = re.compile(
    r"(Test\.php|Tests\.php|_test\.go|_test\.py|_spec\.rb|\.(spec|test)\.[cm]?[jt]sx?)$|^test_[^/]*\.py$")


class ImpactError(ValueError):
    """The request cannot be answered (revision not ready, bad input)."""


@dataclass(frozen=True)
class RevisionRef:
    """One authorized revision and the graph collection that holds its symbols."""

    project_id: str
    revision_id: str
    collection: str
    ready: bool = True


def is_test_path(path: str | None) -> bool:
    parts = [part for part in (path or "").replace("\\", "/").split("/") if part]
    if not parts:
        return False
    return bool(_TEST_DIRS.intersection(parts[:-1])) or bool(_TEST_FILE.search(parts[-1]))


def _origin_rank(origin: str | None) -> int:
    return _ORIGIN_RANK.get(origin or "", 2)


def _symbol_id(row: dict[str, Any]) -> str:
    return row.get("id") or node_key(row.get("kind", ""), row.get("fqn", ""), row.get("name"))


def _clamp(value: int | None, default: int, maximum: int) -> int:
    if value is None:
        return default
    return max(1, min(int(value), maximum))


def _require_ready(revision: RevisionRef) -> None:
    if not revision.ready:
        raise ImpactError(f"Revision {revision.revision_id} is not ready.")
    if not revision.collection:
        raise ImpactError(f"Revision {revision.revision_id} has no symbol graph.")


def _empty_report(revision: RevisionRef, depth: int, max_nodes: int) -> dict[str, Any]:
    return {"revision_id": revision.revision_id, "changed": [], "ambiguous": [], "impacted": [],
            "routes": [], "tests": [], "unresolved": [], "truncated": False, "limit_reached": None,
            "coverage": "complete", "coverage_reasons": [], "producers": None, "runtime": {"observed": False},
            "depth": depth, "max_nodes": max_nodes}


def _changed_row(row: dict[str, Any], change: str, revision: RevisionRef) -> dict[str, Any]:
    return {"symbol_id": _symbol_id(row), "fqn": row.get("fqn", ""), "kind": row.get("kind", ""),
            "language": row.get("language"), "path": row.get("file", ""), "line": row.get("line"),
            "change": change, "revision_id": revision.revision_id}


# --- inputs -----------------------------------------------------------------------------------------

def _resolve(query: Any, collection: str, name: str) -> list[dict[str, Any]]:
    """FQN first, then short name, then a function's trailing name (clause 3)."""
    for lookup in (query.resolve_by_fqn, query.resolve_by_name):
        rows = lookup(collection, name)
        if rows:
            return rows
    if "\\" not in name and "::" not in name and "." not in name:
        return query.resolve_function_by_suffix(collection, "\\" + name) or []
    return []


def analyze_symbols(query: Any, revision: RevisionRef, names: list[str], *, depth: int | None = None,
                    max_nodes: int | None = None, static_routes: list[dict[str, Any]] | None = None,
                    time_limit: float = TIME_LIMIT_S, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Impact of changing the named symbols on ``revision``."""
    _require_ready(revision)
    report = _empty_report(revision, _clamp(depth, DEFAULT_DEPTH, MAX_DEPTH),
                           _clamp(max_nodes, DEFAULT_MAX_NODES, MAX_NODES))
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
    report["changed"] = [_changed_row(row, "named", revision) for row in changed.values()]
    return _run(query, report, [(revision, changed)], static_routes, time_limit, clock)


def analyze_diff(query: Any, revision: RevisionRef, diff: str, definitions: dict[str, dict], *,
                 depth: int | None = None, max_nodes: int | None = None,
                 static_routes: list[dict[str, Any]] | None = None, time_limit: float = TIME_LIMIT_S,
                 clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Impact of a unified diff against ``revision`` (clause 2); ``definitions`` by symbol id."""
    from code_quality.rule_code import changed_symbols

    _require_ready(revision)
    if not isinstance(diff, str) or not diff.strip():
        raise ImpactError("Pass a unified diff (for example from git diff).")
    if len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
        raise ImpactError("The diff is larger than 200 KB. Check a smaller change.")
    hunks, touched = changed_symbols(diff, definitions)
    if not hunks:
        raise ImpactError("No diff hunks were found. Pass a unified diff (git diff output).")
    report = _empty_report(revision, _clamp(depth, DEFAULT_DEPTH, MAX_DEPTH),
                           _clamp(max_nodes, DEFAULT_MAX_NODES, MAX_NODES))
    for hunk in hunks:
        if not hunk["symbol_ids"]:
            report["unresolved"].append({"input": f"{hunk['file']}:{hunk['start']}-{hunk['end']}",
                                         "reason": "no_symbol"})
    if not query.available():
        report["coverage"] = "unavailable"
        return report
    rows = query.resolve_by_symbol_ids(revision.collection, touched)
    changed = {_symbol_id(row): row for row in rows}
    found = {row.get("symbol_id") for row in rows}
    report["unresolved"] += [{"input": sid, "reason": "not_in_graph"} for sid in touched if sid not in found]
    report["changed"] = [_changed_row(row, "modified", revision) for row in changed.values()]
    return _run(query, report, [(revision, changed)], static_routes, time_limit, clock)


def analyze_revisions(query: Any, head: RevisionRef, base: RevisionRef, *, head_definitions: dict[str, dict],
                      base_definitions: dict[str, dict], head_archive: Callable[[], bytes],
                      base_archive: Callable[[], bytes], depth: int | None = None, max_nodes: int | None = None,
                      static_routes: list[dict[str, Any]] | None = None, time_limit: float = TIME_LIMIT_S,
                      clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Impact of everything that changed from ``base`` to ``head`` (clause 5).

    Symbols match across revisions by their canonical ``symbol_id``. Added and modified symbols
    are walked in the head graph; removed ones in the base graph, where their callers still are.
    """
    from code_quality.rule_code import symbol_hashes

    _require_ready(head)
    _require_ready(base)
    report = _empty_report(head, _clamp(depth, DEFAULT_DEPTH, MAX_DEPTH),
                           _clamp(max_nodes, DEFAULT_MAX_NODES, MAX_NODES))
    report["base_revision_id"] = base.revision_id
    if not query.available():
        report["coverage"] = "unavailable"
        return report
    if not base_definitions:
        report.update(coverage="unavailable", unavailable_revision=base.revision_id)
        return report
    common = sorted(set(head_definitions) & set(base_definitions))
    try:
        head_hashes = symbol_hashes(head_archive(), head_definitions, common)
        base_hashes = symbol_hashes(base_archive(), base_definitions, common)
    except Exception:  # noqa: BLE001 — a missing archive leaves the comparison unavailable
        report.update(coverage="unavailable", unavailable_revision=base.revision_id)
        return report
    modified = [sid for sid in common if head_hashes.get(sid) != base_hashes.get(sid)]
    added = sorted(set(head_definitions) - set(base_definitions))
    removed = sorted(set(base_definitions) - set(head_definitions))
    kinds = {**{sid: "modified" for sid in modified}, **{sid: "added" for sid in added},
             **{sid: "removed" for sid in removed}}
    head_rows = query.resolve_by_symbol_ids(head.collection, [*modified, *added])
    base_rows = query.resolve_by_symbol_ids(base.collection, removed)
    head_changed = {_symbol_id(row): row for row in head_rows}
    base_changed = {_symbol_id(row): row for row in base_rows}
    report["changed"] = ([_changed_row(row, kinds.get(row.get("symbol_id"), "modified"), head) for row in head_rows]
                         + [_changed_row(row, "removed", base) for row in base_rows])
    walks = [(head, head_changed)] + ([(base, base_changed)] if base_changed else [])
    return _run(query, report, walks, static_routes, time_limit, clock)


# --- walk -------------------------------------------------------------------------------------------

def _run(query: Any, report: dict[str, Any], walks: list[tuple[RevisionRef, dict[str, dict[str, Any]]]],
         static_routes: list[dict[str, Any]] | None, time_limit: float,
         clock: Callable[[], float]) -> dict[str, Any]:
    head = walks[0][0]
    report["runtime"] = {"observed": query.runtime_observed(head.collection)}
    report["producers"] = query.producer_coverage(head.collection)
    if not any(changed for _, changed in walks):
        _producer_reasons(report)
        return report
    started = clock()
    merged: dict[str, dict[str, Any]] = {}
    route_rows: list[dict[str, Any]] = []
    for revision, changed in walks:
        if not changed:
            continue
        observed = report["runtime"]["observed"] if revision is head else False
        result = _walk(query, revision, changed, report, observed, started, time_limit, clock)
        if result is None:
            report.update(impacted=[], routes=[], tests=[], coverage="unavailable", truncated=False,
                          limit_reached=None)
            return report
        items, ids_by_depth = result
        for item in items:
            key = item.get("canonical") or item["fqn"]
            if key not in merged or item["depth"] < merged[key]["depth"]:
                merged[key] = item
        if revision is head:
            route_rows += _runtime_routes(query, revision, ids_by_depth)
        if report["limit_reached"]:
            break
    items = sorted(merged.values(), key=_impact_order)
    if len(items) > report["max_nodes"]:
        items = items[:report["max_nodes"]]
        report["limit_reached"] = report["limit_reached"] or "nodes"
    report["impacted"] = [{k: v for k, v in item.items() if k != "canonical"} for item in items]
    report["tests"] = [{"fqn": i["fqn"], "path": i["path"], "line": i["line"], "depth": i["depth"]}
                       for i in report["impacted"] if i["test"]]
    report["routes"] = _routes(route_rows, report, static_routes)
    if report["limit_reached"]:
        report["truncated"] = True
        report["coverage"] = "partial"
        report["coverage_reasons"].append(f"limit_{report['limit_reached']}")
    _producer_reasons(report)
    return report


def _producer_reasons(report: dict[str, Any]) -> None:
    """F8: a producer that ran on files but did not finish leaves coverage partial."""
    for name, entry in sorted((report.get("producers") or {}).items()):
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        if status == "partial" or (status == "unavailable" and entry.get("languages")):
            report["coverage_reasons"].append(f"{name}_{status}")
            if report["coverage"] == "complete":
                report["coverage"] = "partial"


def _best_edges(rows: list[dict[str, Any]], order: dict[str, int]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """One row per dependent (most precise origin, earliest target, position) and those seen running."""
    best: dict[str, tuple] = {}
    ran: set[str] = set()
    for row in rows:
        key = _symbol_id(row)
        if row["ref"].get("origin") == "runtime":
            ran.add(key)
        rank = (_origin_rank(row["ref"].get("origin")), order.get(row["target"], 1 << 30),
                row["ref"].get("file") or "", row["ref"].get("line") or 0)
        if key not in best or rank < best[key][0]:
            best[key] = (rank, row)
    return {key: value[1] for key, value in best.items()}, ran


def _walk(query: Any, revision: RevisionRef, changed: dict[str, dict[str, Any]], report: dict[str, Any],
          observed: bool, started: float, time_limit: float,
          clock: Callable[[], float]) -> tuple[list[dict[str, Any]], dict[int, list[str]]] | None:
    parents: dict[str, str | None] = {key: None for key in changed}
    fqns: dict[str, str] = {key: row.get("fqn", "") for key, row in changed.items()}
    kinds: dict[str, str] = {key: row.get("kind", "") for key, row in changed.items()}
    reached: dict[str, dict[str, Any]] = {}
    ids_by_depth: dict[int, list[str]] = {0: list(changed)}
    frontier = sorted(changed, key=lambda key: (changed[key].get("file") or "", changed[key].get("line") or 0, key))
    level = 0
    depth, max_nodes = report["depth"], report["max_nodes"]
    while frontier:
        if clock() - started > time_limit:
            report["limit_reached"] = "time"
            break
        rows = query.upstream(revision.collection, frontier, UPSTREAM_ROW_LIMIT)
        methods = [key for key in frontier if kinds.get(key) == "method"]
        inherited = query.inherited_callers(revision.collection, methods, UPSTREAM_ROW_LIMIT) if methods else []
        if rows is None or inherited is None:
            return None
        order = {key: index for index, key in enumerate(frontier)}
        best, ran = _best_edges([*rows, *inherited], order)
        fresh = {key: row for key, row in best.items() if key not in parents}
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
        next_frontier = []
        for key, row in ordered:
            parents[key] = row["target"]
            fqns[key] = row.get("fqn", "")
            kinds[key] = row.get("kind", "")
            test = is_test_path(row.get("file"))
            reached[key] = {**row, "_depth": level, "_ran": key in ran, "_test": test}
            if not test:                         # clause 11: tests are leaves
                next_frontier.append(key)
        ids_by_depth[level] = [key for key, _ in ordered]
        if report["limit_reached"]:
            break
        frontier = next_frontier
    items = [_impacted_row(key, row, parents, fqns, revision, observed) for key, row in reached.items()]
    return items, ids_by_depth


def _via(key: str, parents: dict[str, str | None], fqns: dict[str, str]) -> list[str]:
    path, seen = [], set()
    current: str | None = key
    while current is not None and current not in seen:
        seen.add(current)
        path.append(fqns.get(current, ""))
        current = parents.get(current)
    return path


def _impacted_row(key: str, row: dict[str, Any], parents: dict[str, str | None], fqns: dict[str, str],
                  revision: RevisionRef, observed: bool) -> dict[str, Any]:
    ref = row["ref"]
    origin = ref.get("origin") or "php-parser"
    if row["_ran"]:
        confirmed: bool | None = True          # seen running on this revision
    else:
        confirmed = False if observed else None  # clause 12: unconfirmed, never absent
    edge = {"kind": ref.get("kind"), "relation": ref.get("relation"), "origin": origin,
            "path": ref.get("file") or row.get("file", ""), "line": ref.get("line")}
    if ref.get("via_class"):
        edge["via_class"] = ref["via_class"]
    return {
        "symbol_id": key, "fqn": row.get("fqn", ""), "kind": row.get("kind", ""),
        "language": row.get("language"), "path": row.get("file", ""), "line": row.get("line"),
        "depth": row["_depth"], "via": _via(key, parents, fqns), "origin": origin, "edge": edge,
        "confirmed": confirmed, "test": row["_test"], "revision_id": revision.revision_id,
        "canonical": row.get("symbol_id") or row.get("fqn"),
    }


def _impact_order(item: dict[str, Any]) -> tuple:
    """Clause 13: depth, then confirmed first, then path and line."""
    return (item["depth"], 0 if item["confirmed"] else 1, item["path"] or "", item["line"] or 0, item["fqn"])


# --- routes -----------------------------------------------------------------------------------------

def _runtime_routes(query: Any, revision: RevisionRef, ids_by_depth: dict[int, list[str]]) -> list[dict[str, Any]]:
    depth_of = {key: d for d, keys in sorted(ids_by_depth.items(), reverse=True) for key in keys}
    rows = query.routes_for(revision.collection, list(depth_of))
    return [{**row, "depth": depth_of.get(row["target"])} for row in rows]


def _action_fqn(action: str) -> str | None:
    """Laravel ``Class@method``, Symfony ``Class::method``, or an invokable controller class."""
    value = (action or "").strip().lstrip("\\")
    if not value or value == "Closure" or value.startswith(("view:", "redirect:")):
        return None
    value = value.replace("@", "::")
    return value if "::" in value else value + "::__invoke"


def _routes(runtime_rows: list[dict[str, Any]], report: dict[str, Any],
            static_routes: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Clause 10: routes handled by a changed (depth 0) or impacted symbol."""
    by_id: dict[str, tuple[str, int]] = {c["symbol_id"]: (c["fqn"], 0) for c in report["changed"]}
    by_id.update({i["symbol_id"]: (i["fqn"], i["depth"]) for i in report["impacted"]})
    by_fqn: dict[str, int] = {}
    for fqn, depth in by_id.values():
        by_fqn[fqn] = min(depth, by_fqn.get(fqn, depth))
    out: dict[tuple, dict[str, Any]] = {}
    for row in runtime_rows:
        if row["target"] in by_id:
            fqn, depth = by_id[row["target"]]
            out[("runtime", row["method"], row["route"], fqn)] = {
                "source": "runtime", "method": row["method"], "path": row["route"], "fqn": fqn, "depth": depth}
    for route in static_routes or []:
        target = _action_fqn(str(route.get("action") or ""))
        if not target:
            continue
        match = target if target in by_fqn else next(
            (fqn for fqn in sorted(by_fqn) if fqn.endswith("\\" + target)), None)
        if match is None:
            continue
        method, path = str(route.get("method") or ""), str(route.get("path") or "")
        out[("static", method, path, match)] = {"source": "static", "method": method, "path": path,
                                                "fqn": match, "depth": by_fqn[match]}
    return sorted(out.values(), key=lambda r: (r["depth"], r["path"], r["method"], r["source"]))
