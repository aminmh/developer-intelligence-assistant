"""Symfony routes from PHP facts and route configuration (capability ``route_analysis``).

Reads ``#[Route]`` attributes and ``@Route`` docblock annotations on controller
classes and actions through the PHP facts export (no PHP is parsed here), joins
them with the ``prefix`` of the YAML ``resource`` entry that loads the
controller's directory, and adds explicit YAML routes. Constants such as
``self::ID_ROUTE`` resolve through parent classes. Each route points at the
handler method's line.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

import yaml

from agent.core.facts import PhpFacts, argument, literal_str, literal_strings, short_name
from agent.core.records import RouteRecord
from agent.core.revision import RevisionContext

FRAMEWORK = "symfony"
ANY_METHOD = "ANY"
ROUTE_FILES = ("config/routes.yaml", "config/routes.yml")
_ROUTE_ATTRIBUTES = {
    "Symfony\\Component\\Routing\\Attribute\\Route",
    "Symfony\\Component\\Routing\\Annotation\\Route",
    "Sensio\\Bundle\\FrameworkExtraBundle\\Configuration\\Route",
}
# Doctrine-style annotation arguments: "first", key="value", key={"a", "b"}.
_ANNOTATION_FIRST = re.compile(r'^\s*"([^"]*)"')
_ANNOTATION_KEY = re.compile(r'\b(path|name|methods)\s*=\s*(?:"([^"]*)"|\{([^}]*)\})')


def join(*parts: str) -> str:
    return "/" + "/".join(part.strip("/") for part in parts if part and part.strip("/"))


def load_yaml(text: str | None) -> Any:
    if not text:
        return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def route_documents(context: RevisionContext) -> dict[str, Any]:
    """Parsed ``config/routes.yaml`` and ``config/routes/*.yaml`` (top level of that folder)."""
    names = [*ROUTE_FILES, *sorted(f for f in context.files if f.startswith("config/routes/")
                                   and f.endswith((".yaml", ".yml")) and f.count("/") == 2)]
    return {name: load_yaml(context.read_text(name, 512_000)) for name in names if context.exists(name)}


def _route_config(documents: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(attribute resources, explicit routes)``."""
    resources, routes = [], []
    for relative, data in documents.items():
        if not isinstance(data, dict):
            continue
        directory = posixpath.dirname(relative)
        for name, entry in data.items():
            if str(name).startswith("when@") or not isinstance(entry, dict):
                continue
            if "resource" in entry:
                resource = entry["resource"]
                path = resource.get("path") if isinstance(resource, dict) else resource
                prefix = entry.get("prefix") if isinstance(entry.get("prefix"), str) else ""
                if isinstance(path, str) and not path.startswith("@") and "*" not in path:
                    resources.append({"path": posixpath.normpath(posixpath.join(directory, path)), "prefix": prefix})
                continue
            if isinstance(entry.get("path"), str):
                methods = entry.get("methods") or [ANY_METHOD]
                methods = [methods] if isinstance(methods, str) else methods
                routes.append({"methods": tuple(str(m).upper() for m in methods), "path": join(entry["path"]),
                               "controller": str(entry.get("controller") or ""), "name": str(name), "file": relative})
    return resources, routes


def _resource_for(relative: str, resources: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The most specific resource whose directory (or file) loads ``relative``."""
    best = None
    for resource in resources:
        target = resource["path"].rstrip("/")
        if relative == target or relative.startswith(target + "/") or target in ("", "."):
            if best is None or len(target) > len(best["path"].rstrip("/")):
                best = resource
    return best


def _route_annotations(items: list[dict[str, Any]], facts: PhpFacts) -> list[dict[str, Any]]:
    """``{path, methods, name}`` for every Route attribute and @Route annotation."""
    found = []
    for attribute in [a for a in items if a.get("kind") == "attribute"]:
        args = attribute.get("args") or []
        raw_path = argument(args, 0, "path")
        path = literal_str(raw_path, facts)
        if path is None and raw_path is not None:
            path = "{unresolved}"  # an expression the facts cannot evaluate; keep the route visible
        found.append({"path": path or "",
                      "methods": tuple(m.upper() for m in literal_strings(argument(args, 99, "methods"), facts)),
                      "name": literal_str(argument(args, 99, "name"), facts)})
    for annotation in [a for a in items if a.get("kind") == "annotation"]:
        raw = annotation.get("args") or ""
        first = _ANNOTATION_FIRST.match(raw)
        values = {key: (one if one is not None else many) for key, one, many in _ANNOTATION_KEY.findall(raw)}
        methods = values.get("methods") or ""
        found.append({"path": values.get("path") or (first.group(1) if first else ""),
                      "methods": tuple(m.strip().strip('"').upper() for m in methods.split(",") if m.strip().strip('"')),
                      "name": values.get("name")})
    return found


def _route_markers(node: dict[str, Any]) -> list[dict[str, Any]]:
    marks = [{"kind": "attribute", **a} for a in node.get("attributes") or [] if a.get("name") in _ROUTE_ATTRIBUTES]
    marks += [{"kind": "annotation", **a} for a in node.get("annotations") or []
              if a.get("name") in ("Route",) or str(a.get("name", "")).endswith("\\Route")]
    return marks


def provide_routes(context: RevisionContext, facts: PhpFacts) -> list[RouteRecord]:
    resources, yaml_routes = _route_config(route_documents(context))
    records: dict[tuple, RouteRecord] = {}
    for cls, file in facts.classes():
        path = str(file.get("path", ""))
        if cls.get("kind") != "class" or not path.startswith("src/"):
            continue
        class_routes = _route_annotations(_route_markers(cls), facts)
        prefix = class_routes[0]["path"] if class_routes else ""
        class_methods = class_routes[0]["methods"] if class_routes else ()
        group = _resource_for(path, resources)
        if resources and group is None:
            continue  # no route resource loads this file, so Symfony never registers its routes
        for method in cls.get("methods") or []:
            for route in _route_annotations(_route_markers(method), facts):
                methods = route["methods"] or class_methods or (ANY_METHOD,)
                record = RouteRecord(framework=FRAMEWORK, methods=tuple(methods),
                                     path=join(group["prefix"] if group else "", prefix, route["path"]),
                                     handler=f"{cls['fqn']}::{method['name']}", file=path,
                                     line=int(method.get("name_line") or method["line"]),
                                     name=route["name"])
                records.setdefault((record.methods, record.path, record.handler), record)
    for route in yaml_routes:
        class_name, _, action = route["controller"].partition("::")
        cls = facts.class_(class_name.lstrip("\\"))
        method = next((m for m in (cls or {}).get("methods") or [] if m.get("name") == action), None)
        file = facts.file_of(class_name.lstrip("\\"))
        record = RouteRecord(framework=FRAMEWORK, methods=route["methods"], path=route["path"],
                             handler=f"{cls['fqn']}::{action}" if cls and action else (route["controller"] or None),
                             file=str(file["path"]) if file and method else route["file"],
                             line=int(method.get("name_line") or method["line"]) if method else 1, name=route["name"])
        records.setdefault((record.methods, record.path, record.handler), record)
    return sorted(records.values(), key=lambda r: (r.path, r.methods, r.handler or ""))


def describe(handler: str | None) -> str:
    owner, _, action = (handler or "").partition("::")
    return f"{short_name(owner)}::{action}" if action else short_name(owner)
