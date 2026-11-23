"""Back-end code changes that may affect consumers while the contract document stays the same.

Spec: @.archcore/frontend-assist/contract-change-notifications.spec.md (code drift).

A back-end developer can change the API without touching the OpenAPI file. Two revisions of the
provider project are compared:

- a documented route that disappears from the code → ``route_removed_in_code`` (breaking);
- a new route under the API that the contract does not describe → ``undocumented_route_added`` (safe:
  nothing a consumer uses breaks, but the contract is now behind the code);
- the handler of a documented operation changed while the contract did not →
  ``handler_changed_contract_unchanged`` (risky: the response may have changed).

Response shapes inside handler code are not inferred; a handler change is only a signal.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from api_contracts.breaking import MAX_ITEMS
from api_contracts.conformance import ContractIndex, _segments, infer_prefix

_MAX_HANDLER_BYTES = 2_000_000


def _key(route: dict[str, Any]) -> tuple[str, str]:
    return route["method"], route["path"]


def _operations_for(route: dict[str, Any], index: ContractIndex, prefix: str | None) -> list[str]:
    path = index.match(route["path"], prefix)
    if path is None:
        return []
    methods = index.paths[path]
    if route["method"] == "ANY":
        return sorted(methods.values())
    return [methods[route["method"]]] if route["method"] in methods else []


def handler_file(route: dict[str, Any], framework: str | None) -> str | None:
    """The file that holds a route's handler, relative to the back-end application folder."""
    if framework == "laravel":
        action = str(route.get("action") or "")
        if "@" not in action and "::" not in action:
            return None  # a closure lives in the route file itself
        cls = action.split("@", 1)[0].split("::", 1)[0].lstrip("\\")
        if not cls.startswith("App\\"):
            return None
        return "app/" + cls[4:].replace("\\", "/") + ".php"
    return route.get("file")


def _digest(root: Path, relative: str) -> str | None:
    path = (root / relative).resolve()
    try:
        if root.resolve() not in path.parents or path.stat().st_size > _MAX_HANDLER_BYTES:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def changed_handlers(old_root: Path, new_root: Path, routes: list[dict[str, Any]], framework: str | None) -> set[str]:
    """Handler files (and route files) whose content differs between two revisions' application folders."""
    files = {name for route in routes for name in (handler_file(route, framework), route.get("file")) if name}
    return {name for name in files if _digest(old_root, name) != _digest(new_root, name)}


def drift_items(old_routes: dict[str, Any], new_routes: dict[str, Any], model: dict[str, Any], *,
                changed_files: set[str] | None = None, contract_changed: bool = False) -> list[dict[str, Any]]:
    """Change items for consumers of ``model`` caused by the code moving from ``old_routes`` to ``new_routes``."""
    index = ContractIndex(model)
    framework = new_routes.get("framework") or old_routes.get("framework")
    old_list, new_list = old_routes.get("routes") or [], new_routes.get("routes") or []
    as_calls = [{"id": i, "url": r["path"], "method": r["method"]} for i, r in enumerate([*old_list, *new_list])]
    prefix = infer_prefix(index, as_calls)
    old_by_key, new_by_key = {_key(r): r for r in old_list}, {_key(r): r for r in new_list}
    still_served = {key for route in new_list for key in _operations_for(route, index, prefix)}
    items: list[dict[str, Any]] = []

    for key in sorted(set(old_by_key) - set(new_by_key)):
        route = old_by_key[key]
        for operation in _operations_for(route, index, prefix):
            if operation in still_served:
                continue  # served by another route now (moved, or an ANY route)
            items.append({"severity": "breaking", "code": "route_removed_in_code", "operation": operation,
                          "pointer": "", "location": {"file": route.get("file"), "line": route.get("line")},
                          "message": f"The back end no longer serves {operation}, but the contract still describes it."})
    for key in sorted(set(new_by_key) - set(old_by_key)):
        route = new_by_key[key]
        if not _operations_for(route, index, prefix) and (route.get("api") or _under_api(route, index, prefix)):
            items.append({"severity": "safe", "code": "undocumented_route_added", "operation": None,
                          "pointer": f"{key[0]} {key[1]}",
                          "location": {"file": route.get("file"), "line": route.get("line")},
                          "message": f"The back end now serves {key[0]} {key[1]}, which the contract does not describe."})
    if changed_files and not contract_changed:
        flagged: set[str] = set()
        for route in new_list:
            if _key(route) not in old_by_key:
                continue
            files = {handler_file(route, framework), route.get("file")} & changed_files
            if not files:
                continue
            for operation in _operations_for(route, index, prefix):
                if operation in flagged:
                    continue
                flagged.add(operation)
                items.append({"severity": "risky", "code": "handler_changed_contract_unchanged", "operation": operation,
                              "pointer": "", "location": {"file": sorted(files)[0], "line": None},
                              "message": f"The code that answers {operation} changed but the contract did not; "
                                         "its response may have changed."})
    order = {"breaking": 0, "risky": 1, "safe": 2}
    items.sort(key=lambda item: (order[item["severity"]], item["operation"] or "", item["pointer"], item["code"]))
    return items[:MAX_ITEMS]


def _under_api(route: dict[str, Any], index: ContractIndex, prefix: str | None) -> bool:
    segments = _segments(route["path"])
    for base in index.bases:
        literal = [part for part in base if part != "{}"]
        if literal and segments[: len(literal)] == literal:
            return True
    return bool(prefix) and segments[: len(_segments(prefix))] == _segments(prefix)
