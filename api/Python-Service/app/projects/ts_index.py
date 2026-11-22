"""Tree-sitter source-to-symbol producer for Go, Java, C#, Ruby, and Rust.

One walker serves every language; a ``_Lang`` record names the node types and
fields that differ. Two passes: the first collects classes, methods, and
functions per file; the second resolves call sites against the whole project,
so a call becomes an edge only when its target is a definition found here.

Each run returns chunks in the shape the JS/TS producer uses (Qdrant + the
``CodeSymbol`` graph) and a symbol export in the shape the PHP inspector uses
(the ``Symbol`` graph behind ``code_symbol_lookup``). Every edge it emits
carries origin ``tree-sitter``.

Symbol identities must be plain identifiers (``telemetry/symbols.py``), so a
name such as Ruby's ``valid?`` is reported as unsupported rather than indexed,
and duplicate identities (overloads) are merged instead of failing the index.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from tree_sitter_language_pack import get_parser

ORIGIN = "tree-sitter"
_MAX_FILES = 400
_MAX_FILE_BYTES = 1_000_000
_MAX_CODE_CHARS = 3_000
_MAX_DOC_CHARS = 400
_MAX_CALLS = 32
_MAX_REPORTED = 200
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_EXTRA_SKIP_DIRS = frozenset({"target", "bin", "obj", "vendor", "node_modules", ".gradle", ".idea"})
_COMMENTS = frozenset({"comment", "line_comment", "block_comment"})


@dataclass(frozen=True)
class _Lang:
    grammar: str
    extensions: frozenset[str]
    containers: dict[str, str]            # node type -> definition kind (class, interface, …)
    methods: frozenset[str]               # callable definitions inside a container
    functions: frozenset[str]             # callable definitions outside any container
    calls: frozenset[str]
    namespaces: frozenset[str] = frozenset()   # nodes that only open a namespace (C#)
    impl_blocks: frozenset[str] = frozenset()  # nodes that add methods to a named type (Rust impl)
    bare_statement_calls: bool = False         # Ruby: `check` alone on a line is a call


LANGS: dict[str, _Lang] = {
    "go": _Lang("go", frozenset({".go"}), {"type_spec": "class"}, frozenset(),
                frozenset({"function_declaration", "method_declaration"}), frozenset({"call_expression"})),
    "java": _Lang("java", frozenset({".java"}),
                  {"class_declaration": "class", "interface_declaration": "interface",
                   "enum_declaration": "enum", "record_declaration": "class"},
                  frozenset({"method_declaration", "constructor_declaration"}), frozenset(),
                  frozenset({"method_invocation"})),
    "csharp": _Lang("csharp", frozenset({".cs"}),
                    {"class_declaration": "class", "interface_declaration": "interface",
                     "struct_declaration": "class", "record_declaration": "class", "enum_declaration": "enum"},
                    frozenset({"method_declaration", "constructor_declaration"}), frozenset(),
                    frozenset({"invocation_expression"}),
                    namespaces=frozenset({"namespace_declaration", "file_scoped_namespace_declaration"})),
    "ruby": _Lang("ruby", frozenset({".rb"}), {"class": "class", "module": "trait"},
                  frozenset({"method", "singleton_method"}), frozenset({"method"}), frozenset({"call"}),
                  bare_statement_calls=True),
    "rust": _Lang("rust", frozenset({".rs"}),
                  {"struct_item": "class", "enum_item": "enum", "trait_item": "interface"},
                  frozenset({"function_item"}), frozenset({"function_item"}), frozenset({"call_expression"}),
                  impl_blocks=frozenset({"impl_item"})),
}


# --- small tree helpers ------------------------------------------------------------------------------------

def _text(node: Any, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _field(node: Any, name: str) -> Any:
    try:
        return node.child_by_field_name(name)
    except (AttributeError, TypeError):
        return None


def _walk(node: Any):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def _segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value) or "_"
    return "_" + cleaned if cleaned[0].isdigit() else cleaned


def _join(*parts: str) -> str:
    return "\\".join(p for p in parts if p)


def _type_name(node: Any, source: bytes) -> str | None:
    """`*Server`, `Server<T>`, `crate::a::Server` -> `Server`."""
    if node is None:
        return None
    names = [n for n in _walk(node) if n.type in {"type_identifier", "identifier", "constant"}]
    return _text(names[-1], source) if names else None


def _doc(node: Any, source: bytes) -> str:
    lines: list[str] = []
    prev, expected = node.prev_named_sibling, node.start_point.row
    while prev is not None and prev.type in _COMMENTS and prev.end_point.row >= expected - 1:
        lines.insert(0, _text(prev, source))
        prev, expected = prev.prev_named_sibling, prev.start_point.row
    return "\n".join(lines)[:_MAX_DOC_CHARS]


# --- namespaces --------------------------------------------------------------------------------------------

def _file_namespace(lang: str, root: Any, source: bytes, relative: str) -> str:
    """The namespace a file's top-level definitions live in."""
    directory = [p for p in relative.split("/")[:-1] if p]
    if lang == "go":  # a Go package is its directory
        return _join(*map(_segment, directory)) or "main"
    if lang == "rust":
        stem = relative.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        parts = [p for p in directory if p != "src"] + ([] if stem in {"mod", "lib", "main"} else [stem])
        return _join("crate", *map(_segment, parts))
    if lang == "java":
        package = next((c for c in root.named_children if c.type == "package_declaration"), None)
        if package is not None and package.named_children:
            return _join(*map(_segment, _text(package.named_children[0], source).split(".")))
        return _join(*map(_segment, directory))
    return ""  # C# and Ruby name their namespaces inside the file


