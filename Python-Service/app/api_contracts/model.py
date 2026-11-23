"""Parse, validate, and normalise Swagger 2.0 and OpenAPI 3.x documents.

Spec: @.archcore/frontend-assist/api-contract-sources-and-versions.spec.md (behaviors 3-9, 11).

Every dialect becomes one internal contract model, so later features read a
single shape::

    {"dialect", "title", "api_version", "base_paths": [...],
     "operations": {"GET /pets/{id}": {...}}, "schemas": {"Pet": {...}}}

Schema references inside the model always read ``#/schemas/<Name>``. The
content hash is the SHA-256 of the model's canonical JSON, so reformatting a
document, or switching it between JSON and YAML, does not create a version.

Validation is structural (the fields this model needs, and every reference
resolving). It is not a full OpenAPI meta-schema check.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from typing import Any, Callable

import yaml

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_REFERENCED_FILES = 200
MAX_ERRORS = 50
_MAX_DEPTH = 200
METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
DIALECTS = ("swagger-2.0", "openapi-3.0", "openapi-3.1")

# Reads one file next to the document: project-relative POSIX path -> bytes, or None when absent.
FileResolver = Callable[[str], "bytes | None"]


class ContractInvalid(ValueError):
    """The document cannot become a contract version; ``errors`` lists why."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors[:MAX_ERRORS] or ["The document is not a valid API contract."]
        super().__init__(self.errors[0])


