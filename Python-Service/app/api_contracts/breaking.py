"""Classify the difference between two contract versions for the people who consume it.

Spec: @.archcore/frontend-assist/contract-change-notifications.spec.md (classification rules).

``diff_models`` says *which* operations and schemas changed. This module says *how much it
matters to a client*: every change item gets a severity:

- ``breaking`` — a client written for the old version can fail against the new one;
- ``risky`` — a client keeps working in most cases but may mishandle the new behaviour;
- ``safe`` — additive, a client written for the old version is unaffected.

Requests and responses are judged in opposite directions. Removing a response property breaks
clients that read it; removing a request property only means the server ignores what clients
still send. Adding a required request property breaks clients; adding a response property does not.
"""

from __future__ import annotations

from typing import Any

SEVERITIES = ("breaking", "risky", "safe")
_RANK = {severity: rank for rank, severity in enumerate(SEVERITIES)}
MAX_ITEMS = 500
_MAX_DEPTH = 12


def max_severity(items: list[dict[str, Any]]) -> str | None:
    """The most severe severity among ``items``, or None when there are none."""
    return min((item["severity"] for item in items), key=_RANK.__getitem__, default=None)


def _item(severity: str, code: str, message: str, *, operation: str | None, pointer: str = "") -> dict[str, Any]:
    return {"severity": severity, "code": code, "operation": operation, "pointer": pointer, "message": message}