def _go_package(root: Any, source: bytes) -> str | None:
    clause = next((c for c in root.named_children if c.type == "package_clause"), None)
    return _text(clause.named_children[0], source) if clause is not None and clause.named_children else None


# --- pass 1: definitions -----------------------------------------------------------------------------------

@dataclass
class _Def:
    kind: str                 # class | interface | enum | trait | method | function
    name: str
    namespace: str
    owner: str | None         # owning class fqn for methods
    file: str
    node: Any
    source: bytes = field(repr=False)
    receiver: str | None = None   # Go receiver variable
    bases: list[str] = field(default_factory=list)

    @property
    def fqn(self) -> str:
        if self.kind == "method":
            return f"{self.owner}::{self.name}"
        return _join(self.namespace, self.name)


class _FileWalker:
    def __init__(self, lang: str, spec: _Lang, source: bytes, relative: str, namespace: str,
                 report: Callable[[dict[str, Any]], None]) -> None:
        self.lang, self.spec, self.source, self.relative = lang, spec, source, relative
        self.namespace, self.report = namespace, report
        self.defs: list[_Def] = []

    def walk(self, node: Any, namespace: str, owner: str | None) -> None:
        for child in node.named_children:
            # A file-scoped namespace (C# `namespace A.B;`) applies to the siblings after it.
            namespace = self.visit(child, namespace, owner) or namespace

    def _name(self, node: Any) -> str | None:
        name = _field(node, "name")
        return _text(name, self.source).strip() if name is not None else None

    def visit(self, node: Any, namespace: str, owner: str | None) -> str | None:
        kind = node.type
        if kind in self.spec.namespaces:
            name = self._name(node)
            inner = _join(namespace, *map(_segment, (name or "").split("."))) if name else namespace
            body = _field(node, "body")
            if body is None:
                return inner
            self.walk(body, inner, owner)
            return None
        if kind in self.spec.containers:
            name = self._name(node)
            if not name or not _IDENT.match(name.split("::")[-1]):
                self.report({"file": self.relative, "line": node.start_point.row + 1, "construct": f"unnamed_{kind}"})
                return
            name = name.split("::")[-1]
            scope = owner or namespace
            definition = _Def(self.spec.containers[kind], name, namespace, None, self.relative, node, self.source,
                              bases=self._bases(node))
            definition.namespace = scope  # nested types live under their outer type
            self.defs.append(definition)
            body = _field(node, "body")
            if body is not None:
                self.walk(body, namespace, _join(scope, name))
            return None
        if kind in self.spec.impl_blocks:
            target = _type_name(_field(node, "type"), self.source)
            body = _field(node, "body")
            if target and body is not None:
                self.walk(body, namespace, _join(namespace, target))
            return None
        if owner and kind in self.spec.methods:
            self._callable(node, "method", namespace, owner)
            return
        if not owner and kind in self.spec.functions:
            if self.lang == "go" and kind == "method_declaration":
                self._go_method(node, namespace)
            else:
                self._callable(node, "function", namespace, None)
            return
        if kind in {"declaration_list", "body_statement", "type_declaration"}:
            self.walk(node, namespace, owner)
        return None

    def _bases(self, node: Any) -> list[str]:
        """Simple names of declared base types (Java, C#, Ruby); used for extends edges."""
        parts = [_field(node, "superclass"), _field(node, "interfaces")]
        parts += [c for c in node.named_children
                  if c.type in {"base_list", "superclass", "super_interfaces", "extends_interfaces"}]
        names = {_text(n, self.source) for part in parts if part is not None
                 for n in _walk(part) if n.type in {"type_identifier", "identifier", "constant"}}
        return sorted(names)[:8]

    def _callable(self, node: Any, kind: str, namespace: str, owner: str | None) -> None:
        name = self._name(node)
        if kind == "method" and node.type == "constructor_declaration":
            name = "constructor"
        if not name:
            return
        if not _IDENT.match(name):
            self.report({"file": self.relative, "line": node.start_point.row + 1,
                         "construct": "unsupported_name", "name": name[:120]})
            return
        if node.type == "function_item" and _field(node, "body") is None:
            return  # a trait signature, not a definition
        self.defs.append(_Def(kind, name, namespace, owner, self.relative, node, self.source))

    def _go_method(self, node: Any, namespace: str) -> None:
        receiver = _field(node, "receiver")
        param = next((c for c in receiver.named_children if c.type == "parameter_declaration"), None) if receiver else None
        type_name = _type_name(_field(param, "type"), self.source) if param is not None else None
        var = _field(param, "name") if param is not None else None
        name = self._name(node)
        if not type_name or not name or not _IDENT.match(name):
            self.report({"file": self.relative, "line": node.start_point.row + 1, "construct": "unsupported_method"})
            return
        self.defs.append(_Def("method", name, namespace, _join(namespace, type_name), self.relative, node,
                              self.source, receiver=_text(var, self.source) if var is not None else None))