class _NoAliasLoader(yaml.SafeLoader):
    """YAML without anchors and aliases, so a small file cannot expand without bound."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise ContractInvalid(["YAML aliases are not supported in a contract document."])
        return super().compose_node(parent, index)


def parse_document(data: bytes) -> dict[str, Any]:
    """Decode JSON or YAML bytes into a mapping."""
    if not data or not data.strip():
        raise ContractInvalid(["The document is empty."])
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ContractInvalid(["The document exceeds the 10 MB limit."])
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ContractInvalid(["The document is not UTF-8 text."]) from exc
    try:
        document = json.loads(text) if text.lstrip().startswith("{") else yaml.load(text, Loader=_NoAliasLoader)  # noqa: S506 — SafeLoader subclass
    except ContractInvalid:
        raise
    except (ValueError, yaml.YAMLError, RecursionError) as exc:
        detail = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
        raise ContractInvalid([f"The document is not valid JSON or YAML: {detail}"]) from exc
    if not isinstance(document, dict):
        raise ContractInvalid(["The document must be a JSON or YAML object."])
    return document


def detect_dialect(document: dict[str, Any]) -> str:
    swagger, openapi = document.get("swagger"), document.get("openapi")
    if str(swagger) == "2.0":
        return "swagger-2.0"
    if isinstance(openapi, str) and re.fullmatch(r"3\.0\.\d+(-.+)?", openapi):
        return "openapi-3.0"
    if isinstance(openapi, str) and re.fullmatch(r"3\.1\.\d+(-.+)?", openapi):
        return "openapi-3.1"
    raise ContractInvalid(["The document must declare swagger: \"2.0\" or openapi: 3.0.x / 3.1.x."])


def _pointer(document: Any, pointer: str) -> Any:
    """Resolve a JSON pointer (``/a/b``); raises KeyError when it does not exist."""
    node = document
    for raw in [part for part in pointer.split("/") if part != ""]:
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            node = node[int(key)]
        elif isinstance(node, dict):
            node = node[key]
        else:
            raise KeyError(pointer)
    return node


_SCHEMA_POINTER = re.compile(r"^/(?:definitions|components/schemas)/([^/]+)$")


class _Bundler:
    """Rewrites every reference so the model is one self-contained object."""

    def __init__(self, root: dict[str, Any], resolver: FileResolver | None, base_path: str) -> None:
        self.documents: dict[str, dict[str, Any]] = {"": root}
        self.resolver = resolver
        self.base_dir = posixpath.dirname(base_path)
        self.schemas: dict[str, Any] = {}
        self.schema_names: dict[tuple[str, str], str] = {}
        self.errors: list[str] = []
        self.files_read = 0
        self.bytes_read = 0

    def _error(self, message: str) -> None:
        if message not in self.errors and len(self.errors) < MAX_ERRORS:
            self.errors.append(message)

    def _load(self, file: str) -> dict[str, Any] | None:
        if file in self.documents:
            return self.documents[file]
        if self.resolver is None:
            self._error(f"The document references another file ({file}); it must be self-contained.")
            return None
        self.files_read += 1
        if self.files_read > MAX_REFERENCED_FILES:
            self._error("The document references more than 200 files.")
            return None
        data = self.resolver(file)
        if data is None:
            self._error(f"Referenced file not found in the revision: {file}")
            return None
        self.bytes_read += len(data)
        if self.bytes_read > MAX_DOCUMENT_BYTES:
            self._error("The referenced files exceed the 10 MB limit.")
            return None
        try:
            self.documents[file] = parse_document(data)
        except ContractInvalid as exc:
            self._error(f"{file}: {exc.errors[0]}")
            return None
        return self.documents[file]

    def _target(self, ref: Any, current_file: str) -> tuple[str, str] | None:
        if not isinstance(ref, str) or not ref:
            self._error("A $ref must be a non-empty string.")
            return None
        location, _, pointer = ref.partition("#")
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", location) or location.startswith("//"):
            self._error(f"References to a URL are not supported: {ref[:200]}")
            return None
        if not location:
            return current_file, pointer
        if location.startswith("/"):
            self._error(f"A file reference must be relative: {ref[:200]}")
            return None
        folder = posixpath.dirname(current_file) if current_file else self.base_dir
        file = posixpath.normpath(posixpath.join(folder, location))
        if file.startswith("../") or file == "..":
            self._error(f"A file reference leaves the project: {ref[:200]}")
            return None
        return file, pointer

    def _schema_name(self, file: str, name: str) -> str:
        key = (file, name)
        if key not in self.schema_names:
            candidate, suffix = name, 2
            while candidate in self.schema_names.values():
                candidate, suffix = f"{name}_{suffix}", suffix + 1
            self.schema_names[key] = candidate
        return self.schema_names[key]

    def rewrite(self, node: Any, current_file: str = "", trail: tuple[tuple[str, str], ...] = ()) -> Any:
        if len(trail) > _MAX_DEPTH:
            self._error("References are nested too deeply.")
            return {}
        if isinstance(node, list):
            return [self.rewrite(item, current_file, trail) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" not in node:
            return {key: self.rewrite(value, current_file, trail) for key, value in node.items()}
        target = self._target(node["$ref"], current_file)
        if target is None:
            return {}
        file, pointer = target
        document = self._load(file)
        if document is None:
            return {}
        named = _SCHEMA_POINTER.match(pointer)
        if named:
            source_name = named.group(1).replace("~1", "/").replace("~0", "~")
            first_use = (file, source_name) not in self.schema_names
            name = self._schema_name(file, source_name)
            if first_use:
                try:
                    self.schemas[name] = None  # reserve, so a recursive schema refers back to itself
                    self.schemas[name] = self.rewrite(_pointer(document, pointer), file, ())
                except (KeyError, IndexError, ValueError):
                    self.schemas.pop(name, None)
                    self._error(f"Reference does not resolve: {node['$ref'][:200]}")
                    return {}
            return {"$ref": f"#/schemas/{name}"}
        if target in trail:
            self._error(f"Circular reference outside the schema section: {node['$ref'][:200]}")
            return {}
        try:
            resolved = _pointer(document, pointer)
        except (KeyError, IndexError, ValueError):
            self._error(f"Reference does not resolve: {node['$ref'][:200]}")
            return {}
        return self.rewrite(resolved, file, (*trail, target))


def _content(schema: Any, media_types: list[str]) -> dict[str, Any]:
    return {media: schema for media in media_types} if schema is not None else {}


def _swagger_operation(operation: dict[str, Any], parameters: list[dict[str, Any]], document: dict[str, Any]) -> dict[str, Any]:
    consumes = [str(v) for v in operation.get("consumes") or document.get("consumes") or ["application/json"]]
    produces = [str(v) for v in operation.get("produces") or document.get("produces") or ["application/json"]]
    plain, body, form = [], None, {}
    for parameter in parameters:
        location = parameter.get("in")
        if location == "body":
            body = {"required": bool(parameter.get("required")), "content": _content(parameter.get("schema") or {}, consumes)}
        elif location == "formData":
            form[str(parameter.get("name"))] = {key: value for key, value in parameter.items()
                                                if key not in ("name", "in", "required", "description")}
        else:
            schema = {key: value for key, value in parameter.items() if key not in ("name", "in", "required", "description")}
            plain.append({"name": parameter.get("name"), "in": location,
                          "required": bool(parameter.get("required")), "schema": schema})
    if form and body is None:
        required = sorted(str(p.get("name")) for p in parameters if p.get("in") == "formData" and p.get("required"))
        schema = {"type": "object", "properties": form, **({"required": required} if required else {})}
        media = [m for m in consumes if m in ("multipart/form-data", "application/x-www-form-urlencoded")]
        body = {"required": bool(required), "content": _content(schema, media or ["application/x-www-form-urlencoded"])}
    responses = {str(status): {"content": _content(response.get("schema"), produces)}
                 for status, response in (operation.get("responses") or {}).items() if isinstance(response, dict)}
    return {"parameters": plain, "request_body": body, "responses": responses}


def _openapi_operation(operation: dict[str, Any], parameters: list[dict[str, Any]]) -> dict[str, Any]:
    def media(content: Any) -> dict[str, Any]:
        if not isinstance(content, dict):
            return {}
        return {str(kind): (value.get("schema") or {}) if isinstance(value, dict) else {} for kind, value in content.items()}

    plain = [{"name": p.get("name"), "in": p.get("in"), "required": bool(p.get("required")),
              "schema": p.get("schema") or {}} for p in parameters]
    request = operation.get("requestBody")
    body = ({"required": bool(request.get("required")), "content": media(request.get("content"))}
            if isinstance(request, dict) else None)
    responses = {str(status): {"content": media(response.get("content"))}
                 for status, response in (operation.get("responses") or {}).items() if isinstance(response, dict)}
    return {"parameters": plain, "request_body": body, "responses": responses}


def _base_paths(document: dict[str, Any], dialect: str) -> list[str]:
    if dialect == "swagger-2.0":
        base = document.get("basePath")
        return [base] if isinstance(base, str) and base not in ("", "/") else []
    paths = []
    for server in document.get("servers") or []:
        url = server.get("url") if isinstance(server, dict) else None
        if isinstance(url, str) and url and url not in paths:
            paths.append(url)
    return paths[:20]


def normalize(document: dict[str, Any], *, resolver: FileResolver | None = None, base_path: str = "") -> dict[str, Any]:
    """Validate ``document`` and return the internal contract model.

    ``resolver`` reads files next to the document; without one, any reference
    to another file is an error. ``base_path`` is the document's own path, which
    relative file references start from.
    """
    dialect = detect_dialect(document)
    errors: list[str] = []
    info = document.get("info")
    if not isinstance(info, dict) or not isinstance(info.get("title"), str) or not info["title"].strip():
        errors.append("info.title is required.")
    if not isinstance(info, dict) or info.get("version") in (None, ""):
        errors.append("info.version is required.")
    paths = document.get("paths")
    if paths is None and dialect == "openapi-3.1":
        paths = {}
    if not isinstance(paths, dict):
        errors.append("paths must be an object.")
        paths = {}

    bundler = _Bundler(document, resolver, base_path)
    root_schemas = (document.get("definitions") if dialect == "swagger-2.0"
                    else (document.get("components") or {}).get("schemas")) or {}
    if not isinstance(root_schemas, dict):
        errors.append("The schema section must be an object.")
        root_schemas = {}
    for name in root_schemas:  # keep the root document's own names before any imported ones
        bundler._schema_name("", str(name))
    for name, schema in root_schemas.items():
        bundler.schemas[str(name)] = bundler.rewrite(schema)

    operations: dict[str, Any] = {}
    for path, item in paths.items():
        if not isinstance(path, str) or not path.startswith("/"):
            if not str(path).startswith("x-"):
                errors.append(f"Path must start with '/': {str(path)[:120]}")
            continue
        item = bundler.rewrite(item)
        if not isinstance(item, dict):
            errors.append(f"{path}: the path item must be an object.")
            continue
        shared = item.get("parameters") if isinstance(item.get("parameters"), list) else []
        for method in METHODS:
            operation = item.get(method)
            if operation is None:
                continue
            label = f"{method.upper()} {path}"
            if not isinstance(operation, dict):
                errors.append(f"{label}: the operation must be an object.")
                continue
            own = operation.get("parameters") if isinstance(operation.get("parameters"), list) else []
            merged: dict[tuple[Any, Any], dict[str, Any]] = {}
            for parameter in [*shared, *own]:
                if not isinstance(parameter, dict) or not parameter.get("name") or not parameter.get("in"):
                    errors.append(f"{label}: every parameter needs a name and a location.")
                    continue
                merged[(parameter["name"], parameter["in"])] = parameter
            if dialect != "openapi-3.1" and not isinstance(operation.get("responses"), dict):
                errors.append(f"{label}: responses are required.")
            parts = (_swagger_operation(operation, list(merged.values()), document) if dialect == "swagger-2.0"
                     else _openapi_operation(operation, list(merged.values())))
            parts["parameters"].sort(key=lambda p: (str(p["in"]), str(p["name"])))
            operations[label] = {
                "method": method.upper(), "path": path,
                "operation_id": operation.get("operationId") if isinstance(operation.get("operationId"), str) else None,
                "summary": str(operation.get("summary") or operation.get("description") or "")[:500],
                "tags": sorted(str(tag) for tag in operation.get("tags") or [] if isinstance(tag, (str, int))),
                "deprecated": bool(operation.get("deprecated")),
                **parts,
            }

    errors.extend(error for error in bundler.errors if error not in errors)
    if errors:
        raise ContractInvalid(errors)
    return {
        "dialect": dialect,
        "title": info["title"].strip()[:300],
        "api_version": str(info["version"])[:100],
        "base_paths": _base_paths(document, dialect),
        "operations": operations,
        "schemas": bundler.schemas,
    }


def canonical_json(model: dict[str, Any]) -> str:
    return json.dumps(model, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(model: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(model).encode("utf-8")).hexdigest()


def load_contract(data: bytes, *, resolver: FileResolver | None = None, base_path: str = "") -> tuple[dict[str, Any], str]:
    """Bytes -> (internal model, content hash). Raises ``ContractInvalid``."""
    try:
        model = normalize(parse_document(data), resolver=resolver, base_path=base_path)
    except RecursionError as exc:
        raise ContractInvalid(["The document is nested too deeply."]) from exc
    return model, content_hash(model)


_OPERATION_PARTS = ("parameters", "request_body", "responses", "deprecated")


def diff_models(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    """Added, removed, and changed operations and schemas between two versions."""
    if previous is None:
        return {"first_version": True, "operations": {"added": sorted(current["operations"]), "removed": [], "changed": []},
                "schemas": {"added": sorted(current["schemas"]), "removed": [], "changed": []}}
    old_ops, new_ops = previous["operations"], current["operations"]
    old_schemas, new_schemas = previous["schemas"], current["schemas"]
    changed = []
    for key in sorted(set(old_ops) & set(new_ops)):
        parts = [part for part in _OPERATION_PARTS if old_ops[key].get(part) != new_ops[key].get(part)]
        if parts:
            changed.append({"operation": key, "parts": parts})
    return {
        "first_version": False,
        "operations": {"added": sorted(set(new_ops) - set(old_ops)), "removed": sorted(set(old_ops) - set(new_ops)),
                       "changed": changed},
        "schemas": {"added": sorted(set(new_schemas) - set(old_schemas)),
                    "removed": sorted(set(old_schemas) - set(new_schemas)),
                    "changed": sorted(name for name in set(old_schemas) & set(new_schemas)
                                      if old_schemas[name] != new_schemas[name])},
    }
