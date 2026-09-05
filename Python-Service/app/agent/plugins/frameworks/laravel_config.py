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


def _env_value(expr: str) -> str | Any:
    """Map an expression to a display value (never a real secret)."""
    m = _ENV_REF_RE.search(expr)
    if m:
        return f"env:{m.group(1)}"
    s = expr.strip()
    if s.startswith("["):
        return "array"
    if s.startswith("'") or s.startswith('"'):
        return s.strip("'\"").strip()
    if s == "true":
        return True
    if s == "false":
        return False
    if s == "null":
        return None
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
        if not in_s and not in_d:
            if ch == "[":
                level += 1
                last_key[level] = None
                i += 1
                continue
            if ch == "]":
                level -= 1
                i += 1
                continue
            if level == 0:
                i += 1
                continue
            if level > depth:
                i += 1
                continue
            km = re.match(r"['\"]?([A-Za-z0-9_.-]+)['\"]?\s*=>\s*", source[i:])
            if km and last_key[level] is None:
                last_key[level] = km.group(1)
                rest = source[i + km.end() :]
                # value ends at , ] or newline at same bracket level
                val_end = len(rest)
                for tok in (",", "\n"):
                    j = rest.find(tok)
                    if j != -1:
                        val_end = min(val_end, j)
                value_expr = rest[:val_end].strip()
                node: Any = out
                for lv in range(1, level):
                    node = node.setdefault(last_key[lv], {})
                key = last_key[level]
                if value_expr.startswith("["):
                    node[key] = {}
                else:
                    node[key] = _env_value(value_expr)
                last_key[level] = None
                i += km.end() + val_end
                continue
        i += 1
    return out


def parse_config_directory(files: list[tuple[str, str]]) -> dict[str, Any]:
    """Parse config files into ``{file_stem: tree}``."""
    out: dict[str, Any] = {}
    for name, src in files:
        stem = name.replace(".php", "")
        out[stem] = parse_config_file(src)
    return out
