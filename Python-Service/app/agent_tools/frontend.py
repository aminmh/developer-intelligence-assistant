"""Model-callable tool that reads the UI map of the current front-end project.

Plan: @.archcore/frontend-assist/frontend-developer-assistance.plan.md (Phase 2).
"""

from __future__ import annotations

from typing import Any

from agent_tools.types import EFFECT_READ_ONLY, SOURCE_BUILTIN, USER_WORKFLOWS, ToolCallRejected, ToolContext, ToolDefinition

_MAX_ROUTES = 80
_MAX_COMPONENTS = 60
_MAX_REQUESTS = 12


def _requests(ui_map: dict[str, Any], component: dict[str, Any]) -> list[str]:
    ids = [*component.get("http_calls", []), *component.get("via_imports", [])]
    calls = [ui_map["http_calls"][index] for index in dict.fromkeys(ids) if index < len(ui_map["http_calls"])]
    return [f"{call['method']} {call['url'] or '(dynamic: ' + call['raw'][:60] + ')'}" for call in calls[:_MAX_REQUESTS]]


def _ui_map(arguments: dict[str, Any], context: ToolContext) -> str:
    from projects.frontend.service import UiMapUnavailable, ui_map_for_revision

    if not context.project_id:
        raise ToolCallRejected("no_project", "The UI map is available only for a selected project.")
    try:
        ui_map = ui_map_for_revision(context.project_id, context.revision_id)
    except (UiMapUnavailable, ValueError) as exc:
        raise ToolCallRejected("no_ui_map", str(exc)) from exc
    if not ui_map.get("framework"):
        return "This project has no Angular, React, or Vue application."
    needle = str(arguments.get("search") or "").casefold()
    components = {component["id"]: component for component in ui_map["components"]}
    label = ui_map.get("meta_framework") or ui_map["framework"]
    lines = [f"{label} application ({ui_map.get('version') or 'version unknown'}): {len(ui_map['routes'])} routes, "
             f"{len(components)} components, {len(ui_map['stores'])} stores, {len(ui_map['http_calls'])} HTTP calls."]

    def describe(component: dict[str, Any]) -> list[str]:
        details = [f"    file: {component['file']}:{component['line']}"]
        if component["uses"]:
            details.append("    uses: " + ", ".join(item.split("#")[-1] for item in component["uses"][:12]))
        if component["stores"]:
            details.append("    stores: " + ", ".join(item.split("#")[-1] for item in component["stores"][:8]))
        requests = _requests(ui_map, component)
        if requests:
            details.append("    requests: " + "; ".join(requests))
        return details

    lines.append("Routes:")
    shown = 0
    for route in ui_map["routes"]:
        text = f"{route['path']} {route['component'] or ''} {route['file'] or ''}"
        if needle and needle not in text.casefold():
            continue
        if shown >= _MAX_ROUTES:
            lines.append(f"  (more than {_MAX_ROUTES} routes; narrow with 'search')")
            break
        shown += 1
        target = f"redirect to {route['redirect']}" if route["redirect"] is not None and not route["component"] \
            else route["component"] or route["file"] or "unresolved"
        lines.append(f"  {route['path']} -> {target}{' (lazy)' if route['lazy'] else ''}")
        component = components.get(route["component_id"] or "")
        if component and needle:
            lines.extend(describe(component))
    if needle:
        matches = [c for c in ui_map["components"] if needle in f"{c['name']} {c['file']}".casefold()][:_MAX_COMPONENTS]
        if matches:
            lines.append("Components:")
            for component in matches:
                lines.append(f"  {component['name']}")
                lines.extend(describe(component))
    lines.extend(f"Note: {note}" for note in ui_map.get("notes", []))
    return "\n".join(lines)


FRONTEND_TOOLS = [
    ToolDefinition(
        name="frontend_ui_map",
        description=(
            "For an Angular, React, or Vue project: list its routes and the component each one renders. With "
            "'search' (a route path, component name, or file), also show what each matching component uses: child "
            "components, stores or services, and the HTTP requests it makes directly or through imported modules. "
            "Use it to find where a screen is built before reading files."
        ),
        input_schema={"type": "object", "properties": {"search": {"type": "string", "maxLength": 100}},
                      "additionalProperties": False},
        effect=EFFECT_READ_ONLY, source=SOURCE_BUILTIN, workflows=USER_WORKFLOWS,
        handler=_ui_map, timeout_seconds=60,
    ),
]
