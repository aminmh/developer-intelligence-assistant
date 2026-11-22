"""Change impact for one request: the one place the agent tool and the HTTP route both call.

Contract: @.archcore/agent-tools/change-impact-analysis.spec.md (clauses 1 and 15, failure
clauses 1-2). Resolves the revision and its symbol graph collection, loads what the chosen input
needs (definitions and archives for a diff or a revision pair, static routes for attribution),
runs the engine in ``inspection.impact`` and turns graph file paths into project-relative ones.
Authorization stays with the caller: the web server authorizes the project before proxying the
route, and the agent runtime binds a tool call to the chat's authorized project.
"""

from __future__ import annotations

from typing import Any

from inspection.impact import ImpactError, RevisionRef, analyze_diff, analyze_revisions, analyze_symbols

INPUT_KINDS = ("diff", "symbols", "base_revision_id")
MAX_SYMBOLS = 50


class ImpactRequestError(ValueError):
    """The request itself is malformed (HTTP 422)."""


class ImpactUnavailable(LookupError):
    """The revision or its data cannot be used right now (HTTP 409)."""


# --- dependencies (replaced in tests) -----------------------------------------------------------------

def _registry() -> Any:
    from telemetry.registry import default_registry

    return default_registry()


def _graph_query() -> Any:
    from inspection.service import inspection_service

    return inspection_service()._query


def _collection(project_id: str, row: dict[str, Any]) -> str:
    """Snapshot symbol graphs are written to ``sym_`` + the snapshot's collection."""
    from telemetry.snapshot import runtime_reference, storage_id

    reference = runtime_reference(project_id, row)
    if not reference:
        raise ImpactUnavailable(f"Revision {row.get('revision_id')} has no symbol graph.")
    return "sym_snapshot_" + storage_id(project_id, reference, "")


def _source_root(project_id: str, revision_id: str) -> Any:
    from agent_tools.source import resolve_source
    from agent_tools.types import ToolCallRejected, ToolContext

    try:
        return resolve_source(ToolContext(workflow="chat", project_id=project_id, revision_id=revision_id))
    except ToolCallRejected:
        return None


def _static_routes(project_id: str, revision_id: str) -> list[dict[str, Any]]:
    from projects.backend_routes import routes_for_revision

    try:
        return list(routes_for_revision(project_id, revision_id).get("routes") or [])
    except Exception:  # noqa: BLE001 — routes enrich the report; impact stands without them
        return []


def _load_revision(project_id: str, revision_id: str) -> Any:
    from code_quality.rule_code import RevisionUnavailable, load_revision

    try:
        return load_revision(project_id, revision_id)
    except RevisionUnavailable as exc:
        raise ImpactUnavailable(str(exc)) from exc


# --- request ------------------------------------------------------------------------------------------

def _validate(project_id: Any, args: dict[str, Any]) -> tuple[str, Any, dict[str, int]]:
    if not isinstance(project_id, str) or not project_id:
        raise ImpactRequestError("project_id is required.")
    given = [kind for kind in INPUT_KINDS if args.get(kind) not in (None, "", [])]
    if len(given) != 1:
        raise ImpactRequestError("Pass exactly one of diff, symbols or base_revision_id.")
    kind, value = given[0], args[given[0]]
    if kind == "symbols":
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ImpactRequestError("symbols must be a list of symbol names.")
        if len(value) > MAX_SYMBOLS:
            raise ImpactRequestError(f"Pass at most {MAX_SYMBOLS} symbols.")
    elif not isinstance(value, str):
        raise ImpactRequestError(f"{kind} must be a string.")
    limits = {}
    for name in ("depth", "max_nodes"):
        number = args.get(name)
        if number is None:
            continue
        if isinstance(number, bool) or not isinstance(number, int):
            raise ImpactRequestError(f"{name} must be a whole number.")
        limits[name] = number
    return kind, value, limits


def _ready_row(registry: Any, project_id: str, revision_id: str) -> dict[str, Any]:
    if revision_id == "latest":
        row = registry.latest_ready(project_id)
        if row is None:
            raise ImpactUnavailable("This project has no indexed revision yet.")
    else:
        row = registry.get_by_revision_id(project_id, revision_id)
        if row is None:
            raise ImpactUnavailable(f"Revision {revision_id} does not exist for the project.")
    if row.get("state") != "ready":
        raise ImpactUnavailable(f"Revision {row.get('revision_id', revision_id)} is not ready.")
    return row


def run_impact(project_id: str, revision_id: str | None, args: dict[str, Any]) -> dict[str, Any]:
    """The impact report for one request; raises ImpactRequestError or ImpactUnavailable."""
    kind, value, limits = _validate(project_id, args or {})
    registry = _registry()
    row = _ready_row(registry, project_id, revision_id or "latest")
    head = RevisionRef(project_id, row["revision_id"], _collection(project_id, row), ready=True)
    query = _graph_query()
    options: dict[str, Any] = {**limits, "static_routes": _static_routes(project_id, head.revision_id)}
    try:
        if kind == "symbols":
            report = analyze_symbols(query, head, value, **options)
        elif kind == "diff":
            revision = _load_revision(project_id, head.revision_id)
            report = analyze_diff(query, head, value, revision.definitions, **options)
        else:
            base_row = _ready_row(registry, project_id, value)
            base = RevisionRef(project_id, base_row["revision_id"], _collection(project_id, base_row), ready=True)
            head_revision = _load_revision(project_id, head.revision_id)
            base_revision = _load_revision(project_id, base.revision_id)
            report = analyze_revisions(query, head, base, head_definitions=head_revision.definitions,
                                       base_definitions=base_revision.definitions,
                                       head_archive=head_revision.archive_loader,
                                       base_archive=base_revision.archive_loader, **options)
    except ImpactError as exc:
        raise ImpactRequestError(str(exc)) from exc
    _relativize(report, _source_root(project_id, head.revision_id))
    return {"project_id": project_id, **report}


# --- paths --------------------------------------------------------------------------------------------

def _relative(value: Any, root: Any) -> str:
    """Project-relative path, or "" for a path outside the revision (never leaked)."""
    from agent_tools.navigation import _graph_path

    return _graph_path(value, root) or ""


def _relativize(report: dict[str, Any], root: Any) -> None:
    for row in [*report.get("changed", []), *report.get("impacted", []), *report.get("tests", [])]:
        row["path"] = _relative(row.get("path"), root)
        if isinstance(row.get("edge"), dict):
            row["edge"]["path"] = _relative(row["edge"].get("path"), root)
    located = {row["fqn"]: (row["path"], row.get("line")) for row in
               [*report.get("impacted", []), *report.get("changed", [])] if row.get("path")}
    for route in report.get("routes", []):
        path, line = located.get(route.get("fqn"), ("", None))
        route["handler_path"], route["handler_line"] = path, line
