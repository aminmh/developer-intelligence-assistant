"""Tree-sitter helpers shared by the front-end UI map extractor.

Everything here is syntax-level and framework-neutral: parsing a source file
(including the script blocks of a Vue single-file component), reading string
and object literals, and turning a URL expression into a path template.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator

from tree_sitter_language_pack import get_parser

GRAMMARS = {
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
}

_SCRIPT_BLOCK = re.compile(rb"<script\b([^>]*)>(.*?)</script\s*>", re.DOTALL | re.IGNORECASE)
_LANG = re.compile(rb"""\blang\s*=\s*["']?([A-Za-z]+)""")
_TEMPLATE_OPEN = re.compile(rb"<template\b[^>]*>", re.IGNORECASE)


@dataclass(frozen=True)
class Parsed:
    """One parsed block of script. ``line_offset`` maps block lines back to file lines."""

    root: Any
    source: bytes
    line_offset: int = 0

    def line(self, node: Any) -> int:
        return int(node.start_point.row) + 1 + self.line_offset


def split_vue(source: bytes) -> tuple[list[tuple[bytes, str, int]], bytes]:
    """A Vue single-file component -> ([(script, grammar, line offset)], template)."""
    scripts = []
    for match in _SCRIPT_BLOCK.finditer(source):
        lang = _LANG.search(match.group(1))
        name = lang.group(1).decode().lower() if lang else "js"
        grammar = "tsx" if name == "tsx" else "typescript" if name == "ts" else "javascript"
        scripts.append((match.group(2), grammar, source.count(b"\n", 0, match.start(2))))
    opening = _TEMPLATE_OPEN.search(source)
    closing = source.rfind(b"</template")
    template = source[opening.end():closing] if opening and closing > opening.end() else b""
    return scripts, template


def parse_file(suffix: str, source: bytes) -> tuple[list[Parsed], bytes]:
    """Parse a source file into script blocks, plus its markup template when it has one."""
    if suffix == ".vue":
        scripts, template = split_vue(source)
        return [Parsed(get_parser(grammar).parse(script).root_node, script, offset)
                for script, grammar, offset in scripts], template
    grammar = GRAMMARS.get(suffix)
    if grammar is None:
        return [], b""
    return [Parsed(get_parser(grammar).parse(source).root_node, source)], b""


def text(node: Any, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def field(node: Any, name: str) -> Any:
    try:
        return node.child_by_field_name(name)
    except (AttributeError, TypeError):
        return None


def walk(node: Any) -> Iterator[Any]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(getattr(current, "named_children", ())))


def string_value(node: Any, source: bytes) -> str | None:
    """The value of a plain string literal, or of a template string without substitutions."""
    if node is None:
        return None
    if node.type == "string":
        return text(node, source)[1:-1]
    if node.type == "template_string" and not any(c.type == "template_substitution" for c in node.named_children):
        return text(node, source)[1:-1]
    return None


def unwrap(node: Any) -> Any:
    """Strip parentheses and TypeScript casts around an expression."""
    while node is not None and node.type in ("parenthesized_expression", "as_expression", "satisfies_expression",
                                             "non_null_expression", "type_assertion"):
        inner = [child for child in node.named_children if not child.type.startswith("type") and child.type != "comment"]
        if not inner:
            break
        node = inner[0] if node.type != "type_assertion" else inner[-1]
    return node


def object_pairs(node: Any, source: bytes) -> dict[str, Any]:
    """``{key: value}`` of an object literal: property name -> value node."""
    pairs: dict[str, Any] = {}
    node = unwrap(node)
    if node is None or node.type != "object":
        return pairs
    for child in node.named_children:
        if child.type == "pair":
            key, value = field(child, "key"), field(child, "value")
            if key is not None and value is not None:
                name = string_value(key, source) if key.type == "string" else text(key, source)
                if name is not None:
                    pairs[name] = value
        elif child.type == "shorthand_property_identifier":
            pairs[text(child, source)] = child
    return pairs


def call_name(node: Any, source: bytes) -> tuple[str, str]:
    """For a call expression: (object text, function or property name). Object is '' for a plain call."""
    function = field(node, "function")
    if function is None:
        return "", ""
    if function.type == "identifier":
        return "", text(function, source)
    if function.type == "member_expression":
        target, prop = field(function, "object"), field(function, "property")
        if target is not None and prop is not None:
            return text(target, source)[:80], text(prop, source)
    return "", ""


