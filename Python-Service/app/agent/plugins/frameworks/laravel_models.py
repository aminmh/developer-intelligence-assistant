"""Static Laravel Eloquent model parser.

Extracts per-model: table name, primary key, fillable/guarded, casts, traits,
scopes and relationship methods (belongsTo/hasMany/hasOne/belongsToMany/
morphMany/morphTo/morphToMany + target model). Source text only — no runtime.
"""

from __future__ import annotations

import re
from typing import Any

_CLASS_RE = re.compile(
    r"class\s+(?P<name>\w+)\s+extends\s+[\\\w]+",
)
_NAMESPACE_RE = re.compile(r"namespace\s+([^;]+);")
_TABLE_RE = re.compile(r"protected\s+\$table\s*=\s*['\"]([^'\"]+)['\"]")
_PRIMARY_RE = re.compile(r"protected\s+\$primaryKey\s*=\s*['\"]([^'\"]+)['\"]")
_ARRAY_ATTR_RE = re.compile(
    r"protected\s+\$(?P<attr>fillable|guarded|casts|hidden|appends|with)\s*=\s*\[(.*?)\]",
    re.DOTALL,
)
_TRAITS_RE = re.compile(r"use\s+([A-Za-z_\\, ]+);")
_RELATION_RE = re.compile(
    r"public\s+function\s+(?P<method>\w+)\s*\(\s*\)\s*:\s*[A-Za-z_\\]+\s*\{"
    r"[\s\S]{0,400}?return\s+\$this\s*->\s*(?P<rel>belongsTo|belongsToMany|hasMany|"
    r"hasOne|morphMany|morphTo|morphToMany|morphOne|hasManyThrough|hasOneThrough)\s*\("
    r"\s*(?P<target>[^,\)]+)",
)
_SCOPE_RE = re.compile(r"public\s+function\s+scope(?P<scope>\w+)\s*\(")


def _split_items(body: str) -> list[str]:
    items: list[str] = []
    for m in re.finditer(r"['\"]([^'\"]+)['\"]", body):
        items.append(m.group(1))
    return items


def parse_model(source: str, file_name: str = "") -> dict[str, Any]:
    """Parse one Eloquent model file."""
    model: dict[str, Any] = {
        "name": None,
        "file": file_name,
        "table": None,
        "primary_key": "id",
        "fillable": [],
        "guarded": [],
        "casts": [],
        "traits": [],
        "relations": [],
        "scopes": [],
    }
    cm = _CLASS_RE.search(source)
    if not cm:
        return model
    model["name"] = cm.group("name")

    ns_m = _NAMESPACE_RE.search(source)
    ns = ns_m.group(1).strip() if ns_m else ""

    for am in _ARRAY_ATTR_RE.finditer(source):
        attr = am.group("attr")
        values = _split_items(am.group(1))
        if attr in ("fillable", "guarded", "hidden", "appends", "with"):
            model[attr] = values
        elif attr == "casts":
            # entries like 'is_admin' => 'boolean'
            model["casts"] = re.findall(r"['\"]([^'\"]+)['\"]\s*=>\s*['\"]([^'\"]+)['\"]", am.group(1))

    tm = _TABLE_RE.search(source)
    if tm:
        model["table"] = tm.group(1)
    pm = _PRIMARY_RE.search(source)
    if pm:
        model["primary_key"] = pm.group(1)

    tr = _TRAITS_RE.search(source)
    if tr:
        model["traits"] = [
            t.strip().lstrip("\\").split("::")[0]
            for t in tr.group(1).split(",")
            if t.strip() and t.strip() != "class"
        ]

    for rm in _RELATION_RE.finditer(source):
        target = rm.group("target").strip().strip("'\"")
        target = target.split("::")[0].strip().lstrip("\\")
        short = target.split("\\")[-1]
        model["relations"].append(
            {
                "method": rm.group("method"),
                "type": rm.group("rel"),
                "target": f"{ns}\\{short}" if not target.count("\\") and ns else target,
            }
        )

    model["scopes"] = [s.group("scope") for s in _SCOPE_RE.finditer(source)]
    return model


def parse_models_directory(files: list[tuple[str, str]]) -> list[dict[str, Any]]:
    return [parse_model(src, name) for name, src in files]
