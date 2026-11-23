"""Compare a front-end project with a contract version, and say what a contract change touches.

Spec: @.archcore/frontend-assist/contract-conformance-findings.spec.md.

Both reports are pure functions of two immutable inputs: the UI map of a
revision (``projects.frontend.ui_map``) and a contract version model
(``api_contracts.model``). Nothing is stored.

The UI map is extracted from syntax, so it sometimes sees only part of a
request. Each request therefore says which of its parts were fully visible, and
this module reports something as missing only when it was. A request that
matches nothing is an ``error``; anything inferred from partial information is a
``warning`` or ``info``.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any

from api_contracts.model import diff_models

CHECKER_VERSION = 1
MAX_FINDINGS = 2000
_PLACEHOLDER = re.compile(r"\{[^{}]*\}")
_HOST = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/]*")
_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


# ── path matching ────────────────────────────────────────────────────────────

def _segments(path: str) -> list[str]:
    """Path segments with every dynamic segment written as ``{}``."""
    path = _HOST.sub("", path.split("?", 1)[0])
    return ["{}" if _PLACEHOLDER.search(part) else part for part in path.split("/") if part]


class ContractIndex:
    """A contract version prepared for matching request addresses."""

    def __init__(self, model: dict[str, Any]) -> None:
        self.model = model
        self.paths: dict[str, dict[str, str]] = {}
        for key, operation in model["operations"].items():
            self.paths.setdefault(operation["path"], {})[operation["method"]] = key
        self._path_segments = {path: _segments(path) for path in self.paths}
        self.bases = [segments for segments in (_segments(base) for base in model.get("base_paths") or []) if segments]

    def _score(self, call: list[str], path: list[str]) -> tuple[float, bool] | None:
        """(score, weak). Weak means a run-time variable stands where the contract has fixed text."""
        if len(call) < len(path):
            return None
        score, weak = 0.0, False
        for mine, theirs in zip(call[len(call) - len(path):], path):
            if theirs == "{}":
                score += 2 if mine == "{}" else 1     # a literal passed where the contract has a parameter
            elif mine == theirs:
                score += 2
            elif mine == "{}":
                score, weak = score + 0.5, True        # a variable where the contract has fixed text: possible, weak
            else:
                return None
        return score, weak

    def _prefix_declared(self, prefix: list[str]) -> bool:
        """True when what precedes the contract path is nothing, run-time variables, or a declared base path."""
        literal = [part for part in prefix if part != "{}"]
        if not literal:
            return True
        return any(literal == [part for part in base if part != "{}"][-len(literal):] for base in self.bases)

    def candidates(self, url: str) -> list[tuple[float, bool, str, str, bool]]:
        """(score, prefix declared, prefix text, contract path, weak) for every path the address could be, best first."""
        call = _segments(url)
        found = []
        for path, segments in self._path_segments.items():
            scored = self._score(call, segments)
            if scored is None or (not segments and call):
                continue
            prefix = call[: len(call) - len(segments)]
            found.append((scored[0], self._prefix_declared(prefix), "/" + "/".join(prefix) if prefix else "", path, scored[1]))
        return sorted(found, key=lambda item: (item[1], item[0]), reverse=True)

    def match(self, url: str, accepted_prefix: str | None = None) -> str | None:
        """The contract path for a request address, or None."""
        for _score, declared, prefix, path, weak in self.candidates(url):
            # An undeclared prefix is accepted only for a firm match, never on a guess about a variable.
            if declared or (accepted_prefix is not None and prefix == accepted_prefix and not weak):
                return path
        return None


def infer_prefix(index: ContractIndex, calls: list[dict[str, Any]]) -> str | None:
    """A literal prefix (for example ``/api/v1``) that requests share but the contract does not declare.

    Many contracts omit ``servers`` / ``basePath``. Without this, every request of such a project
    would be reported as unknown. The prefix is accepted only when most otherwise-unmatched
    requests agree on it.
    """
    if index.bases:
        # The contract says where the API lives. Guessing a second prefix would match unrelated
        # routes that merely end the same way (/api/v1/admin/sizes is not /api/v1/sizes).
        return None
    prefixes: Counter[str] = Counter()
    undeclared = 0
    for call in calls:
        if not call.get("url") or index.match(call["url"]) is not None:
            continue
        options = [option for option in index.candidates(call["url"]) if not option[4]]
        if options:
            undeclared += 1
            prefixes[options[0][2]] += 1
    if not prefixes:
        return None
    prefix, count = prefixes.most_common(1)[0]
    return prefix if prefix and count >= 2 and count * 2 > undeclared else None


# ── schemas ──────────────────────────────────────────────────────────────────

def _resolve(schema: Any, schemas: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Follow references and merge ``allOf`` so the object's own properties are visible."""
    if not isinstance(schema, dict) or depth > 8:
        return {}
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/schemas/"):
        return _resolve(schemas.get(ref[10:]), schemas, depth + 1)
    if isinstance(schema.get("allOf"), list):
        merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for part in schema["allOf"]:
            resolved = _resolve(part, schemas, depth + 1)
            merged["properties"].update(resolved.get("properties") or {})
            merged["required"].extend(resolved.get("required") or [])
            if resolved.get("additionalProperties"):
                merged["additionalProperties"] = resolved["additionalProperties"]
        return merged
    return schema