def call_arguments(node: Any) -> list[Any]:
    arguments = field(node, "arguments")
    return [child for child in getattr(arguments, "named_children", ()) if child.type != "comment"]


def _placeholder(node: Any, source: bytes) -> str:
    names = re.findall(r"[A-Za-z_$][\w$]*", text(node, source))
    if len(names) > 1 and names[-1] == "value":  # a Vue ref: `${orderId.value}` is about orderId
        names.pop()
    return "{" + (names[-1] if names else "value") + "}"


def url_template(node: Any, source: bytes, constants: dict[str, str]) -> str | None:
    """Turn a URL expression into a path template, e.g. `` `${base}/users/${id}` `` -> ``{base}/users/{id}``.

    Dynamic parts become ``{name}`` placeholders; file-level string constants are
    substituted. Returns None when no literal path text is left.
    """
    return url_parts(_build_address(node, source, constants))[0]


def _build_address(node: Any, source: bytes, constants: dict[str, str]) -> str:
    def build(part: Any, depth: int = 0) -> str:
        part = unwrap(part)
        if part is None or depth > 12:
            return "{value}"
        literal = string_value(part, source)
        if literal is not None:
            return literal
        if part.type == "template_string":
            pieces, position = [], part.start_byte + 1
            for child in part.named_children:
                if child.type != "template_substitution":
                    continue
                pieces.append(source[position:child.start_byte].decode("utf-8", errors="replace"))
                inner = [c for c in child.named_children if c.type != "comment"]
                pieces.append(build(inner[0], depth + 1) if inner else "{value}")
                position = child.end_byte
            pieces.append(source[position:part.end_byte - 1].decode("utf-8", errors="replace"))
            return "".join(pieces)
        if part.type == "binary_expression" and text(part, source).count("+"):
            left, right = field(part, "left"), field(part, "right")
            if left is not None and right is not None:
                return build(left, depth + 1) + build(right, depth + 1)
        if part.type == "identifier":
            name = text(part, source)
            return constants.get(name, "{" + name + "}")
        return _placeholder(part, source)

    return build(node)


_QUERY_NAME = re.compile(r"(?:^|&)([A-Za-z0-9_.\[\]-]+)=")


def url_parts(template: str) -> tuple[str | None, list[str], bool]:
    """Split a built address into (path template, query parameter names, whether the query was fully visible)."""
    path, _, query = template.partition("?")
    path = path.strip()
    names = list(dict.fromkeys(_QUERY_NAME.findall(query)))
    # A placeholder standing for a whole `name=value` pair (`?${qs}`) hides parameter names.
    complete = all("=" in part or not part for part in query.split("&"))
    if "/" not in re.sub(r"\{[^{}]*\}", "", path):
        return None, names, complete
    return path[:300], names, complete


def build_url(node: Any, source: bytes, constants: dict[str, str]) -> tuple[str | None, list[str], bool]:
    """Like :func:`url_template`, also returning the query parameter names written in the address."""
    return url_parts(_build_address(node, source, constants))


def object_keys(node: Any, source: bytes) -> tuple[list[str] | None, bool]:
    """Keys of an object literal and whether all of them are visible (no spread). (None, False) for anything else."""
    node = unwrap(node)
    if node is None or node.type != "object":
        return None, False
    complete = not any(child.type == "spread_element" for child in node.named_children)
    return list(object_pairs(node, source)), complete


def enclosing_symbol(node: Any, source: bytes) -> str:
    """``Class.method`` / function / variable that contains ``node``; '' at module level."""
    names: list[str] = []
    current = node.parent
    while current is not None:
        name_node = None
        if current.type in ("function_declaration", "method_definition", "class_declaration",
                            "generator_function_declaration", "abstract_class_declaration"):
            name_node = field(current, "name")
        elif current.type in ("variable_declarator", "public_field_definition", "field_definition"):
            name_node = field(current, "name")
        elif current.type == "pair":
            name_node = field(current, "key")
        if name_node is not None:
            name = text(name_node, source)[:80]
            if name and (not names or names[-1] != name):
                names.append(name)
        current = current.parent
    return ".".join(reversed(names[:3]))
