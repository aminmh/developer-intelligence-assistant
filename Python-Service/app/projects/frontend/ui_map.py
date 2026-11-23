"""Build the UI map of an Angular, React, or Vue project.

Plan: @.archcore/frontend-assist/frontend-developer-assistance.plan.md (Phase 2).

The UI map answers "where is this screen built and what does it depend on":

    route -> component -> child components -> stores -> HTTP calls

Extraction is static and heuristic. It reads syntax, not types, so it finds the
common patterns of each framework and says what it could not resolve instead of
guessing: a route whose component cannot be resolved keeps ``file: None``, and
an HTTP call whose address is built at run time keeps ``url: None``.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
from pathlib import Path
from typing import Any

from projects.frontend.parsing import (
    GRAMMARS, Parsed, build_url, call_arguments, call_name, enclosing_symbol, field, object_keys, object_pairs,
    parse_file, string_value, text, unwrap, walk,
)

EXTRACTOR_VERSION = 3
MAX_FILES = 5000
MAX_FILE_BYTES = 1_000_000
MAX_ITEMS = 5000
_REACH_DEPTH = 6

SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "coverage", ".next", ".nuxt", ".output", ".angular",
             ".cache", "tmp", "temp", "out", "storybook-static", "__tests__", "__mocks__", "e2e", "cypress"}
_SKIP_FILE = re.compile(r"\.(spec|test|stories|d)\.[cm]?[jt]sx?$|\.config\.[cm]?[jt]s$")
_SOURCE_SUFFIXES = (*GRAMMARS, ".vue")
_RESOLVE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".vue", ".mjs", ".mts")

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
_HTTP_FUNCTIONS = {"fetch", "$fetch", "useFetch", "useLazyFetch", "axios", "ky", "ofetch"}
# The object a `.get(...)`/`.post(...)` call is made on must look like an HTTP client,
# so `map.get(key)` and `params.delete(name)` are not mistaken for requests.
_HTTP_CLIENT = re.compile(r"^(\$?axios\w*|\$?https?|\w*http(client)?|\$?api\w*|\w*api|\w*client|ky|\$fetch|ofetch|request|instance)$",
                          re.IGNORECASE)
_ROUTE_KEYS = {"component", "element", "Component", "loadComponent", "loadChildren", "children", "redirectTo",
               "redirect", "components", "lazy"}
_STORE_CALLS = {"defineStore": "pinia", "createSlice": "redux", "configureStore": "redux", "createStore": "store",
                "createContext": "context", "createFeature": "ngrx", "createReducer": "ngrx"}
_TEMPLATE_TAG = re.compile(r"<([A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*)[\s/>]")
_FRAMEWORK_PACKAGES = (("angular", "@angular/core"), ("vue", "nuxt"), ("vue", "vue"), ("react", "next"), ("react", "react"))


def _pascal(tag: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in tag.split("-")) if "-" in tag else tag


def _join_route(parent: str, path: str) -> str:
    if path.startswith("/"):
        joined = path
    else:
        joined = f"{parent.rstrip('/')}/{path}" if path else parent
    return "/" + re.sub(r"/+", "/", joined).strip("/")


# ── project discovery ────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    for candidate in (raw, re.sub(r",(\s*[}\]])", r"\1", re.sub(r"^\s*//.*$|/\*.*?\*/", "", raw, flags=re.M | re.S))):
        try:
            data = json.loads(candidate)
            return data if isinstance(data, dict) else {}
        except ValueError:
            continue
    return {}


def detect_framework(package_dir: Path) -> dict[str, Any] | None:
    """``{"framework", "meta_framework", "version"}`` from one package.json, or None."""
    data = _read_json(package_dir / "package.json")
    dependencies = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {}),
                    **(data.get("peerDependencies") or {})} if data else {}
    for framework, package in _FRAMEWORK_PACKAGES:
        if package in dependencies:
            core = {"angular": "@angular/core", "vue": "vue", "react": "react"}[framework]
            return {"framework": framework, "meta_framework": package if package in ("nuxt", "next") else None,
                    "version": str(dependencies.get(core) or dependencies[package])[:40],
                    "inertia": any(name.startswith("@inertiajs/") for name in dependencies)}
    return None


def find_apps(root: Path, max_depth: int = 3) -> list[dict[str, Any]]:
    """Front-end applications in a source tree: every package.json that depends on a supported framework."""
    apps = []
    for directory, dirs, names in os.walk(root):
        depth = len(Path(directory).relative_to(root).parts)
        dirs[:] = sorted(name for name in dirs if name.lower() not in SKIP_DIRS and not name.startswith("."))
        if depth >= max_depth:
            dirs[:] = []
        if "package.json" in names:
            found = detect_framework(Path(directory))
            if found:
                apps.append({"root": "" if depth == 0 else Path(directory).relative_to(root).as_posix(), **found})
    return apps


# ── per-file facts ───────────────────────────────────────────────────────────

class _File:
    def __init__(self, path: str) -> None:
        self.path = path
        self.imports: dict[str, list[str]] = {}       # specifier -> imported local names
        self.lazy_imports: list[str] = []
        self.constants: dict[str, str] = {}
        self.http_calls: list[dict[str, Any]] = []
        self.components: list[dict[str, Any]] = []
        self.stores: list[dict[str, Any]] = []
        self.models: list[dict[str, Any]] = []
        self.route_lists: list[tuple[int, list[dict[str, Any]]]] = []
        self.template = ""
        self.identifiers: set[str] = set()


def _collect_imports(parsed: Parsed, facts: _File) -> None:
    for node in parsed.root.named_children:
        if node.type == "import_statement":
            specifier = string_value(field(node, "source"), parsed.source)
            if specifier:
                names = [text(n, parsed.source) for n in walk(node) if n.type == "identifier"]
                facts.imports.setdefault(specifier, []).extend(names)
        elif node.type in ("lexical_declaration", "variable_declaration", "export_statement"):
            for declarator in walk(node):
                if declarator.type != "variable_declarator":
                    continue
                name, value = field(declarator, "name"), field(declarator, "value")
                literal = string_value(unwrap(value), parsed.source) if value is not None else None
                if name is not None and literal is not None and len(literal) <= 300:
                    facts.constants[text(name, parsed.source)] = literal


def _import_specifier(node: Any, source: bytes) -> str | None:
    """The module of the first dynamic ``import('x')`` inside ``node``."""
    for item in walk(node):
        if item.type == "call_expression":
            function = field(item, "function")
            if function is not None and function.type == "import":
                arguments = call_arguments(item)
                if arguments:
                    return string_value(arguments[0], source)
    return None


def _body_keys(node: Any, parsed: Parsed) -> tuple[list[str] | None, bool]:
    """Keys of a request body written as an object literal, also inside ``JSON.stringify(...)``."""
    node = unwrap(node)
    if node is not None and node.type == "call_expression" and call_name(node, parsed.source) == ("JSON", "stringify"):
        arguments = call_arguments(node)
        node = arguments[0] if arguments else None
    return object_keys(node, parsed.source)


def _http_call(node: Any, parsed: Parsed, facts: _File) -> None:
    target, name = call_name(node, parsed.source)
    arguments = call_arguments(node)
    method, url_node, client = None, None, target or name
    body_node, options = None, {}
    options_seen = True  # False when a config argument exists but is not an object literal
    if not target and name in _HTTP_FUNCTIONS and arguments:
        config = object_pairs(arguments[0], parsed.source)
        if config and "url" in config:                       # axios({url, method, params, data})
            url_node, options = config["url"], config
        else:
            url_node = arguments[0]
            if len(arguments) > 1:
                options = object_pairs(arguments[1], parsed.source)
                options_seen = unwrap(arguments[1]).type == "object"
        method_node = options.get("method")
        method = (string_value(unwrap(method_node), parsed.source) or "GET") if method_node is not None else "GET"
        body_node = options.get("body") or options.get("data")
    elif target and name.lower() in HTTP_METHODS and arguments and _HTTP_CLIENT.match(target.rsplit(".", 1)[-1]):
        method, url_node = name, arguments[0]
        with_body = name.lower() in ("post", "put", "patch")
        if with_body and len(arguments) > 1:
            body_node = arguments[1]
        config_index = 2 if with_body else 1
        if len(arguments) > config_index:
            options = object_pairs(arguments[config_index], parsed.source)
            options_seen = unwrap(arguments[config_index]).type == "object"
    elif target and name == "request" and arguments and _HTTP_CLIENT.match(target.rsplit(".", 1)[-1]):
        config = object_pairs(arguments[0], parsed.source)
        first = string_value(arguments[0], parsed.source)
        if config and "url" in config:                       # axios.request({url, method})
            url_node, options = config["url"], config
            method = string_value(unwrap(config.get("method")), parsed.source) or "GET"
            body_node = config.get("data") or config.get("body")
        elif first and first.lower() in HTTP_METHODS and len(arguments) > 1:  # http.request('GET', url, options)
            method, url_node = first, arguments[1]
            if len(arguments) > 2:
                options = object_pairs(arguments[2], parsed.source)
                options_seen = unwrap(arguments[2]).type == "object"
                body_node = options.get("body")
    if method is None or url_node is None:
        return
    url_node = unwrap(url_node)
    if url_node.type in ("arrow_function", "function_expression", "object", "array", "number", "true", "false"):
        return
    url, query, query_complete = build_url(url_node, parsed.source, facts.constants)
    params_node = options.get("params") or options.get("query")
    if params_node is not None:
        keys, complete = object_keys(params_node, parsed.source)
        query = list(dict.fromkeys([*query, *(keys or [])]))
        query_complete = query_complete and keys is not None and complete
    body_keys, body_complete = _body_keys(body_node, parsed) if body_node is not None else (None, False)
    facts.http_calls.append({
        "method": method.upper(), "url": url, "raw": text(url_node, parsed.source)[:200],
        "file": facts.path, "line": parsed.line(node), "symbol": enclosing_symbol(node, parsed.source),
        "client": client[:60],
        # What the checker may rely on: names are trusted as complete only when every part was visible.
        "query": query, "query_complete": bool(query_complete and options_seen and url is not None),
        "has_body": body_node is not None, "body_keys": body_keys, "body_complete": bool(body_keys is not None and body_complete),
    })


def _model(node: Any, parsed: Parsed, facts: _File) -> None:
    """A TypeScript interface or object type alias: the shape the front end expects from the API."""
    name_node = field(node, "name")
    body = field(node, "body") if node.type == "interface_declaration" else field(node, "value")
    if name_node is None or body is None or body.type not in ("interface_body", "object_type"):
        return
    properties = []
    for member in body.named_children:
        if member.type != "property_signature":
            continue
        key = field(member, "name")
        if key is None:
            continue
        label = string_value(key, parsed.source) if key.type == "string" else text(key, parsed.source)
        head = text(member, parsed.source).split(":", 1)[0]
        if label:
            properties.append({"name": label[:120], "optional": head.rstrip().endswith("?")})
    if properties:
        facts.models.append({"name": text(name_node, parsed.source)[:120], "file": facts.path,
                             "line": parsed.line(node), "properties": properties[:200]})


def _route_component(value: Any, parsed: Parsed) -> dict[str, Any]:
    """What a route's component/element/loader value points to: a name and/or a lazily imported module."""
    value = unwrap(value)
    result: dict[str, Any] = {"component": None, "module": None, "lazy": False}
    if value is None:
        return result
    module = _import_specifier(value, parsed.source)
    if module:
        result.update(module=module, lazy=True)
        for item in walk(value):                               # import('./x').then(m => m.Name)
            if item.type == "member_expression" and field(item, "property") is not None:
                prop = text(field(item, "property"), parsed.source)
                if prop[:1].isupper():
                    result["component"] = prop
        return result
    if value.type == "identifier":
        result["component"] = text(value, parsed.source)
        return result
    for item in walk(value):                                   # element: <Page />
        if item.type in ("jsx_self_closing_element", "jsx_opening_element"):
            name = field(item, "name")
            if name is not None:
                result["component"] = text(name, parsed.source).split(".")[0]
                return result
    return result


