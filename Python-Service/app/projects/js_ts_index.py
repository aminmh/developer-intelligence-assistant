"""Bounded JavaScript and TypeScript source-to-symbol adapter."""

from __future__ import annotations

import os
import hashlib
import re
from pathlib import Path
from typing import Any

from tree_sitter_language_pack import get_parser

_MAX_FILES = 400
_MAX_FILE_BYTES = 1_000_000
_MAX_CODE_CHARS = 3_000
_EXTENSIONS = {
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
}
_SKIP_DIRS = {
    ".git", "node_modules", "vendor", "dist", "build", "coverage",
    ".next", ".nuxt", ".cache", "tmp", "temp", "out",
}


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
        stack.extend(reversed(getattr(current, "named_children", ())))


def _module_name(relative_path: str) -> str:
    stem = relative_path.rsplit(".", 1)[0]
    readable = re.sub(r"[^A-Za-z0-9_]", "_", stem)[:48].strip("_") or "source"
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:10]
    return f"js_{readable}_{digest}"


def _calls(
    node: Any, source: bytes, module: str, local_names: set[str], class_name: str | None,
) -> list[str]:
    found: set[str] = set()
    for item in _walk(node):
        if item.type != "call_expression":
            continue
        function = _field(item, "function")
        if function is None:
            continue
        name = _text(function, source).strip()
        if name in local_names:
            found.add(f"{module}\\{name}")
        elif class_name and name.startswith("this.") and name.removeprefix("this.").isidentifier():
            found.add(f"{module}\\{class_name}::{name.removeprefix('this.')}")
        elif name.isidentifier():
            found.add(name)
    return sorted(found)[:32]


def _unresolved_call_targets(
    node: Any, source: bytes, local_names: set[str], relative: str,
    class_methods: set[str] | None = None,
) -> list[dict[str, Any]]:
    unresolved: list[dict[str, Any]] = []
    for item in _walk(node):
        if item.type != "call_expression":
            continue
        function = _field(item, "function")
        if function is None:
            continue
        target = _text(function, source).strip()
        local_method = target.removeprefix("this.") if target.startswith("this.") else None
        if target not in local_names and not (local_method and local_method in (class_methods or set())):
            unresolved.append({
                "file": relative,
                "line": int(item.start_point.row) + 1,
                "construct": "unresolved_call_target",
                "target": target[:160],
            })
    return unresolved


def _definition(
    node: Any, source: bytes, relative: str, module: str,
    kind: str, name: str, class_name: str | None, local_names: set[str],
) -> dict[str, Any]:
    line_start = int(node.start_point.row) + 1
    line_end = int(node.end_point.row) + 1
    qualified = f"{module}\\{class_name}\\{name}" if class_name else f"{module}\\{name}"
    code = _text(node, source)[:_MAX_CODE_CHARS]
    params = _field(node, "parameters")
    body = _field(node, "body") or node
    return {
        "type": kind,
        "origin": "tree-sitter",
        "name": name[:120],
        "class": class_name,
        "class_fqn": f"{module}\\{class_name}" if class_name else qualified,
        "namespace": module,
        "file": relative,
        "start_line": line_start,
        "end_line": line_end,
        "modifiers": [],
        "parameters": [_text(child, source)[:120] for child in getattr(params, "named_children", ())] if params else [],
        "return_type": None,
        "calls": _calls(body, source, module, local_names, class_name),
        "callers": [],
        "has_docblock": False,
        "docblock": "",
        "code": code,
    }