# --- pass 2: call resolution -------------------------------------------------------------------------------

@dataclass
class _Project:
    classes: dict[str, set[str]] = field(default_factory=dict)       # simple name -> class fqns
    methods: dict[str, set[str]] = field(default_factory=dict)       # class fqn -> method names
    functions: dict[str, set[str]] = field(default_factory=dict)     # namespace -> function names
    packages: dict[str, set[str]] = field(default_factory=dict)      # Go package / Rust module name -> namespaces

    def add(self, d: _Def) -> None:
        if d.kind == "method":
            self.methods.setdefault(d.owner or "", set()).add(d.name)
        elif d.kind == "function":
            self.functions.setdefault(d.namespace, set()).add(d.name)
        else:
            self.classes.setdefault(d.name, set()).add(d.fqn)

    def unique_class(self, name: str) -> str | None:
        found = self.classes.get(name) or set()
        return next(iter(found)) if len(found) == 1 else None


def _call_parts(lang: str, node: Any, source: bytes, receiver_var: str | None) -> tuple[str, str | None, str] | None:
    """(receiver kind, receiver text, called name); kinds: bare, self, ident, path."""
    t = lambda n: _text(n, source).strip() if n is not None else None  # noqa: E731
    if lang == "java":
        obj, name = _field(node, "object"), t(_field(node, "name"))
        if not name:
            return None
        if obj is None:
            return "bare", None, name
        return ("self", None, name) if obj.type == "this" else ("ident", t(obj), name)
    if lang == "ruby":
        recv, name = _field(node, "receiver"), t(_field(node, "method"))
        if not name:
            return None
        if recv is None:
            return "bare", None, name
        if recv.type == "self":
            return "self", None, name
        return "ident", t(recv).split("::")[-1], name
    function = _field(node, "function")
    if function is None:
        return None
    if function.type == "identifier":
        return "bare", None, t(function)
    if lang == "go" and function.type == "selector_expression":
        operand, name = t(_field(function, "operand")), t(_field(function, "field"))
        kind = "self" if receiver_var and operand == receiver_var else "ident"
        return kind, operand, name
    if lang == "csharp" and function.type == "member_access_expression":
        expr, name = _field(function, "expression"), t(_field(function, "name"))
        return ("self", None, name) if expr is not None and expr.type == "this_expression" or t(expr) == "this" \
            else ("ident", t(expr), name)
    if lang == "csharp" and function.type == "generic_name":
        return "bare", None, t(function).split("<")[0]
    if lang == "rust" and function.type == "field_expression":
        value, name = _field(function, "value"), t(_field(function, "field"))
        return ("self", None, name) if value is not None and value.type == "self" else ("ident", t(value), name)
    if lang == "rust" and function.type == "scoped_identifier":
        return "path", t(_field(function, "path")), t(_field(function, "name"))
    return None