def _route_objects(array: Any, parsed: Parsed, prefix: str, seen: set[int]) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    seen.add(array.id)
    for element in array.named_children:
        pairs = object_pairs(element, parsed.source)
        path = string_value(unwrap(pairs.get("path")), parsed.source) if "path" in pairs else None
        if path is None and not ("children" in pairs and pairs.keys() & _ROUTE_KEYS):
            continue
        full = _join_route(prefix, path or "")
        target = next((pairs[key] for key in ("component", "Component", "element", "loadComponent", "lazy") if key in pairs), None)
        route = {"path": full, "line": parsed.line(element), "kind": "config", "children_module": None,
                 "redirect": string_value(unwrap(pairs.get("redirectTo") or pairs.get("redirect")), parsed.source),
                 **_route_component(target, parsed)}
        if "loadChildren" in pairs:
            route["children_module"] = _import_specifier(pairs["loadChildren"], parsed.source)
            route["lazy"] = True
        if route["component"] or route["module"] or route["redirect"] or route["children_module"] or path is not None:
            routes.append(route)
        children = unwrap(pairs.get("children"))
        if children is not None and children.type == "array":
            routes.extend(_route_objects(children, parsed, full, seen))
    return routes


def _is_route_array(array: Any, parsed: Parsed) -> bool:
    for element in array.named_children:
        pairs = object_pairs(element, parsed.source)
        if "path" in pairs and string_value(unwrap(pairs["path"]), parsed.source) is not None and pairs.keys() & _ROUTE_KEYS:
            return True
    return False