def _declarations(root: Any, source: bytes, relative: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    module = _module_name(relative)
    definitions: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    top_level: list[tuple[Any, str, str]] = []
    for node in getattr(root, "named_children", ()):
        target = node
        if node.type == "export_statement":
            target = next((child for child in node.named_children if child.type.endswith("declaration")), node)
        if target.type in {"function_declaration", "class_declaration"}:
            name_node = _field(target, "name")
            if name_node is not None:
                top_level.append((target, target.type, _text(name_node, source)))
            else:
                unsupported.append({"file": relative, "line": int(target.start_point.row) + 1,
                                    "construct": "anonymous_declaration", "kind": target.type})
        elif target.type in {"lexical_declaration", "variable_declaration"}:
            for declaration in target.named_children:
                if declaration.type != "variable_declarator":
                    continue
                name_node = _field(declaration, "name")
                value_node = _field(declaration, "value")
                if name_node is None:
                    continue
                if value_node is None or value_node.type not in {"arrow_function", "function_expression"}:
                    unsupported.append({"file": relative, "line": int(declaration.start_point.row) + 1,
                                        "construct": "non_function_variable",
                                        "name": _text(name_node, source)[:120]})
                    continue
                top_level.append((value_node, "function_declaration", _text(name_node, source)))
        elif target.type in {"interface_declaration", "type_alias_declaration", "enum_declaration", "ambient_declaration", "internal_module"}:
            name_node = _field(target, "name")
            unsupported.append({"file": relative, "line": int(target.start_point.row) + 1,
                                "construct": target.type,
                                "name": _text(name_node, source) if name_node else None})

    local_names = {name for _node, _kind, name in top_level}
    for node, kind, name in top_level:
        normalized = "class" if kind == "class_declaration" else "function"
        if normalized != "class":
            unsupported.extend(_unresolved_call_targets(node, source, local_names, relative))
        definitions.append(_definition(node, source, relative, module, normalized, name, None, local_names))
        if normalized != "class":
            continue
        body = _field(node, "body")
        methods = [method for method in getattr(body, "named_children", ()) if method.type == "method_definition"] if body else []
        class_methods = {
            _text(name_node, source)
            for method in methods
            if (name_node := _field(method, "name")) is not None
        }
        for method in methods:
            if method.type != "method_definition":
                continue
            name_node = _field(method, "name")
            if name_node is None:
                unsupported.append({"file": relative, "line": int(method.start_point.row) + 1,
                                    "construct": "computed_method_name"})
                continue
            method_name = _text(name_node, source)
            if not method_name.isidentifier():
                unsupported.append({"file": relative, "line": int(method.start_point.row) + 1,
                                    "construct": "unsupported_method_name",
                                    "name": method_name[:120]})
                continue
            unsupported.extend(_unresolved_call_targets(method, source, local_names, relative, class_methods))
            definitions.append(_definition(method, source, relative, module, "method", method_name, name, local_names))
        for member in getattr(body, "named_children", ()) if body else ():
            if member.type not in {"method_definition", "comment"}:
                unsupported.append({"file": relative, "line": int(member.start_point.row) + 1,
                                    "construct": f"class_member:{member.type}"})
    return definitions, unsupported


def produce_chunks(workspace_path: str | Path, language: str) -> dict[str, Any]:
    """Parse bounded project sources and report partial syntax coverage honestly."""
    root = Path(workspace_path)
    if not root.is_dir():
        return {"chunks": [], "files": 0, "error": f"workspace not found: {workspace_path}",
                "unsupported_constructs": [], "malformed_files": []}
    chunks: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    malformed: list[str] = []
    files_seen = 0
    skipped_large = 0
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name.lower() not in _SKIP_DIRS)
        for filename in sorted(names):
            path = Path(directory) / filename
            grammar = _EXTENSIONS.get(path.suffix.lower())
            if grammar is None:
                continue
            if files_seen >= _MAX_FILES:
                break
            files_seen += 1
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    skipped_large += 1
                    unsupported.append({"file": path.relative_to(root).as_posix(), "construct": "file_size_limit"})
                    continue
                source = path.read_bytes()
                tree = get_parser(grammar).parse(source)
                relative = path.relative_to(root).as_posix()
                found, unhandled = _declarations(tree.root_node, source, relative)
                chunks.extend(found)
                unsupported.extend(unhandled)
                if tree.root_node.has_error:
                    malformed.append(relative)
            except (OSError, UnicodeError, ValueError) as exc:
                malformed.append(path.relative_to(root).as_posix())
                unsupported.append({"file": path.relative_to(root).as_posix(), "construct": type(exc).__name__})
    if files_seen >= _MAX_FILES:
        unsupported.append({"construct": "file_count_limit", "limit": _MAX_FILES})
    complete = not malformed and not unsupported and files_seen < _MAX_FILES
    return {
        "chunks": chunks,
        "files": files_seen,
        "error": None,
        "unsupported_constructs": unsupported,
        "malformed_files": sorted(set(malformed)),
        "coverage": "complete" if complete else "partial",
        "files_skipped_for_size": skipped_large,
    }