def _resolve(d: _Def, kind: str, recv: str | None, name: str, project: _Project) -> tuple[str, str, str | None] | None:
    """Resolved target as (reference kind, target fqn, member); None when it is not a definition here."""
    if not name or not _IDENT.match(name):
        return None
    if kind in {"bare", "self"} and d.owner and name in project.methods.get(d.owner, ()):
        return "method_call", d.owner, name
    if kind == "bare" and name in project.functions.get(d.namespace, ()):
        return "function_call", _join(d.namespace, name), None
    if kind in {"ident", "path"} and recv:
        last = re.split(r"::|\.", recv)[-1]
        cls = project.unique_class(last)
        if cls and name in project.methods.get(cls, ()):
            return "static_call", cls, name
        spaces = [ns for ns in project.packages.get(last, ()) if name in project.functions.get(ns, ())]
        if len(spaces) == 1:
            return "function_call", _join(spaces[0], name), None
    return None


def _calls_of(lang: str, spec: _Lang, d: _Def, project: _Project) -> tuple[list[dict[str, Any]], int]:
    body = _field(d.node, "body") or d.node
    resolved: dict[tuple[str, str, str | None], int] = {}
    unresolved = 0
    for item in _walk(body):
        parts = None
        if item.type in spec.calls:
            parts = _call_parts(lang, item, d.source, d.receiver)
        elif spec.bare_statement_calls and item.type == "identifier" and item.parent is not None \
                and item.parent.type == "body_statement":
            parts = ("bare", None, _text(item, d.source))
        if parts is None:
            continue
        target = _resolve(d, *parts, project)
        if target is None:
            unresolved += item.type in spec.calls
            continue
        resolved.setdefault(target, item.start_point.row + 1)
    return [{"kind": k, "to_fqn": fqn, "to_member": member, "line": line}
            for (k, fqn, member), line in sorted(resolved.items(), key=lambda kv: kv[1])], unresolved


# --- output ------------------------------------------------------------------------------------------------

def _chunk(lang: str, d: _Def, calls: list[dict[str, Any]], unresolved: int) -> dict[str, Any]:
    node, source = d.node, d.source
    doc = _doc(node, source)
    params = _field(node, "parameters")
    is_member = d.kind == "method"
    owner_name = d.owner.rsplit("\\", 1)[-1] if d.owner else None
    return {
        "type": "method" if is_member else "function" if d.kind == "function" else "class",
        "definition_kind": d.kind,
        "language": lang,
        "origin": ORIGIN,
        "name": d.name,
        "class": owner_name if is_member else (d.name if d.kind not in {"function"} else None),
        "class_fqn": d.owner if is_member else (d.fqn if d.kind != "function" else _join(d.namespace, d.name)),
        "namespace": d.namespace,
        "file": d.file,
        "start_line": node.start_point.row + 1,
        "end_line": node.end_point.row + 1,
        "modifiers": [],
        "parameters": [_text(c, source)[:120] for c in params.named_children] if params is not None else [],
        "return_type": None,
        "extends": d.bases,
        "calls": [f"{c['to_fqn']}::{c['to_member']}" if c["to_member"] else c["to_fqn"] for c in calls][:_MAX_CALLS],
        "callers": [],
        "unresolved_reference_count": unresolved,
        "has_docblock": bool(doc),
        "docblock": doc,
        "code": _text(node, source)[:_MAX_CODE_CHARS],
    }


def _definition_record(d: _Def, project: _Project) -> dict[str, Any]:
    record: dict[str, Any] = {"kind": d.kind, "name": d.name, "fqn": d.fqn,
                              "file": d.file, "line": d.node.start_point.row + 1, "end_line": d.node.end_point.row + 1,
                              "origin": ORIGIN}
    if d.kind == "method":
        record["owner"] = d.owner
    elif d.kind == "function":
        record["name"] = d.fqn  # the inspector keys function scopes by fqn
    else:
        record["relations"] = [{"kind": "extends", "target": cls} for base in d.bases
                               if (cls := project.unique_class(base)) and cls != d.fqn]
    return record