def _jsx_attributes(element: Any, parsed: Parsed) -> dict[str, Any]:
    attributes = {}
    for child in element.named_children:
        if child.type == "jsx_attribute" and child.named_children:
            key = text(child.named_children[0], parsed.source)
            attributes[key] = child.named_children[1] if len(child.named_children) > 1 else None
    return attributes


def _jsx_routes(node: Any, parsed: Parsed, prefix: str, routes: list[dict[str, Any]]) -> None:
    """``<Route path="..." element={<X/>}>`` trees (React Router), with nested paths joined."""
    for child in node.named_children:
        opening = child if child.type == "jsx_self_closing_element" else (
            field(child, "open_tag") or next((c for c in child.named_children if c.type == "jsx_opening_element"), None)
        ) if child.type == "jsx_element" else None
        next_prefix = prefix
        if opening is not None and field(opening, "name") is not None and text(field(opening, "name"), parsed.source) == "Route":
            attributes = _jsx_attributes(opening, parsed)
            path = string_value(attributes.get("path"), parsed.source) if attributes.get("path") is not None else None
            target = attributes.get("element") or attributes.get("component") or attributes.get("Component")
            next_prefix = _join_route(prefix, path or "")
            if path is not None or target is not None:
                routes.append({"path": next_prefix, "line": parsed.line(opening), "kind": "config",
                               "children_module": None, "redirect": None, **_route_component(target, parsed)})
        _jsx_routes(child, parsed, next_prefix, routes)


