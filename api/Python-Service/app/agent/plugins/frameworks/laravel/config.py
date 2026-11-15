"""Static Laravel config extractor.

Walks ``config/*.php`` and returns a shallow key tree of each file with value
*types* and env() references. Actual secret values are never extracted:
``env('MAIL_PASSWORD')`` appears as ``env:MAIL_PASSWORD`` so the model learns
configuration shape without exposing credentials.

Output:
    {"mail": {"default": "env:MAIL_MAILER",
              "from": {"address": "env:MAIL_FROM_ADDRESS", "name": null}}}
"""

from __future__ import annotations

import re
from typing import Any

_ENV_REF_RE = re.compile(
    r"env\s*\(\s*['\"]([^'\"]+)['\"]\s*(?:,\s*['\"]([^'\"]*)['\"])?\s*\)"
)
_KEYPAIR_RE = re.compile(
    r"(?P<key>['\"]?[A-Za-z0-9_.-]+['\"]?)\s*=>\s*(?P<value>[^,\n]+)"
)
_RETURN_RE = re.compile(r"return\s*\[")
_CLASS_CONST_RE = re.compile(r"(?P<class>[A-Za-z_][A-Za-z0-9_\\]*)::class\s*$")
# Keys may be plain identifiers, dotted, dashed — or PHP ``::class`` constants.
_KEY_RE = re.compile(r"['\"]?([A-Za-z0-9_.-]+(?:::class)?)['\"]?\s*=>\s*")


def _value_end(rest: str) -> int:
    """End of a scalar value expression: first top-level `,` `]` or newline.

    Commas *inside* calls (``env('A', '')``) or brackets must not terminate
    the value — the old newline/comma split mangled single-line env() calls
    into ``expr``.
    """
    depth_p = depth_b = 0
    in_s = in_d = False
    i = 0
    n = len(rest)
    while i < n:
        ch = rest[i]
        if in_d and ch == "\\":
            i += 2
            continue
        if in_s and ch == "\\":
            i += 2
            continue
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif not in_s and not in_d:
            if ch == "(":
                depth_p += 1
            elif ch == ")":
                depth_p = max(0, depth_p - 1)
            elif ch == "[":
                depth_b += 1
            elif ch == "]":
                if depth_b == 0:
                    return i  # end of an inline array
                depth_b -= 1
            elif depth_p == 0 and depth_b == 0 and ch in ",\n":
                return i
        i += 1
    return n


def _compact_arrays(node: Any) -> Any:
    """Replace leaf ``{}`` values (PHP array/list content below parse depth or
    never keyed) with the deterministic marker ``array`` — an empty object is
    never truthful for PHP config, every bracket block is an array."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if isinstance(v, dict):
                if not v:
                    out[k] = "array"
                else:
                    out[k] = _compact_arrays(v)
            else:
                out[k] = v
        return out
    return node


def _env_value(expr: str) -> str | Any:
    """Map an expression to a display value (never a real secret)."""
    m = _ENV_REF_RE.search(expr)
    if m:
        return f"env:{m.group(1)}"
    s = expr.strip()
    if s.startswith("["):
        return "array"
    if s.startswith(("'", '"')):
        return s.strip("'\"").strip()
    if s == "true":
        return True
    if s == "false":
        return False
    if s == "null":
        return None
    cm = _CLASS_CONST_RE.match(s)
    if cm:
        cls = cm.group("class")
        return cls if "\\" not in cls else "class:" + cls.rsplit("\\", 1)[-1]
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return "expr"


def parse_config_file(source: str, depth: int = 2) -> dict[str, Any]:
    """Parse a Laravel config file into a nested key tree (max ``depth``)."""
    out: dict[str, Any] = {}
    if not _RETURN_RE.search(source):
        return out

    # strip comments
    source = re.sub(r"//[^\n]*", "", source)
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)

    level = 0
    in_s = in_d = False
    last_key: list[str | None] = [None] * 8
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if not in_s and not in_d:
            # A key may start at a quote OR a bare identifier. Try the key
            # pattern first (it consumes its own quotes) so quoted keys are
            # parsed instead of being swallowed as string literals.
            if 0 < level <= depth and last_key[level] is None:
                km = _KEY_RE.match(source[i:])
                if km:
                    last_key[level] = km.group(1)
                    rest = source[i + km.end() :]
                    val_end = _value_end(rest)
                    value_expr = rest[:val_end].strip()
                    node: Any = out
                    for lv in range(1, level):
                        node = node.setdefault(last_key[lv], {})
                    key = last_key[level]
                    if value_expr.startswith("["):
                        node[key] = {}
                        # Keep last_key[level] set — this key's subtree is
                        # OPEN; the matching ']' below releases it. Jump to
                        # the opening '[' so the main loop bumps level.
                        skip = km.end() + rest.index("[")
                        i += skip
                        continue
                    node[key] = _env_value(value_expr)
                    last_key[level] = None
                    i += km.end() + val_end
                    continue
            if ch == "[":
                level += 1
                last_key[level] = None
                i += 1
                continue
            if ch == "]":
                # A bracket closes the subtree opened by the key one level
                # up: release that level's slot so later siblings match.
                if level > 0:
                    level -= 1
                    last_key[level] = None
                i += 1
                continue
            if level == 0 or level > depth:
                i += 1
                continue
        if in_d and ch == "\\":
            i += 2
            continue
        if in_s and ch == "\\":
            i += 2
            continue
        if ch == "'" and not in_d:
            in_s = not in_s
            i += 1
            continue
        if ch == '"' and not in_s:
            in_d = not in_d
            i += 1
            continue
        i += 1
    return _compact_arrays(out)


def parse_config_directory(files: list[tuple[str, str]]) -> dict[str, Any]:
    """Parse config files into ``{file_stem: tree}``."""
    out: dict[str, Any] = {}
    for name, src in files:
        stem = name.replace(".php", "")
        out[stem] = parse_config_file(src)
    return out