def _merge(chunks: list[dict[str, Any]], report: Callable[[dict[str, Any]], None]) -> list[dict[str, Any]]:
    """One chunk per identity: overloads in one file merge; repeats in other files are reported."""
    kept: dict[tuple, dict[str, Any]] = {}
    for chunk in chunks:
        key = (chunk["type"], chunk["class_fqn"], chunk["name"]) if chunk["type"] != "class" else ("class", chunk["class_fqn"])
        first = kept.get(key)
        if first is None:
            kept[key] = chunk
        elif first["file"] == chunk["file"]:
            first["code"] = (first["code"] + "\n\n" + chunk["code"])[:_MAX_CODE_CHARS]
            first["end_line"] = max(first["end_line"], chunk["end_line"])
            first["calls"] = sorted(set(first["calls"]) | set(chunk["calls"]))[:_MAX_CALLS]
            first["unresolved_reference_count"] += chunk["unresolved_reference_count"]
        else:
            report({"file": chunk["file"], "line": chunk["start_line"], "construct": "duplicate_symbol",
                    "name": chunk["name"], "first_file": first["file"]})
    return list(kept.values())


def _skip_dirs() -> frozenset[str]:
    try:
        from projects.structure_profile import _SKIP_DIR_NAMES
        return frozenset(n.lower() for n in _SKIP_DIR_NAMES) | _EXTRA_SKIP_DIRS
    except ImportError:
        return _EXTRA_SKIP_DIRS


def produce_chunks(workspace_path: str | Path, language: str) -> dict[str, Any]:
    """Index one language of a workspace. Never raises; partial coverage is reported, not hidden."""
    spec = LANGS.get(language)
    root = Path(workspace_path)
    empty = {"chunks": [], "files": 0, "unsupported_constructs": [], "malformed_files": [],
             "symbol_index": {"definitions": [], "references": []}}
    if spec is None:
        return {**empty, "error": f"no tree-sitter indexer for {language}"}
    if not root.is_dir():
        return {**empty, "error": f"workspace not found: {workspace_path}"}

    unsupported: list[dict[str, Any]] = []
    unsupported_total = 0

    def report(item: dict[str, Any]) -> None:
        nonlocal unsupported_total
        unsupported_total += 1
        if len(unsupported) < _MAX_REPORTED:
            unsupported.append(item)

    malformed: list[str] = []
    defs: list[_Def] = []
    project = _Project()
    files_seen = skipped_large = 0
    parser = get_parser(spec.grammar)
    skip = _skip_dirs()
    limited = False
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d.lower() not in skip and not d.startswith("."))
        for filename in sorted(names):
            path = Path(directory) / filename
            if path.suffix.lower() not in spec.extensions:
                continue
            if files_seen >= _MAX_FILES:
                limited = True
                break
            files_seen += 1
            relative = path.relative_to(root).as_posix()
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    skipped_large += 1
                    report({"file": relative, "construct": "file_size_limit"})
                    continue
                source = path.read_bytes()
                tree = parser.parse(source)
                namespace = _file_namespace(language, tree.root_node, source, relative)
                walker = _FileWalker(language, spec, source, relative, namespace, report)
                walker.walk(tree.root_node, namespace, None)
                if language == "go" and (package := _go_package(tree.root_node, source)):
                    project.packages.setdefault(package, set()).add(namespace)
                if language == "rust":
                    project.packages.setdefault(namespace.rsplit("\\", 1)[-1], set()).add(namespace)
                defs.extend(walker.defs)
                if tree.root_node.has_error:
                    malformed.append(relative)
            except (OSError, UnicodeError, ValueError, RecursionError) as exc:
                malformed.append(relative)
                report({"file": relative, "construct": type(exc).__name__})
        if limited:
            break
    if limited:
        report({"construct": "file_count_limit", "limit": _MAX_FILES})

    for d in defs:
        project.add(d)
    chunks: list[dict[str, Any]] = []
    definitions: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    seen_defs: set[tuple[str, str]] = set()
    for d in defs:
        calls, unresolved = _calls_of(language, spec, d, project) if d.kind in {"method", "function"} else ([], 0)
        chunks.append(_chunk(language, d, calls, unresolved))
        if (d.kind, d.fqn) in seen_defs:
            continue
        seen_defs.add((d.kind, d.fqn))
        definitions.append(_definition_record(d, project))
        scope = d.fqn
        references += [{"scope": scope, "kind": c["kind"], "to_fqn": c["to_fqn"], "to_member": c["to_member"],
                        "file": d.file, "line": c["line"], "origin": ORIGIN} for c in calls]
    chunks = _merge(chunks, report)
    complete = not malformed and not unsupported_total
    return {
        "chunks": chunks,
        "files": files_seen,
        "error": None,
        "unsupported_constructs": unsupported,
        "unsupported_total": unsupported_total,
        "malformed_files": sorted(set(malformed)),
        "coverage": "complete" if complete else "partial",
        "files_skipped_for_size": skipped_large,
        "symbol_index": {"definitions": definitions, "references": references},
    }