def _decorator_class(decorator: Any) -> Any:
    parent = decorator.parent
    if parent is None:
        return None
    if parent.type in ("class_declaration", "abstract_class_declaration"):
        return parent
    return next((c for c in parent.named_children if c.type in ("class_declaration", "abstract_class_declaration")), None)


def _has_jsx(node: Any) -> bool:
    return any(item.type in ("jsx_element", "jsx_self_closing_element", "jsx_fragment") for item in walk(node))


def _jsx_names(node: Any, parsed: Parsed) -> list[str]:
    names = []
    for item in walk(node):
        if item.type in ("jsx_self_closing_element", "jsx_opening_element"):
            name = field(item, "name")
            if name is not None:
                tag = text(name, parsed.source).split(".")[0]
                if tag[:1].isupper() and tag not in names:
                    names.append(tag)
    return names


def _scan(parsed: Parsed, facts: _File, framework: str) -> None:
    _collect_imports(parsed, facts)
    seen_arrays: set[int] = set()
    uses_zustand = any("zustand" in specifier for specifier in facts.imports)
    for node in walk(parsed.root):
        kind = node.type
        if kind in ("identifier", "type_identifier"):
            facts.identifiers.add(text(node, parsed.source))
        elif kind in ("interface_declaration", "type_alias_declaration"):
            _model(node, parsed, facts)
        elif kind == "call_expression":
            function = field(node, "function")
            if function is not None and function.type == "import":
                arguments = call_arguments(node)
                specifier = string_value(arguments[0], parsed.source) if arguments else None
                if specifier:
                    facts.lazy_imports.append(specifier)
                continue
            _http_call(node, parsed, facts)
            target, name = call_name(node, parsed.source)
            store_kind = _STORE_CALLS.get(name) if not target or target in ("Vuex", "React", "toolkit") else None
            if not target and name == "create" and uses_zustand:
                store_kind = "zustand"
            if store_kind:
                owner = enclosing_symbol(node, parsed.source).split(".")[0]
                arguments = call_arguments(node)
                label = owner or (string_value(arguments[0], parsed.source) if arguments else None) or name
                facts.stores.append({"name": label, "kind": store_kind, "file": facts.path, "line": parsed.line(node)})
        elif kind == "new_expression" and text(node, parsed.source).startswith("new Vuex.Store"):
            owner = enclosing_symbol(node, parsed.source).split(".")[0]
            facts.stores.append({"name": owner or "store", "kind": "store", "file": facts.path, "line": parsed.line(node)})
        elif kind == "array" and node.id not in seen_arrays and _is_route_array(node, parsed):
            facts.route_lists.append((parsed.line(node), _route_objects(node, parsed, "", seen_arrays)))
        elif kind == "decorator" and framework == "angular":
            call = next((c for c in node.named_children if c.type == "call_expression"), None)
            decorated = _decorator_class(node)
            if call is None or decorated is None or field(decorated, "name") is None:
                continue
            _target, decorator_name = call_name(call, parsed.source)
            class_name = text(field(decorated, "name"), parsed.source)
            arguments = call_arguments(call)
            options = object_pairs(arguments[0], parsed.source) if arguments else {}
            if decorator_name == "Component":
                facts.components.append({
                    "name": class_name, "kind": "angular", "file": facts.path, "line": parsed.line(decorated),
                    "selector": string_value(unwrap(options.get("selector")), parsed.source),
                    "template_url": string_value(unwrap(options.get("templateUrl")), parsed.source),
                    "inline_template": string_value(unwrap(options.get("template")), parsed.source) or "",
                    "tags": [], "identifiers": None})
            elif decorator_name == "Injectable":
                facts.stores.append({"name": class_name, "kind": "service", "file": facts.path, "line": parsed.line(decorated)})
    if framework == "react":
        routes: list[dict[str, Any]] = []
        _jsx_routes(parsed.root, parsed, "", routes)
        if routes:
            facts.route_lists.append((routes[0]["line"], routes))
        for node in parsed.root.named_children:
            for target in ([node] if node.type != "export_statement" else node.named_children):
                candidates = []
                if target.type in ("function_declaration", "class_declaration") and field(target, "name") is not None:
                    candidates.append((text(field(target, "name"), parsed.source), target))
                elif target.type in ("lexical_declaration", "variable_declaration"):
                    candidates.extend((text(field(d, "name"), parsed.source), d) for d in target.named_children
                                      if d.type == "variable_declarator" and field(d, "name") is not None
                                      and field(d, "value") is not None)
                for name, body in candidates:
                    if name[:1].isupper() and _has_jsx(body):
                        facts.components.append({"name": name, "kind": "react", "file": facts.path,
                                                 "line": parsed.line(body), "tags": _jsx_names(body, parsed)})


