"""Dependency-free twin of PHP App\\Telemetry\\SymbolId.

Canonical IDs are readable join keys, never globally unique storage IDs.
The shared PHP JSON fixtures are the cross-language contract.
"""
from __future__ import annotations

import re
from typing import Any

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Identity fields must be strings")
    # Match PHP trim's byte set exactly; Python's default strips Unicode too.
    return value.strip(" \t\n\r\x00\x0b")


def _identifier(value: Any) -> str:
    text = _text(value)
    if not re.fullmatch(_IDENTIFIER, text):
        raise ValueError("Invalid identifier")
    return text


def _fqn(value: Any) -> str:
    text = _text(value).removeprefix("\\")
    if not re.fullmatch(_IDENTIFIER + r"(?:\\" + _IDENTIFIER + r")*", text):
        raise ValueError("Invalid fully-qualified PHP name")
    return text


def relative_path(value: Any) -> str:
    path = _text(value).replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if (not path or path.startswith("/") or ":" in path
            or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or any(part in ("", ".", "..") for part in path.split("/"))):
        raise ValueError("Expected an unambiguous repository-relative path")
    return path


def normalize(value: dict[str, Any]) -> str:
    kind = _text(value.get("kind"))
    if kind == "method":
        return _fqn(value.get("class")) + "::" + _identifier(value.get("method"))
    if kind == "function":
        return "\\" + _fqn(value.get("name"))
    if kind == "closure":
        line = value.get("line")
        if type(line) is not int or line < 1:
            raise ValueError("Closure line must be a positive integer")
        return f"closure@{relative_path(value.get('path'))}:{line}"
    if kind == "controller":
        controller = _text(value.get("controller"))
        parts = re.split(r"::|@", controller)
        if len(parts) > 2 or ("\\" not in parts[0]):
            raise ValueError("Controller must identify a concrete fully-qualified class")
        return normalize({"kind": "method", "class": parts[0],
                          "method": parts[1] if len(parts) == 2 else "__invoke"})
    if kind == "route":
        method = _text(value.get("method"))
        template = _text(value.get("template"))
        if not re.fullmatch(r"[A-Za-z]+", method):
            raise ValueError("Route must have one HTTP method")
        stripped = re.sub(r"\{" + _IDENTIFIER + r"\??\}", "param", template)
        if (not template.startswith("/") or "//" in template
                or any(p in (".", "..") for p in template.split("/"))
                or re.search(r"[\s?#{}\\\x00-\x1f\x7f]", stripped, flags=re.ASCII)):
            raise ValueError("Invalid route template")
        return f"route:{method.upper()} {template}"
    if kind == "table":
        return f"table:{_identifier(value.get('connection'))}.{_identifier(value.get('name'))}"
    raise ValueError("Unsupported symbol kind")


def from_chunk(chunk: dict[str, Any]) -> str | None:
    kind = chunk.get("node_type") or chunk.get("type")
    if kind == "method":
        class_name = chunk.get("class_fqn") or chunk.get("parent_symbol") or chunk.get("class")
        if class_name == "(anonymous)":
            return None
        return normalize({"kind": kind, "class": class_name,
                          "method": chunk.get("method_name") or chunk.get("name")})
    if kind == "function":
        name = chunk.get("function_name") or chunk.get("name")
        namespace = chunk.get("namespace")
        if namespace is None:
            namespace = ""
        if not isinstance(namespace, str):
            raise ValueError("Invalid namespace")
        if _text(namespace):
            name = _fqn(namespace) + "\\" + _identifier(name)
        return normalize({"kind": kind, "name": name})
    if kind in ("closure", "arrow_function"):
        return normalize({"kind": "closure", "path": chunk.get("file_path") or chunk.get("file"),
                          "line": chunk.get("start_line")})
    if kind in ("route", "table", "controller"):
        return normalize({**chunk, "kind": kind})
    return None
