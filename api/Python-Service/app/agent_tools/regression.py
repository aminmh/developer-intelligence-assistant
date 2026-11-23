"""The ``regression_check`` agent tool (@.archcore/regression/regression-check.spec.md clause 22).

Compares the chat's revision (candidate) with a base revision. It returns the newest finished
check of that pair, or starts a static check in the background and says so; it never sends HTTP to
the developer's app, so it stays read-only for the project.
"""

from __future__ import annotations

from typing import Any

from agent_tools.types import EFFECT_READ_ONLY, SOURCE_BUILTIN, ToolCallRejected, ToolContext, ToolDefinition

WORKFLOWS = frozenset({"chat", "code_refactor"})
MAX_ITEMS = 80


def _store() -> Any:
    from regression.store import RegressionStore

    return RegressionStore()


def _start(project_id: str, base: str, candidate: str, store: Any) -> dict[str, Any]:
    from regression.service import start_static

    return start_static(project_id, base, candidate, store=store)


def _items(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Cited rows: each symbol with path and line, each route with method and path plus its handler."""
    items = []
    for row in report.get("symbols", []):
        reasons = next((e.get("reasons") for e in row.get("evidence", []) if e.get("reasons")), [])
        items.append({"kind": "symbol", "symbol": row.get("fqn"), "path": row.get("path"), "line": row.get("line"),
                      "change": row.get("change"), "verdict": row.get("verdict"), "coverage": row.get("coverage"),
                      "reasons": reasons})
    for route in report.get("routes", []):
        replay = route.get("replay") or {}
        items.append({"kind": "route", "route": f"{route.get('method', '')} {route.get('path', '')}".strip(),
                      "path": route.get("handler_path"), "line": route.get("handler_line"),
                      "verdict": route.get("verdict"), "coverage": route.get("coverage"),
                      "depends_on": route.get("depends_on"),
                      "replay_differences": replay.get("differences", [])[:3] if replay else None,
                      "span_flags": (route.get("spans") or {}).get("flags")})
    for other in report.get("replay_other", []):
        if other.get("equal") is False:  # replayed requests outside the changed routes that still differ
            items.append({"kind": "request", "route": f"{other['method']} {other['path']}", "path": None, "line": None,
                          "verdict": other["verdict"], "replay_differences": other.get("differences", [])[:3]})
    return items[:MAX_ITEMS]


def regression_check(args: dict[str, Any], context: ToolContext, *, store: Any = None,
                     start: Any = _start) -> dict[str, Any]:
    from regression.model import JUDGMENT_LABEL, verdict_counts
    from regression.service import RegressionBusy, RegressionRequestError, RegressionUnavailable

    if not (context.project_id and context.revision_id):
        return {"state": "unavailable", "items": [],
                "detail": "A regression check needs an indexed project revision to compare."}
    base = args.get("base_revision_id")
    if not isinstance(base, str) or not base:
        raise ToolCallRejected("invalid_arguments", "base_revision_id is required.")
    store = store or _store()
    pair = [c for c in store.list(context.project_id)
            if c["base_revision_id"] == base and c["candidate_revision_id"] == context.revision_id]
    done = next((c for c in pair if c["state"] == "done"), None)
    if done is None:
        running = next((c for c in pair if c["state"] in ("pending", "running")), None)
        if running is None and not args.get("start", True):
            return {"state": "none", "items": [], "detail": "No check exists for this revision pair."}
        if running is None:
            try:
                running = start(context.project_id, base, context.revision_id, store)
            except RegressionRequestError as exc:
                raise ToolCallRejected("invalid_arguments", str(exc)) from exc
            except (RegressionUnavailable, RegressionBusy) as exc:
                return {"state": "unavailable", "items": [], "detail": str(exc)}
        return {"state": running["state"], "check_id": running["check_id"], "items": [],
                "detail": "The static check is running; call regression_check again in a minute for the report."}
    report = done["report"]
    return {"state": "done", "check_id": done["check_id"], "tier": report.get("tier"),
            "coverage": report.get("coverage"), "counts": verdict_counts(report),
            "uncovered": report.get("uncovered", [])[:MAX_ITEMS], "partial": report.get("partial", []),
            "contract_changes": report.get("contract_changes", [])[:20], "notes": report.get("notes", []),
            "package_changes": [{**{k: row.get(k) for k in ("name", "from", "to", "change", "dev")},
                                 "notes": {"coverage": (row.get("notes") or {}).get("coverage"),
                                           "versions_found": (row.get("notes") or {}).get("versions_found", []),
                                           "cites": [f"{e.get('section')}: {e.get('path_or_url')}"
                                                     for e in (row.get("notes") or {}).get("excerpts", [])[:5]]}}
                                for row in report.get("package_changes", [])[:20]],
            "judgment_label": JUDGMENT_LABEL, "items": _items(report),
            "characterization_tests": (
                "Route tests from the recorded baseline and a replayable request file can be downloaded in Code Quality "
                "> Regression > Characterization tests." if store.latest_run(context.project_id, done["check_id"], "baseline")
                else "Record a baseline in Code Quality > Regression to get route tests that pin the base version's responses.")}


REGRESSION_TOOLS = [
    ToolDefinition(
        name="regression_check",
        description=(
            "Check whether a refactor or upgrade changed behavior: compare this revision with an older base "
            "revision. Returns changed symbols and affected routes, each with a verdict (unchanged, "
            "changed_expected, suspected_regression, not_covered), its path and line, and what covers it "
            "(tests, runtime). Equivalence verdicts are model judgments, not proof; report uncovered items "
            "as unverified. The first call starts the check; call again for the report."),
        input_schema={"type": "object", "properties": {
            "base_revision_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "start": {"type": "boolean", "default": True,
                      "description": "Start a check when none exists for this pair."}},
            "required": ["base_revision_id"], "additionalProperties": False},
        effect=EFFECT_READ_ONLY, source=SOURCE_BUILTIN, workflows=WORKFLOWS, handler=regression_check,
        timeout_seconds=30,
    ),
]