def _file_routes(files: list[str], app: dict[str, Any]) -> list[dict[str, Any]]:
    """File-based routes of Next.js (pages/ and app/) and Nuxt (pages/)."""
    routes = []
    for path in files:
        match = re.match(r"^(?:src/)?(pages|app)/(.+)\.(vue|tsx|jsx|ts|js)$", path)
        if not match:
            continue
        folder, rest, suffix = match.groups()
        if app["meta_framework"] == "nuxt":
            if folder != "pages" or suffix != "vue":
                continue
        elif folder == "app":
            if not rest.endswith("page") or (rest != "page" and not rest.endswith("/page")):
                continue
            rest = rest[:-4].rstrip("/")
        elif suffix not in ("tsx", "jsx", "js", "ts") or rest.startswith(("api/", "_")):
            continue
        segments = []
        for segment in rest.split("/"):
            if segment == "index" or (segment.startswith("(") and segment.endswith(")")):
                continue
            segment = re.sub(r"^\[\[?\.\.\.(\w+)\]?\]$", r"*", segment)
            segment = re.sub(r"^\[(\w+)\]$", r":\1", segment)
            segments.append(re.sub(r"^_(\w+)$", r":\1", segment))
        routes.append({"path": "/" + "/".join(segments), "line": 1, "kind": "file", "component": None, "module": None,
                       "lazy": False, "redirect": None, "children_module": None, "source_file": path, "file": path})
    return routes


