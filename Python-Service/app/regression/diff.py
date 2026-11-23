"""Response comparison for replay: status, selected headers and body, minus noise.

Contract: @.archcore/regression/regression-check.spec.md clauses 13, 15-16. A response is
``{"status", "headers", "body" (bytes or None), "body_sha256", "truncated", "error"}``. JSON
bodies compare structurally by path (``$.items[0].total``); HTML compares as normalized text
without CSRF tokens; anything else compares as whitespace-normalized text, or by hash when the
body was too large to keep. Values that look like timestamps or UUIDs compare by shape only.
Noise is the set of paths that differed between two baseline recordings of the same request.
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_DIFFERENCES = 50
COMPARED_HEADERS = ("content-type", "location")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_TOKEN_KEYS = re.compile(r"(^_token$|csrf|xsrf|nonce)", re.I)
_INDEX = re.compile(r"\[\d+\]")


def _shape(value: Any) -> Any:
    if isinstance(value, str):
        if _TIMESTAMP.match(value):
            return "<timestamp>"
        if _UUID.match(value):
            return "<uuid>"
    return value


def flatten(value: Any, path: str = "$") -> dict[str, Any]:
    """Leaf values by JSON path; empty containers are leaves so additions and removals show."""
    if isinstance(value, dict) and value:
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _TOKEN_KEYS.search(str(key)):
                continue
            out.update(flatten(item, f"{path}.{key}"))
        return out
    if isinstance(value, list) and value:
        out = {}
        for index, item in enumerate(value):
            out.update(flatten(item, f"{path}[{index}]"))
        return out
    return {path: _shape(value)}


def general(path: str) -> str:
    """A path with list indices generalized, so noise in one item covers its siblings."""
    return _INDEX.sub("[*]", path)


def _rule(pattern: str) -> re.Pattern:
    """Ignore rule: ``$.a.b``, ``$.items[*].x``, ``$..key`` (any depth); matches the path and its children."""
    pattern = pattern.strip()
    escaped = re.escape(pattern).replace(r"\[\*\]", r"\[\d+\]").replace(r"\.\.", r"(?:\..+)?\.")
    return re.compile(f"^{escaped}(?:[.\\[].*)?$")


def compile_rules(rules: list[str] | None) -> list[re.Pattern]:
    out = []
    for rule in rules or []:
        if isinstance(rule, str) and rule.startswith("$") and len(rule) <= 300:
            out.append(_rule(rule))
    return out


def _media(headers: dict[str, str]) -> str:
    return str(headers.get("content-type", "")).split(";", 1)[0].strip().lower()


def _html_text(body: str) -> str:
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", body)
    body = re.sub(r"(?is)<meta[^>]+(csrf|xsrf)[^>]*>", " ", body)
    body = re.sub(r"(?is)<input[^>]+name=[\"']?_token[^>]*>", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", body).strip()


def body_view(response: dict[str, Any]) -> tuple[str, Any]:
    """``(kind, comparable)`` where kind is json, html, text, hash or none."""
    raw = response.get("body")
    if response.get("truncated") or (raw is None and response.get("body_sha256")):
        return "hash", response.get("body_sha256")
    if not raw:
        return "none", None
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    media = _media(response.get("headers") or {})
    if media.endswith("json") or (not media and text.lstrip()[:1] in ("{", "[")):
        try:
            return "json", flatten(json.loads(text))
        except ValueError:
            pass
    if "html" in media:
        return "html", _html_text(text)
    return "text", re.sub(r"\s+", " ", text).strip()


def _headers(response: dict[str, Any]) -> dict[str, str]:
    headers = response.get("headers") or {}
    out = {name: str(headers.get(name, "")) for name in COMPARED_HEADERS}
    out["content-type"] = _media(headers)
    return out


def _differences(base: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """Every difference between two responses, before noise and rules."""
    diffs: list[dict[str, Any]] = []
    if base.get("error") or candidate.get("error"):
        if base.get("error") != candidate.get("error"):
            diffs.append({"where": "error", "path": "", "base": base.get("error"), "candidate": candidate.get("error")})
        return diffs
    if base.get("status") != candidate.get("status"):
        diffs.append({"where": "status", "path": "", "base": base.get("status"), "candidate": candidate.get("status")})
    left, right = _headers(base), _headers(candidate)
    for name in COMPARED_HEADERS:
        if left[name] != right[name]:
            diffs.append({"where": "header", "path": name, "base": left[name], "candidate": right[name]})
    kind_a, view_a = body_view(base)
    kind_b, view_b = body_view(candidate)
    if kind_a == "json" and kind_b == "json":
        for path in sorted(set(view_a) | set(view_b)):
            if view_a.get(path, "<missing>") != view_b.get(path, "<missing>"):
                diffs.append({"where": "body", "path": path, "base": view_a.get(path, "<missing>"),
                              "candidate": view_b.get(path, "<missing>")})
    elif (kind_a, view_a) != (kind_b, view_b):
        diffs.append({"where": "body", "path": kind_a if kind_a == kind_b else f"{kind_a}->{kind_b}",
                      "base": _excerpt(view_a), "candidate": _excerpt(view_b)})
    return diffs


def _excerpt(value: Any) -> Any:
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "…"
    if isinstance(value, dict):
        return f"<json with {len(value)} values>"
    return value


def _key(diff: dict[str, Any]) -> str:
    return f"{diff['where']}:{general(diff['path']) if diff['where'] == 'body' else diff['path']}"


def noise_between(first: dict[str, Any], second: dict[str, Any]) -> list[str]:
    """Clause 13: what differs between two baseline recordings of one probe."""
    return sorted({_key(diff) for diff in _differences(first, second)})


def compare(base: dict[str, Any], candidate: dict[str, Any], *, noise: list[str] | None = None,
            ignore: list[str] | None = None) -> dict[str, Any]:
    """Clauses 15-16: differences beyond noise and ignore rules, capped, with what was dropped."""
    noisy = set(noise or [])
    rules = compile_rules(ignore)
    kept, dropped = [], {"noise": 0, "ignored": 0}
    for diff in _differences(base, candidate):
        if _key(diff) in noisy:
            dropped["noise"] += 1
        elif diff["where"] == "body" and any(rule.match(diff["path"]) for rule in rules):
            dropped["ignored"] += 1
        else:
            kept.append(diff)
    return {"equal": not kept, "differences": kept[:MAX_DIFFERENCES], "more": max(0, len(kept) - MAX_DIFFERENCES),
            "dropped": dropped}
