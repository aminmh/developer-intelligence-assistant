"""Deterministic signals for queries that target named code symbols."""

from __future__ import annotations

import re

_CODE_SYMBOL_RE = re.compile(
    r"\b(?:[A-Z][a-z0-9]+){2,}\b"
    r"|\b[A-Za-z_][A-Za-z0-9_]*(?:Service|Controller|Repository|Handler|"
    r"Middleware|Model|Policy|Request|Resource|Listener|Provider|Command|"
    r"Event|Job|Factory|Trait|DTO)\b"
)
_FILE_RE = re.compile(r"\b[\w./-]+\.php\b", re.IGNORECASE)
_SEARCH_WORDS = re.compile(
    r"\b(?:where|find|locate|search|reference|references|caller|callers|"
    r"called|used|usages|imports|imported|defined|definition)\b",
    re.IGNORECASE,
)
_REFACTOR_WORDS = re.compile(
    r"\b(?:refactor|rewrite|clean|cleanup|improve|optimi[sz]e|rename|split|"
    r"extract|remove|change|update)\b",
    re.IGNORECASE,
)
_DEBUG_WORDS = re.compile(
    r"\b(?:error|exception|bug|crash|failing|fails|failure|stacktrace|"
    r"stack trace|500)\b",
    re.IGNORECASE,
)
_EXPLAIN_WORDS = re.compile(
    r"\b(?:how|what|why|explain|describe|understand|summari[sz]e|works?|"
    r"purpose|behavior|behaviour)\b",
    re.IGNORECASE,
)


def infer_code_query(prompt: str) -> dict[str, str | None]:
    """Infer a named class/file and the strongest obvious action boundary."""
    symbols = [match.group(0) for match in _CODE_SYMBOL_RE.finditer(prompt or "")]
    target_file_match = _FILE_RE.search(prompt or "")
    if _DEBUG_WORDS.search(prompt or ""):
        intent = "debug"
    elif _REFACTOR_WORDS.search(prompt or ""):
        intent = "refactor"
    elif _SEARCH_WORDS.search(prompt or ""):
        intent = "search"
    elif symbols:
        intent = "explain"
    else:
        intent = "chat"
    return {
        "class_name": symbols[0] if symbols else None,
        "target_file": target_file_match.group(0) if target_file_match else None,
        "intent": intent,
    }


def specific_symbol_route(prompt: str) -> str | None:
    """Return a deterministic route when a named symbol makes intent obvious."""
    inferred = infer_code_query(prompt)
    if not inferred["class_name"] or inferred["intent"] == "chat":
        return None
    return {
        "explain": "code_explain",
        "refactor": "code_refactor",
        "debug": "debug",
        "search": "search",
    }.get(str(inferred["intent"]))