def _resolve(schema: Any, schemas: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Follow ``$ref`` and merge ``allOf``, so an object's properties are visible."""
    if not isinstance(schema, dict) or depth > _MAX_DEPTH:
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
        return merged
    return schema


def _types(schema: dict[str, Any]) -> tuple[frozenset[str], bool]:
    """(non-null types, nullable). Handles OpenAPI 3.0 ``nullable`` and 3.1 type lists."""
    raw = schema.get("type")
    names = set(raw) if isinstance(raw, list) else ({raw} if isinstance(raw, str) else set())
    nullable = bool(schema.get("nullable")) or "null" in names
    names.discard("null")
    if not names and isinstance(schema.get("properties"), dict):
        names = {"object"}
    return frozenset(names), nullable


class _Comparer:
    def __init__(self, old_schemas: dict[str, Any], new_schemas: dict[str, Any], operation: str) -> None:
        self.old_schemas, self.new_schemas = old_schemas, new_schemas
        self.operation = operation
        self.items: list[dict[str, Any]] = []
        self._seen: set[tuple[str, str, str]] = set()

    def add(self, severity: str, code: str, message: str, pointer: str) -> None:
        self.items.append(_item(severity, code, message, operation=self.operation, pointer=pointer))

    def schema(self, old: Any, new: Any, pointer: str, direction: str, depth: int = 0) -> None:
        """Compare two schemas. ``direction`` is ``request`` (client writes) or ``response`` (client reads)."""
        if depth > _MAX_DEPTH or not isinstance(old, dict) or not isinstance(new, dict):
            return
        old_ref, new_ref = old.get("$ref"), new.get("$ref")
        if isinstance(old_ref, str) and isinstance(new_ref, str):
            key = (old_ref, new_ref, direction)
            if key in self._seen:  # a recursive schema: compared once per direction
                return
            self._seen.add(key)
        old, new = _resolve(old, self.old_schemas), _resolve(new, self.new_schemas)
        if not old or not new:
            return
        old_types, old_nullable = _types(old)
        new_types, new_nullable = _types(new)
        where = pointer or "body"
        if old_types and new_types and old_types != new_types:
            if direction == "response" and old_types == {"integer"} and new_types == {"number"}:
                self.add("risky", "response_type_widened", f"{where} may now be a decimal number, not only an integer.",
                         pointer)
            else:
                self.add("breaking", f"{direction}_type_changed",
                         f"{where} changed type from {'/'.join(sorted(old_types))} to {'/'.join(sorted(new_types))}.",
                         pointer)
            return
        if direction == "response" and new_nullable and not old_nullable:
            self.add("risky", "response_became_nullable", f"{where} can now be null.", pointer)
        if direction == "request" and old_nullable and not new_nullable:
            self.add("breaking", "request_null_refused", f"{where} no longer accepts null.", pointer)
        self._enum(old, new, pointer, direction)
        if "object" in old_types or isinstance(old.get("properties"), dict):
            self._object(old, new, pointer, direction, depth)
        if "array" in old_types and isinstance(old.get("items"), dict) and isinstance(new.get("items"), dict):
            self.schema(old["items"], new["items"], f"{pointer}[]", direction, depth + 1)

    def _enum(self, old: dict[str, Any], new: dict[str, Any], pointer: str, direction: str) -> None:
        old_values, new_values = old.get("enum"), new.get("enum")
        if not isinstance(old_values, list) or not isinstance(new_values, list):
            return
        removed = [value for value in old_values if value not in new_values]
        added = [value for value in new_values if value not in old_values]
        where = pointer or "body"
        if direction == "request" and removed:
            self.add("breaking", "request_enum_value_removed",
                     f"{where} no longer accepts {', '.join(map(repr, removed[:5]))}.", pointer)
        if direction == "response" and added:
            self.add("risky", "response_enum_value_added",
                     f"{where} may now be {', '.join(map(repr, added[:5]))}.", pointer)
        if direction == "response" and removed:
            self.add("safe", "response_enum_value_removed",
                     f"{where} is no longer {', '.join(map(repr, removed[:5]))}.", pointer)

    def _object(self, old: dict[str, Any], new: dict[str, Any], pointer: str, direction: str, depth: int) -> None:
        old_props = old.get("properties") if isinstance(old.get("properties"), dict) else {}
        new_props = new.get("properties") if isinstance(new.get("properties"), dict) else {}
        old_required = set(old.get("required") or [])
        new_required = set(new.get("required") or [])
        prefix = f"{pointer}." if pointer else ""
        for name in sorted(set(old_props) - set(new_props)):
            if direction == "response":
                self.add("breaking", "response_property_removed", f"Response property {prefix}{name} was removed.",
                         prefix + name)
            else:
                self.add("risky", "request_property_removed",
                         f"Request property {prefix}{name} was removed; the server may ignore or reject it.", prefix + name)
        for name in sorted(set(new_props) - set(old_props)):
            if direction == "request" and name in new_required:
                self.add("breaking", "request_property_required",
                         f"Request property {prefix}{name} was added and is required.", prefix + name)
            else:
                self.add("safe", f"{direction}_property_added", f"Property {prefix}{name} was added.", prefix + name)
        for name in sorted(set(old_props) & set(new_props)):
            if direction == "request" and name in new_required and name not in old_required:
                self.add("breaking", "request_property_required", f"Request property {prefix}{name} is now required.",
                         prefix + name)
            if direction == "response" and name in old_required and name not in new_required:
                self.add("risky", "response_property_optional",
                         f"Response property {prefix}{name} may now be missing.", prefix + name)
            self.schema(old_props[name], new_props[name], prefix + name, direction, depth + 1)


def _json_schema(content: dict[str, Any] | None) -> Any:
    content = content or {}
    for media, schema in content.items():
        if "json" in media:
            return schema
    return next(iter(content.values()), None)


def _parameters(operation: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(str(p.get("in")), str(p.get("name"))): p for p in operation.get("parameters") or []}


def _compare_operation(key: str, old: dict[str, Any], new: dict[str, Any],
                       old_schemas: dict[str, Any], new_schemas: dict[str, Any]) -> list[dict[str, Any]]:
    compare = _Comparer(old_schemas, new_schemas, key)
    if new.get("deprecated") and not old.get("deprecated"):
        compare.add("risky", "operation_deprecated", f"{key} is now deprecated.", "")

    old_params, new_params = _parameters(old), _parameters(new)
    for location, name in sorted(set(old_params) - set(new_params)):
        compare.add("risky", "parameter_removed",
                    f"The {location} parameter {name} was removed; the server may ignore it.", f"parameters.{name}")
    for location, name in sorted(set(new_params) - set(old_params)):
        if new_params[(location, name)].get("required"):
            compare.add("breaking", "required_parameter_added", f"A required {location} parameter {name} was added.",
                        f"parameters.{name}")
        else:
            compare.add("safe", "parameter_added", f"An optional {location} parameter {name} was added.",
                        f"parameters.{name}")
    for location, name in sorted(set(old_params) & set(new_params)):
        before, after = old_params[(location, name)], new_params[(location, name)]
        if after.get("required") and not before.get("required"):
            compare.add("breaking", "parameter_became_required", f"The {location} parameter {name} is now required.",
                        f"parameters.{name}")
        compare.schema(before.get("schema") or {}, after.get("schema") or {}, f"parameters.{name}", "request")

    old_body, new_body = old.get("request_body"), new.get("request_body")
    if new_body and not old_body and new_body.get("required"):
        compare.add("breaking", "request_body_required", "A required request body was added.", "request_body")
    elif old_body and new_body:
        if new_body.get("required") and not old_body.get("required"):
            compare.add("breaking", "request_body_required", "The request body is now required.", "request_body")
        compare.schema(_json_schema(old_body.get("content")) or {}, _json_schema(new_body.get("content")) or {},
                       "", "request")

    old_responses, new_responses = old.get("responses") or {}, new.get("responses") or {}
    for status in sorted(set(old_responses) - set(new_responses)):
        success = status.startswith("2")
        compare.add("breaking" if success else "safe", "response_status_removed",
                    f"The {status} response was removed.", f"responses.{status}")
    for status in sorted(set(new_responses) - set(old_responses)):
        compare.add("risky" if status.startswith(("2", "4")) else "safe", "response_status_added",
                    f"The operation may now answer {status}.", f"responses.{status}")
    for status in sorted(set(old_responses) & set(new_responses)):
        before = _json_schema(old_responses[status].get("content"))
        after = _json_schema(new_responses[status].get("content"))
        if before and not after and status.startswith("2"):
            compare.add("breaking", "response_body_removed", f"The {status} response no longer has a body.",
                        f"responses.{status}")
        elif before and after:
            compare.schema(before, after, f"responses.{status}", "response")
    return compare.items


def classify(old: dict[str, Any] | None, new: dict[str, Any]) -> list[dict[str, Any]]:
    """Change items between two contract models, most severe first. ``old`` None means a first version."""
    if old is None:
        return []
    items: list[dict[str, Any]] = []
    old_ops, new_ops = old["operations"], new["operations"]
    for key in sorted(set(old_ops) - set(new_ops)):
        items.append(_item("breaking", "operation_removed", f"{key} was removed from the contract.", operation=key))
    for key in sorted(set(new_ops) - set(old_ops)):
        items.append(_item("safe", "operation_added", f"{key} was added to the contract.", operation=key))
    schemas_changed = old["schemas"] != new["schemas"]  # a shared schema can change an operation that looks equal
    for key in sorted(set(old_ops) & set(new_ops)):
        if schemas_changed or old_ops[key] != new_ops[key]:
            items.extend(_compare_operation(key, old_ops[key], new_ops[key], old["schemas"], new["schemas"]))
    # Most severe first, then a stable order, so the same pair of versions always reads the same.
    items.sort(key=lambda item: (_RANK[item["severity"]], item["operation"] or "", item["pointer"], item["code"]))
    return items[:MAX_ITEMS]


def summarize(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {severity: 0 for severity in SEVERITIES}
    for item in items:
        counts[item["severity"]] += 1
    return counts