def _object_shape(schema: Any, schemas: dict[str, Any]) -> tuple[set[str], set[str], bool] | None:
    """(property names, required names, open to other properties) of an object schema, or None."""
    resolved = _resolve(schema, schemas)
    properties = resolved.get("properties")
    if not isinstance(properties, dict) or not properties:
        return None
    return set(properties), set(resolved.get("required") or []), bool(resolved.get("additionalProperties"))


def _body_schema(operation: dict[str, Any]) -> Any:
    content = (operation.get("request_body") or {}).get("content") or {}
    for media, schema in content.items():
        if "json" in media:
            return schema
    return next(iter(content.values()), None)


def _schema_refs(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/schemas/"):
            found.add(ref[10:])
        for value in node.values():
            _schema_refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _schema_refs(value, found)


def _referenced_schemas(operation: dict[str, Any], schemas: dict[str, Any]) -> set[str]:
    """Every schema an operation reaches, through references of references."""
    found: set[str] = set()
    _schema_refs(operation, found)
    pending = list(found)
    while pending:
        nested: set[str] = set()
        _schema_refs(schemas.get(pending.pop()), nested)
        pending.extend(nested - found)
        found |= nested
    return found


# ── consumer check ───────────────────────────────────────────────────────────

def _finding(check: str, code: str, severity: str, message: str, *, operation: str | None = None,
             location: dict[str, Any] | None = None, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    location = location or {}
    identity = f"{check}|{code}|{operation}|{location.get('file')}|{location.get('line')}|{sorted((evidence or {}).items())}"
    return {"finding_id": "cf_" + hashlib.sha256(identity.encode()).hexdigest()[:20], "check": check, "code": code,
            "severity": severity, "message": message, "operation": operation, "location": location,
            "evidence": evidence or {}}


def _where(call: dict[str, Any]) -> dict[str, Any]:
    return {"file": call["file"], "line": call["line"], "symbol": call.get("symbol") or ""}


def _check_call(call: dict[str, Any], key: str, operation: dict[str, Any], schemas: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    where = _where(call)
    if operation["deprecated"]:
        findings.append(_finding("consumer", "deprecated_operation", "warning",
                                 f"{key} is marked deprecated in the contract.", operation=key, location=where))
    declared = {p["name"]: p for p in operation["parameters"] if p["in"] == "query"}
    for name in call.get("query") or []:
        if name not in declared:
            findings.append(_finding("consumer", "unknown_query_parameter", "warning",
                                     f"Query parameter '{name}' is not defined for {key}.", operation=key,
                                     location=where, evidence={"parameter": name}))
    if call.get("query_complete"):
        for name, parameter in declared.items():
            if parameter["required"] and name not in (call.get("query") or []):
                findings.append(_finding("consumer", "missing_required_parameter", "warning",
                                         f"{key} requires query parameter '{name}', which this request does not send.",
                                         operation=key, location=where, evidence={"parameter": name}))
    body = operation.get("request_body")
    if body is None:
        if call.get("has_body") and call["method"] in ("POST", "PUT", "PATCH") and call.get("body_keys"):
            findings.append(_finding("consumer", "unexpected_request_body", "warning",
                                     f"{key} defines no request body, but this request sends one.",
                                     operation=key, location=where))
        return findings
    if body.get("required") and not call.get("has_body"):
        findings.append(_finding("consumer", "missing_request_body", "warning",
                                 f"{key} requires a request body, which this request does not send.",
                                 operation=key, location=where))
    shape = _object_shape(_body_schema(operation), schemas)
    if shape and call.get("body_keys") is not None:
        properties, required, open_ended = shape
        if not open_ended:
            for name in call["body_keys"]:
                if name not in properties:
                    findings.append(_finding("consumer", "unknown_body_property", "warning",
                                             f"Body property '{name}' is not in the request schema of {key}.",
                                             operation=key, location=where, evidence={"property": name}))
        if call.get("body_complete"):
            for name in sorted(required - set(call["body_keys"])):
                findings.append(_finding("consumer", "missing_required_property", "warning",
                                         f"{key} requires body property '{name}', which this request does not send.",
                                         operation=key, location=where, evidence={"property": name}))
    return findings


def _check_models(ui_map: dict[str, Any], schemas: dict[str, Any]) -> list[dict[str, Any]]:
    by_name = {name.casefold(): name for name in schemas}
    findings = []
    for model in ui_map.get("models") or []:
        name = model["name"]
        stripped = name[1:] if len(name) > 2 and name[0] == "I" and name[1].isupper() else name  # IOrder -> Order
        schema_name = by_name.get(name.casefold()) or by_name.get(stripped.casefold())
        shape = _object_shape(schemas.get(schema_name), schemas) if schema_name else None
        if not shape:
            continue
        properties, required, open_ended = shape
        where = {"file": model["file"], "line": model["line"], "symbol": name}
        mine = {prop["name"] for prop in model["properties"]}
        extra = sorted(mine - properties) if not open_ended else []
        if extra:  # one finding per model, so a thin schema does not bury the report
            listed = ", ".join(extra[:8]) + (f" and {len(extra) - 8} more" if len(extra) > 8 else "")
            findings.append(_finding("consumer", "model_property_not_in_contract", "warning",
                                     f"{name} declares {len(extra)} propert{'y' if len(extra) == 1 else 'ies'} that contract "
                                     f"schema {schema_name} does not define: {listed}.",
                                     location=where, evidence={"schema": schema_name, "properties": extra}))
        missing = sorted(required - mine)
        if missing:
            findings.append(_finding("consumer", "model_missing_property", "info",
                                     f"Contract schema {schema_name} always returns {', '.join(missing[:8])}, "
                                     f"which {name} does not declare.",
                                     location=where, evidence={"schema": schema_name, "properties": missing}))
    return findings


def match_calls(index: ContractIndex, calls: list[dict[str, Any]]) -> tuple[dict[int, tuple[str, str | None]], str | None]:
    """Request id -> (contract path, operation key or None when the method is not defined), and the accepted prefix."""
    prefix = infer_prefix(index, calls)
    matched: dict[int, tuple[str, str | None]] = {}
    for call in calls:
        if not call.get("url"):
            continue
        path = index.match(call["url"], prefix)
        if path is not None:
            matched[call["id"]] = (path, index.paths[path].get(call["method"]))
    return matched, prefix


def consumer_report(ui_map: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    """Findings and coverage for a front-end revision against one contract version."""
    index = ContractIndex(model)
    calls = ui_map.get("http_calls") or []
    matched, prefix = match_calls(index, calls)
    findings: list[dict[str, Any]] = []
    used: set[str] = set()
    unresolved = 0
    for call in calls:
        where = _where(call)
        if not call.get("url"):
            unresolved += 1
            findings.append(_finding("consumer", "unresolved_address", "info",
                                     "This request builds its address at run time, so it could not be checked.",
                                     location=where, evidence={"expression": call.get("raw", "")[:120]}))
            continue
        label = f"{call['method']} {call['url']}"
        if call["id"] not in matched:
            findings.append(_finding("consumer", "unknown_operation", "error",
                                     f"{label} matches no path in the contract.", location=where,
                                     evidence={"request": label}))
            continue
        path, key = matched[call["id"]]
        if key is None:
            findings.append(_finding("consumer", "method_not_in_contract", "error",
                                     f"The contract defines {path} only for {', '.join(sorted(index.paths[path]))}, not {call['method']}.",
                                     location=where, evidence={"request": label, "path": path}))
            continue
        used.add(key)
        findings.extend(_check_call(call, key, model["operations"][key], model["schemas"]))
    findings.extend(_check_models(ui_map, model["schemas"]))

    findings.sort(key=lambda f: (_SEVERITY_ORDER[f["severity"]], f["location"].get("file") or "", f["location"].get("line") or 0, f["code"]))
    cut = max(0, len(findings) - MAX_FINDINGS)
    findings = findings[:MAX_FINDINGS]
    severities = Counter(finding["severity"] for finding in findings)
    notes = list(ui_map.get("notes") or [])
    if prefix:
        notes.append(f"Requests share the prefix {prefix}, which the contract does not declare as a base path; "
                     "it was treated as one.")
    if cut:
        notes.append(f"{cut} more finding(s) were left out of this report.")
    return {
        "checker_version": CHECKER_VERSION, "check": "consumer",
        "summary": {"errors": severities["error"], "warnings": severities["warning"], "info": severities["info"]},
        "coverage": {
            "requests_total": len(calls), "requests_checked": len(calls) - unresolved, "requests_unresolved": unresolved,
            "requests_matched": sum(1 for _path, key in matched.values() if key),
            "operations_total": len(model["operations"]), "operations_used": len(used),
            "unused_operations": sorted(set(model["operations"]) - used),
            "inferred_prefix": prefix,
        },
        "findings": findings, "notes": notes,
    }


# ── provider check ───────────────────────────────────────────────────────────

def provider_report(routes_document: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    """Findings and coverage for a back-end revision's routes against one contract version.

    A back end usually serves more than its API (pages, webhooks, health checks). Only routes
    under the contract's base path are judged; without a declared base path the shared prefix
    of matching routes is used, and Laravel falls back to the routes of ``routes/api.php``.
    """
    index = ContractIndex(model)
    routes = routes_document.get("routes") or []
    as_calls = [{"id": position, "url": route["path"], "method": route["method"]} for position, route in enumerate(routes)]
    prefix = infer_prefix(index, as_calls)
    notes = list(routes_document.get("notes") or [])

    bases = [[part for part in base if part != "{}"] for base in index.bases]
    bases = [base for base in bases if base]
    if bases:
        def in_scope(route: dict[str, Any]) -> bool:
            segments = _segments(route["path"])
            return any(segments[: len(base)] == base for base in bases)
        scope = "the contract's base path"
    elif prefix:
        wanted = _segments(prefix)
        def in_scope(route: dict[str, Any]) -> bool:
            return _segments(route["path"])[: len(wanted)] == wanted
        scope = f"the prefix {prefix}, which routes that match the contract share"
        notes.append(f"The contract declares no base path. Routes under {prefix} were treated as the API.")
    elif any(route.get("api") for route in routes):
        def in_scope(route: dict[str, Any]) -> bool:
            return bool(route.get("api"))
        scope = "routes/api.php"
    else:
        def in_scope(route: dict[str, Any]) -> bool:
            return True
        scope = "all routes"

    findings: list[dict[str, Any]] = []
    implemented: set[str] = set()
    judged = documented = 0
    for route in routes:
        if not in_scope(route):
            continue
        judged += 1
        label = f"{route['method']} {route['path']}"
        where = {"file": route.get("file"), "line": route.get("line"), "symbol": route.get("action") or ""}
        path = index.match(route["path"], prefix)
        if path is None:
            findings.append(_finding("provider", "undocumented_route", "warning",
                                     f"{label} is served by the project but is not in the contract.",
                                     location=where, evidence={"route": label}))
            continue
        methods = index.paths[path]
        if route["method"] == "ANY":
            implemented.update(methods.values())
            documented += 1
        elif route["method"] in methods:
            implemented.add(methods[route["method"]])
            documented += 1
        else:
            findings.append(_finding("provider", "undocumented_route", "warning",
                                     f"{label} is served by the project, but the contract defines {path} only for "
                                     f"{', '.join(sorted(methods))}.", location=where,
                                     evidence={"route": label, "path": path}))
    for key in sorted(set(model["operations"]) - implemented):
        findings.append(_finding("provider", "unimplemented_operation", "error",
                                 f"The contract defines {key}, but no route in the project serves it.",
                                 operation=key, evidence={"operation": key}))

    findings.sort(key=lambda f: (_SEVERITY_ORDER[f["severity"]], f["location"].get("file") or "", f["code"], f["message"]))
    cut = max(0, len(findings) - MAX_FINDINGS)
    findings = findings[:MAX_FINDINGS]
    if cut:
        notes.append(f"{cut} more finding(s) were left out of this report.")
    severities = Counter(finding["severity"] for finding in findings)
    return {
        "checker_version": CHECKER_VERSION, "check": "provider",
        "summary": {"errors": severities["error"], "warnings": severities["warning"], "info": severities["info"]},
        "coverage": {
            "routes_total": len(routes), "routes_in_scope": judged, "routes_documented": documented,
            "operations_total": len(model["operations"]), "operations_implemented": len(implemented),
            "scope": scope, "inferred_prefix": prefix,
        },
        "findings": findings, "notes": notes,
    }


# ── runtime check ────────────────────────────────────────────────────────────

def _status_declared(responses: dict[str, Any], status: int) -> bool:
    """True when the contract's responses cover this status: exactly, by class (``4XX``), or by ``default``."""
    return any(key in responses for key in (str(status), f"{status // 100}XX", f"{status // 100}xx", "default"))


def runtime_report(observations: list[dict[str, Any]], model: dict[str, Any]) -> dict[str, Any]:
    """Findings for what the running back end was seen to answer, against one contract version.

    ``observations`` are counts per ``(method, route template, status code)`` from server request
    spans. Telemetry carries no response bodies, so this compares routes and status codes only.
    """
    index = ContractIndex(model)
    seen = [row for row in observations if row.get("route") and row["route"] != "(unmatched)" and row.get("method")]
    as_calls = [{"id": position, "url": row["route"], "method": row["method"]} for position, row in enumerate(seen)]
    prefix = infer_prefix(index, as_calls)
    findings: list[dict[str, Any]] = []
    observed: set[str] = set()
    outside = requests_total = 0
    notes: list[str] = []
    undocumented: dict[str, int] = {}
    for row in seen:
        count = int(row.get("requests") or 0)
        requests_total += count
        label = f"{row['method']} /{row['route'].strip('/')}"
        path = index.match(row["route"], prefix)
        key = index.paths[path].get(row["method"]) if path is not None else None
        if key is None:
            # Traffic to pages and other non-API routes is expected; only routes under the API prefix are reported.
            literal = [base for base in ([part for part in b if part != "{}"] for b in index.bases) if base]
            wanted = literal or ([_segments(prefix)] if prefix else [])
            segments = _segments(row["route"])
            if path is not None or any(segments[: len(base)] == base for base in wanted):
                undocumented[label] = undocumented.get(label, 0) + count
            else:
                outside += count
            continue
        observed.add(key)
        status = row.get("status_code")
        if isinstance(status, int) and not _status_declared(model["operations"][key]["responses"], status):
            findings.append(_finding("runtime", "undocumented_status", "warning",
                                     f"{key} answered with HTTP {status} ({count} request{'s' if count != 1 else ''}), "
                                     "which the contract does not list for it.", operation=key,
                                     evidence={"status": status, "requests": count}))
    for label, count in sorted(undocumented.items()):
        findings.append(_finding("runtime", "undocumented_route_observed", "warning",
                                 f"{label} received {count} request{'s' if count != 1 else ''} but is not in the contract.",
                                 evidence={"route": label, "requests": count}))
    if prefix:
        notes.append(f"The contract declares no base path. Routes under {prefix} were treated as the API.")
    if not seen:
        notes.append("No requests were recorded for this revision in the runtime window, so there is nothing to compare.")
    findings.sort(key=lambda f: (_SEVERITY_ORDER[f["severity"]], f["code"], -int(f["evidence"].get("requests") or 0), f["message"]))
    severities = Counter(finding["severity"] for finding in findings)
    return {
        "checker_version": CHECKER_VERSION, "check": "runtime",
        "summary": {"errors": severities["error"], "warnings": severities["warning"], "info": severities["info"]},
        "coverage": {
            "requests_total": requests_total, "requests_outside_api": outside,
            "operations_total": len(model["operations"]), "operations_observed": len(observed),
            "unobserved_operations": sorted(set(model["operations"]) - observed), "inferred_prefix": prefix,
        },
        "findings": findings[:MAX_FINDINGS], "notes": notes,
    }


# ── change impact ────────────────────────────────────────────────────────────

def impact_report(ui_map: dict[str, Any], old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """What changing the contract from ``old`` to ``new`` touches in a consumer project."""
    changes = diff_models(old, new)
    changed_schemas = set(changes["schemas"]["changed"]) | set(changes["schemas"]["removed"])
    entries: dict[str, dict[str, Any]] = {}
    for key in changes["operations"]["removed"]:
        entries[key] = {"operation": key, "change": "removed", "parts": [], "schemas": []}
    for item in changes["operations"]["changed"]:
        entries[item["operation"]] = {"operation": item["operation"], "change": "changed", "parts": item["parts"], "schemas": []}
    for key, operation in old["operations"].items():
        touched = sorted(_referenced_schemas(operation, old["schemas"]) & changed_schemas)
        if not touched:
            continue
        if key in entries:
            entries[key]["schemas"] = touched
        elif key in new["operations"]:
            entries[key] = {"operation": key, "change": "schema", "parts": [], "schemas": touched}

    # Requests are matched against the old version: that is the contract the code was written for.
    index = ContractIndex(old)
    calls = ui_map.get("http_calls") or []
    matched, _prefix = match_calls(index, calls)
    calls_by_operation: dict[str, list[dict[str, Any]]] = {}
    for call in calls:
        key = (matched.get(call["id"]) or (None, None))[1]
        if key:
            calls_by_operation.setdefault(key, []).append(call)

    components = ui_map.get("components") or []
    by_id = {component["id"]: component for component in components}
    parents: dict[str, list[str]] = {}
    for component in components:
        for child in component["uses"]:
            parents.setdefault(child, []).append(component["id"])
    routes_by_component: dict[str, list[str]] = {}
    for route in ui_map.get("routes") or []:
        if route.get("component_id"):
            routes_by_component.setdefault(route["component_id"], []).append(route["path"])

    affected_components: set[str] = set()
    affected_routes: set[str] = set()
    result = []
    for key, entry in sorted(entries.items()):
        sites = calls_by_operation.get(key, [])
        ids = {call["id"] for call in sites}
        owners = [c["id"] for c in components if ids & (set(c["http_calls"]) | set(c["via_imports"]))]
        # Everything that renders an owner, up to the routes.
        reached, pending = set(owners), list(owners)
        while pending:
            for parent in parents.get(pending.pop(), []):
                if parent not in reached:
                    reached.add(parent)
                    pending.append(parent)
        routes = sorted({path for component_id in reached for path in routes_by_component.get(component_id, [])})
        affected_components |= set(owners)
        affected_routes |= set(routes)
        result.append({**entry,
                       "calls": [{"file": call["file"], "line": call["line"], "symbol": call.get("symbol") or "",
                                  "request": f"{call['method']} {call['url']}"} for call in sites],
                       "components": [{"id": owner, "name": by_id[owner]["name"], "file": by_id[owner]["file"]} for owner in owners],
                       "routes": routes})
    result.sort(key=lambda entry: (not entry["calls"], {"removed": 0, "changed": 1, "schema": 2}[entry["change"]], entry["operation"]))
    return {
        "checker_version": CHECKER_VERSION,
        "summary": {"operations_changed": len(result), "operations_in_use": sum(1 for entry in result if entry["calls"]),
                    "components": len(affected_components), "routes": len(affected_routes),
                    "operations_added": len(changes["operations"]["added"])},
        "added_operations": changes["operations"]["added"],
        "changed_schemas": sorted(changed_schemas),
        "entries": result, "notes": list(ui_map.get("notes") or []),
    }