# ── assembly ─────────────────────────────────────────────────────────────────

class _Resolver:
    """Resolve an import specifier to a project file."""

    def __init__(self, app_root: Path, files: set[str]) -> None:
        self.files = files
        self.aliases: list[tuple[str, str]] = []
        for config in ("tsconfig.json", "tsconfig.app.json", "jsconfig.json"):
            options = _read_json(app_root / config).get("compilerOptions") or {}
            base = str(options.get("baseUrl") or ".").strip("./")
            for pattern, targets in (options.get("paths") or {}).items():
                if isinstance(targets, list) and targets and pattern.endswith("/*") and str(targets[0]).endswith("/*"):
                    target = posixpath.normpath(posixpath.join(base, str(targets[0])[:-2])).strip("./")
                    self.aliases.append((pattern[:-1], f"{target}/" if target else ""))
        source_dir = "src/" if any(path.startswith("src/") for path in files) else ""
        self.aliases.extend([("@/", source_dir), ("~/", source_dir), ("~~/", ""), ("@@/", "")])

    def _existing(self, path: str) -> str | None:
        path = posixpath.normpath(path)
        if path in self.files:
            return path
        for suffix in _RESOLVE_SUFFIXES:
            if path + suffix in self.files:
                return path + suffix
        for suffix in _RESOLVE_SUFFIXES:
            if f"{path}/index{suffix}" in self.files:
                return f"{path}/index{suffix}"
        return None

    def resolve(self, specifier: str, importer: str) -> str | None:
        if specifier.startswith("."):
            return self._existing(posixpath.join(posixpath.dirname(importer), specifier))
        for prefix, target in self.aliases:
            if specifier.startswith(prefix):
                found = self._existing(target + specifier[len(prefix):])
                if found:
                    return found
        return None


def _list_files(app_root: Path) -> tuple[list[str], bool]:
    files, truncated = [], False
    for directory, dirs, names in os.walk(app_root):
        dirs[:] = sorted(name for name in dirs if name.lower() not in SKIP_DIRS)
        for name in sorted(names):
            if not name.lower().endswith((*_SOURCE_SUFFIXES, ".html")) or _SKIP_FILE.search(name):
                continue
            if len(files) >= MAX_FILES:
                truncated = True
                break
            files.append((Path(directory) / name).relative_to(app_root).as_posix())
    return files, truncated


def _vue_component_name(path: str) -> str:
    stem = Path(path).stem
    if stem.lower() == "index":
        stem = Path(path).parent.name or stem
    return _pascal(stem)


