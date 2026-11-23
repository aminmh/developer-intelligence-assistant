"""TypeScript building blocks shared by every generator: names, types, and operation signatures.

Plan: @.archcore/frontend-assist/frontend-developer-assistance.plan.md (Phase 5).

Everything is derived from the internal contract model (``api_contracts.model``),
so Swagger 2.0 and OpenAPI 3.x documents produce the same code. Generation is
deterministic: the same contract version always gives the same files.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_RESERVED = {
    "break", "case", "catch", "class", "const", "continue", "debugger", "default", "delete", "do", "else", "enum",
    "export", "extends", "false", "finally", "for", "function", "if", "import", "in", "instanceof", "new", "null",
    "return", "super", "switch", "this", "throw", "true", "try", "typeof", "var", "void", "while", "with", "yield",
    "let", "static", "implements", "interface", "package", "private", "protected", "public", "await", "async",
    "arguments", "eval", "body", "query", "signal", "request", "config",
}
_BUILTIN_TYPES = {"Array", "Record", "Promise", "Date", "Blob", "FormData", "Object", "String", "Number", "Boolean",
                  "Error", "Map", "Set", "Partial", "Required", "Readonly", "Pick", "Omit", "Function", "Symbol"}
_MAX_DEPTH = 12


def words(text: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", re.sub(r"[^A-Za-z0-9]+", " ", text))
    return [word for word in spaced.split() if word]


def pascal(text: str) -> str:
    name = "".join(word[:1].upper() + word[1:] for word in words(text)) or "Unnamed"
    return f"_{name}" if name[0].isdigit() else name


def camel(text: str) -> str:
    name = pascal(text)
    name = name[:1].lower() + name[1:]
    return f"{name}_" if name in _RESERVED else name


def literal(value: Any) -> str:
    """A JSON value as a TypeScript literal."""
    return json.dumps(value, ensure_ascii=False)


def property_key(name: str) -> str:
    return name if _IDENTIFIER.match(name) else literal(name)


def comment(text: Any, indent: str = "") -> str:
    """A one-line JSDoc comment, or '' when there is nothing to say."""
    if not isinstance(text, str) or not text.strip():
        return ""
    line = " ".join(text.split()).replace("*/", "* /")[:200]
    return f"{indent}/** {line} */\n"


class TypeNames:
    """Schema name -> the TypeScript type name used for it, unique and valid."""

    def __init__(self, schemas: dict[str, Any]) -> None:
        self.names: dict[str, str] = {}
        taken: set[str] = set(_BUILTIN_TYPES)
        for schema_name in sorted(schemas):
            base = pascal(schema_name)
            candidate, suffix = base, 2
            while candidate in taken:
                candidate, suffix = f"{base}{suffix}", suffix + 1
            taken.add(candidate)
            self.names[schema_name] = candidate

    def of(self, ref: str) -> str:
        return self.names.get(ref.removeprefix("#/schemas/"), "unknown")


def ts_type(schema: Any, names: TypeNames, depth: int = 0) -> str:
    """The TypeScript type of a JSON schema."""
    if not isinstance(schema, dict) or depth > _MAX_DEPTH:
        return "unknown"
    if isinstance(schema.get("$ref"), str):
        return names.of(schema["$ref"])
    nullable = schema.get("nullable") is True
    kinds = schema.get("type")
    if isinstance(kinds, list):                                    # OpenAPI 3.1: type: [string, "null"]
        nullable = nullable or "null" in kinds
        kinds = [kind for kind in kinds if kind != "null"]
        if len(kinds) > 1:
            union = " | ".join(dict.fromkeys(ts_type({**schema, "type": kind}, names, depth + 1) for kind in kinds))
            return f"{union} | null" if nullable else union
        kinds = kinds[0] if kinds else None

    def finish(result: str) -> str:
        if not nullable or result in ("unknown", "null"):
            return result
        return f"({result}) | null" if " & " in result else f"{result} | null"

    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return finish(" | ".join(dict.fromkeys(literal(value) for value in schema["enum"])))
    if "const" in schema:
        return finish(literal(schema["const"]))
    for keyword, joiner in (("oneOf", " | "), ("anyOf", " | "), ("allOf", " & ")):
        options = schema.get(keyword)
        if isinstance(options, list) and options:
            parts = list(dict.fromkeys(ts_type(option, names, depth + 1) for option in options))
            own = _object_type(schema, names, depth) if schema.get("properties") else None
            if own:
                parts.append(own)
            parts = [f"({part})" if (" | " in part and joiner == " & ") else part for part in parts]
            return finish(joiner.join(parts) if len(parts) > 1 else parts[0])
    if kinds == "string":
        return finish("Blob" if schema.get("format") == "binary" else "string")
    if kinds in ("integer", "number"):
        return finish("number")
    if kinds == "boolean":
        return finish("boolean")
    if kinds == "null":
        return "null"
    if kinds == "array" or "items" in schema:
        item = ts_type(schema.get("items"), names, depth + 1)
        return finish(f"Array<{item}>")
    if kinds == "object" or "properties" in schema or "additionalProperties" in schema:
        return finish(_object_type(schema, names, depth))
    return "unknown"


def _object_type(schema: dict[str, Any], names: TypeNames, depth: int) -> str:
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    extra = schema.get("additionalProperties")
    if not properties:
        if isinstance(extra, dict):
            return f"Record<string, {ts_type(extra, names, depth + 1)}>"
        return "Record<string, unknown>"
    required = set(schema.get("required") or [])
    members = [f"{property_key(key)}{'' if key in required else '?'}: {ts_type(value, names, depth + 1)}"
               for key, value in properties.items()]
    if extra is True or isinstance(extra, dict):
        members.append("[key: string]: unknown")
    return "{ " + "; ".join(members) + " }"


def declaration(name: str, schema: Any, names: TypeNames) -> str:
    """``export interface`` for a plain object schema, ``export type`` for everything else."""
    type_name = names.names[name]
    doc = comment(schema.get("description") if isinstance(schema, dict) else None)
    plain_object = (isinstance(schema, dict) and isinstance(schema.get("properties"), dict) and schema["properties"]
                    and not any(key in schema for key in ("allOf", "oneOf", "anyOf", "enum", "$ref"))
                    and schema.get("nullable") is not True and not isinstance(schema.get("type"), list))
    if not plain_object:
        return f"{doc}export type {type_name} = {ts_type(schema, names)};\n"
    required = set(schema.get("required") or [])
    lines = [f"{doc}export interface {type_name} {{"]
    for key, value in schema["properties"].items():
        lines.append(comment(value.get("description") if isinstance(value, dict) else None, "  ").rstrip("\n")
                     if isinstance(value, dict) and value.get("description") else "")
        lines.append(f"  {property_key(key)}{'' if key in required else '?'}: {ts_type(value, names, 1)};")
    extra = schema.get("additionalProperties")
    if extra is True or isinstance(extra, dict):
        lines.append("  [key: string]: unknown;")
    lines.append("}")
    return "\n".join(line for line in lines if line) + "\n"


# ── operations ───────────────────────────────────────────────────────────────

@dataclass
class Operation:
    """One contract operation, reduced to what a generated function needs."""

    key: str
    name: str
    method: str
    path: str
    summary: str
    deprecated: bool
    path_parameters: list[tuple[str, str, str]] = field(default_factory=list)   # (wire name, argument name, type)
    query_type: str | None = None          # inline object type of the query parameters
    query_required: bool = False
    body_type: str | None = None
    body_required: bool = False
    body_form: bool = False                # multipart or URL-encoded body
    response_type: str = "void"
    success_status: int = 200
    response_schema: Any = None
    skipped_parameters: int = 0            # header and cookie parameters, which the generated code does not send


def _response(operation: dict[str, Any], names: TypeNames) -> tuple[str, int, Any]:
    responses = operation.get("responses") or {}
    successes = sorted((status for status in responses if re.fullmatch(r"2\d\d", status)), key=int)
    candidates = successes or [status for status in ("2XX", "2xx", "default") if status in responses]
    for status in candidates:
        content = (responses[status] or {}).get("content") or {}
        code = int(status) if status.isdigit() else 200
        for media, schema in content.items():
            if "json" in media:
                return ts_type(schema, names), code, schema
        if content:
            media, schema = next(iter(content.items()))
            return ("Blob" if not media.startswith("text/") else "string"), code, None
        return "void", code, None
    return "void", 200, None


def operations_of(model: dict[str, Any], names: TypeNames, selected: set[str] | None = None) -> list[Operation]:
    """Operations in a stable order, each with a unique function name."""
    result: list[Operation] = []
    taken: set[str] = set()
    ordered = sorted(model["operations"].items(), key=lambda item: (item[1]["path"], item[1]["method"]))
    for key, source in ordered:
        if selected is not None and key not in selected:
            continue
        base = camel(source["operation_id"]) if source.get("operation_id") and words(source["operation_id"]) else camel(
            source["method"].lower() + " " + " ".join(
                ("by " + part.strip("{}")) if part.startswith("{") else part for part in source["path"].split("/") if part))
        name, suffix = base, 2
        while name in taken:
            name, suffix = f"{base}{suffix}", suffix + 1
        taken.add(name)
        operation = Operation(key=key, name=name, method=source["method"], path=source["path"],
                              summary=source.get("summary") or "", deprecated=bool(source.get("deprecated")))
        by_wire = {p["name"]: p for p in source["parameters"] if p["in"] == "path"}
        arguments: set[str] = set()
        for wire in re.findall(r"\{([^{}]+)\}", source["path"]):
            argument = camel(wire)
            while argument in arguments:
                argument += "_"
            arguments.add(argument)
            kind = ts_type((by_wire.get(wire) or {}).get("schema"), names)
            operation.path_parameters.append((wire, argument, kind if kind in ("string", "number", "boolean") else "string | number"))
        query = [p for p in source["parameters"] if p["in"] == "query"]
        if query:
            members = [f"{property_key(p['name'])}{'' if p['required'] else '?'}: {ts_type(p.get('schema'), names)}" for p in query]
            operation.query_type = "{ " + "; ".join(members) + " }"
            operation.query_required = any(p["required"] for p in query)
        operation.skipped_parameters = sum(1 for p in source["parameters"] if p["in"] in ("header", "cookie"))
        body = source.get("request_body")
        if body and body.get("content"):
            content = body["content"]
            json_media = next((media for media in content if "json" in media), None)
            if json_media:
                operation.body_type = ts_type(content[json_media], names)
            elif any(media.startswith(("multipart/", "application/x-www-form-urlencoded")) for media in content):
                operation.body_type, operation.body_form = "FormData", True
            else:
                operation.body_type = "Blob | string"
            operation.body_required = bool(body.get("required"))
        operation.response_type, operation.success_status, operation.response_schema = _response(source, names)
        result.append(operation)
    return result


def path_template(operation: Operation) -> str:
    """The path as a template literal body: ``/orders/${encodeURIComponent(String(orderId))}``."""
    path = operation.path
    for wire, argument, _kind in operation.path_parameters:
        path = path.replace("{" + wire + "}", "${encodeURIComponent(String(" + argument + "))}")
    return path.replace("`", "\\`")


def arguments(operation: Operation, *, wrap: str | None = None) -> list[tuple[str, str, bool]]:
    """(name, type, optional) of a generated function's parameters, required ones first.

    ``wrap`` names a generic that wraps each type, for example ``MaybeRefOrGetter`` in Vue composables.
    """
    def shown(kind: str) -> str:
        return f"{wrap}<{kind}>" if wrap else kind

    result = [(argument, shown(kind), False) for _wire, argument, kind in operation.path_parameters]
    body = ("body", shown(operation.body_type), not operation.body_required) if operation.body_type else None
    query = ("query", shown(operation.query_type), not operation.query_required) if operation.query_type else None
    extras = [item for item in (body, query) if item]
    result.extend(sorted(extras, key=lambda item: item[2]))  # a required argument never follows an optional one
    return result


def signature(items: list[tuple[str, str, bool]], *more: str) -> str:
    return ", ".join([*(f"{name}{'?' if optional else ''}: {kind}" for name, kind, optional in items), *more])


def docblock(operation: Operation, indent: str = "") -> str:
    lines = [operation.summary.strip() or operation.key]
    if operation.summary.strip():
        lines.append(operation.key)
    if operation.deprecated:
        lines.append("@deprecated The contract marks this operation as deprecated.")
    body = "\n".join(f"{indent} * {' '.join(line.split()).replace('*/', '* /')[:200]}" for line in lines)
    return f"{indent}/**\n{body}\n{indent} */\n"