def build_ui_map(root: str | Path, app: dict[str, Any] | None = None) -> dict[str, Any]:
    """The UI map of one front-end application under ``root``.

    ``app`` is one entry from :func:`find_apps`; when omitted, the first application found is used.
    """
    root = Path(root)
    apps = find_apps(root)
    app = app or (apps[0] if apps else None)
    if app is None:
        return {"extractor_version": EXTRACTOR_VERSION, "framework": None, "apps": [], "routes": [], "components": [],
                "stores": [], "http_calls": [], "models": [], "notes": ["No Angular, React, or Vue application was found."],
                "files_scanned": 0}
    framework = app["framework"]
    app_root = root / app["root"] if app["root"] else root
    paths, truncated = _list_files(app_root)
    known = set(paths)
    notes: list[str] = []
    if truncated:
        notes.append(f"Only the first {MAX_FILES} source files were read.")
    facts: dict[str, _File] = {}
    templates: dict[str, str] = {}
    unparsed = 0
    for path in paths:
        full = app_root / path
        try:
            if full.stat().st_size > MAX_FILE_BYTES:
                unparsed += 1
                continue
            source = full.read_bytes()
        except OSError:
            unparsed += 1
            continue
        if path.endswith(".html"):
            templates[path] = source.decode("utf-8", errors="replace")
            continue
        entry = facts[path] = _File(path)
        try:
            blocks, template = parse_file(Path(path).suffix.lower(), source)
        except (ValueError, RuntimeError):
            unparsed += 1
            continue
        entry.template = template.decode("utf-8", errors="replace")
        for parsed in blocks:
            _scan(parsed, entry, framework)
        if path.endswith(".vue"):
            entry.components.append({"name": _vue_component_name(path), "kind": "vue", "file": path, "line": 1, "tags": []})
    if unparsed:
        notes.append(f"{unparsed} file(s) were too large or unreadable and were skipped.")

    resolver = _Resolver(app_root, known)
    imports: dict[str, list[str]] = {}
    imported_names: dict[str, dict[str, set[str]]] = {}
    for path, entry in facts.items():
        targets: list[str] = []
        for specifier, names in [*entry.imports.items(), *((s, []) for s in entry.lazy_imports)]:
            resolved = resolver.resolve(specifier, path)
            if resolved and resolved != path:
                if resolved not in targets:
                    targets.append(resolved)
                imported_names.setdefault(path, {}).setdefault(resolved, set()).update(names)
        imports[path] = targets

    # Components, with an id that stays unique when two files define the same name.
    components: list[dict[str, Any]] = []
    by_file: dict[str, list[dict[str, Any]]] = {}
    for entry in facts.values():
        for component in entry.components[: MAX_ITEMS - len(components)]:
            component["id"] = f"{component['file']}#{component['name']}"
            components.append(component)
            by_file.setdefault(component["file"], []).append(component)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for component in components:
        by_name.setdefault(component["name"], []).append(component)
    by_selector = {c["selector"]: c for c in components if c.get("selector") and re.fullmatch(r"[a-z][\w-]*", c["selector"])}

    stores: list[dict[str, Any]] = []
    for entry in facts.values():
        for store in entry.stores:
            store["id"] = f"{store['file']}#{store['name']}"
            if len(stores) < MAX_ITEMS and all(s["id"] != store["id"] for s in stores):
                stores.append(store)
    stores_by_file: dict[str, list[dict[str, Any]]] = {}
    for store in stores:
        stores_by_file.setdefault(store["file"], []).append(store)

    http_calls: list[dict[str, Any]] = []
    calls_by_file: dict[str, list[int]] = {}
    for entry in facts.values():
        for call in entry.http_calls:
            if len(http_calls) >= MAX_ITEMS:
                break
            call["id"] = len(http_calls)
            calls_by_file.setdefault(call["file"], []).append(call["id"])
            http_calls.append(call)

    models = [model for entry in facts.values() for model in entry.models][:MAX_ITEMS]

    def pick(name: str, importer: str) -> dict[str, Any] | None:
        """The component called ``name`` as seen from ``importer``: same file, then an imported file, then a unique name."""
        candidates = by_name.get(name) or []
        for candidate in candidates:
            if candidate["file"] == importer or candidate["file"] in imports.get(importer, []):
                return candidate
        return candidates[0] if len(candidates) == 1 else None

    component_files = set(by_file)
    for component in components:
        path = component["file"]
        entry = facts[path]
        used: list[str] = []
        if component["kind"] == "react":
            used = [found["id"] for tag in component["tags"] if (found := pick(tag, path))]
        elif component["kind"] == "vue":
            tags = {_pascal(tag) for tag in _TEMPLATE_TAG.findall(entry.template)}
            used = [found["id"] for tag in sorted(tags) if (found := pick(tag, path))]
        else:
            markup = component.get("inline_template") or ""
            if component.get("template_url"):
                markup += templates.get(posixpath.normpath(posixpath.join(posixpath.dirname(path), component["template_url"])), "")
            used = [by_selector[tag]["id"] for tag in sorted(set(_TEMPLATE_TAG.findall(markup))) if tag in by_selector]
        component["uses"] = [item for item in dict.fromkeys(used) if item != component["id"]]
        component["stores"] = [store["id"] for target in imports.get(path, []) for store in stores_by_file.get(target, [])
                               if store["name"] in imported_names.get(path, {}).get(target, set())
                               or store["name"] in entry.identifiers]
        component["stores"] += [store["id"] for store in stores_by_file.get(path, []) if store["id"] not in component["stores"]]
        component["http_calls"] = list(calls_by_file.get(path, []))
        # Requests made by the services, stores, and API modules this component imports (not by child components).
        reached, frontier, visited = [], [path], {path}
        for _depth in range(_REACH_DEPTH):
            following = []
            for current in frontier:
                for target in imports.get(current, []):
                    if target in visited or target in component_files:
                        continue
                    visited.add(target)
                    following.append(target)
                    reached.extend(calls_by_file.get(target, []))
            frontier = following
        component["via_imports"] = sorted(set(reached))
        for key in ("tags", "identifiers", "inline_template", "template_url"):
            component.pop(key, None)

    # Routes: configured lists first, then file-based ones.
    routes: list[dict[str, Any]] = []
    pending = [(path, "", route_list) for path, entry in facts.items() for _line, route_list in entry.route_lists]
    lazy_children: dict[str, str] = {}
    for path, _prefix, route_list in pending:
        for route in route_list:
            if route.get("children_module"):
                target = resolver.resolve(route["children_module"], path)
                if target:
                    lazy_children[target] = route["path"]
    for path, _prefix, route_list in pending:
        prefix = lazy_children.get(path, "")
        for route in route_list:
            target_file = resolver.resolve(route["module"], path) if route.get("module") else None
            component = None
            if route.get("component"):
                component = next((c for c in by_file.get(target_file or "", []) if c["name"] == route["component"]), None) \
                    or pick(route["component"], path)
            elif target_file and len(by_file.get(target_file, [])) == 1:
                component = by_file[target_file][0]
            routes.append({
                "path": _join_route(prefix, route["path"].lstrip("/")) if prefix else route["path"],
                "component": component["name"] if component else route.get("component"),
                "component_id": component["id"] if component else None,
                "file": component["file"] if component else target_file,
                "source_file": path, "line": route["line"], "kind": route["kind"], "lazy": bool(route.get("lazy")),
                "redirect": route.get("redirect"),
            })
    if app.get("meta_framework"):
        for route in _file_routes(paths, app):
            page = by_file.get(route["file"], [])
            routes.append({**{k: route[k] for k in ("path", "file", "source_file", "line", "kind", "lazy", "redirect")},
                           "component": page[0]["name"] if page else None,
                           "component_id": page[0]["id"] if page else None})
    routes.sort(key=lambda route: (route["path"], route["source_file"], route["line"]))

    if app.get("inertia") and not routes:
        notes.append("This application uses Inertia: the back end chooses which page to render, "
                     "so there are no client-side routes. Pages are listed under Components.")
    unresolved_calls = sum(1 for call in http_calls if call["url"] is None)
    if unresolved_calls:
        notes.append(f"{unresolved_calls} HTTP call(s) build their address at run time and have no path template.")
    unresolved_routes = sum(1 for route in routes if not route["component_id"] and not route["redirect"])
    if unresolved_routes:
        notes.append(f"{unresolved_routes} route(s) could not be linked to a component.")
    return {
        "extractor_version": EXTRACTOR_VERSION, "framework": framework, "meta_framework": app.get("meta_framework"),
        "version": app.get("version"), "app_root": app["root"], "apps": apps, "files_scanned": len(facts),
        "routes": routes[:MAX_ITEMS], "components": components, "stores": stores, "http_calls": http_calls,
        "models": models, "imports": {path: targets for path, targets in imports.items() if targets}, "notes": notes,
    }
