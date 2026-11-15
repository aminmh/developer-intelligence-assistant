"""Framework-knowledge cache keyed by project filename.

Analysis runs once per upload (during the orchestration job) and is reused
across every later question about that project — no re-extraction per prompt.
Thread-safe LRU, bounded size.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from typing import Any

_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_LOCK = threading.Lock()
_MAX_ENTRIES = 64


def _normalize(filename: str) -> str:
    """'Archive.zip' / 'chunk_code_Archive' / 'archive' → 'archive'."""
    stem = filename
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    stem = re.sub(r"^chunk_code_", "", stem)
    stem = re.sub(r"[^A-Za-z0-9_]", "", stem).lower()
    return stem


def put(filename: str, profile: dict[str, Any]) -> None:
    key = _normalize(filename)
    if not key or not profile:
        return
    with _LOCK:
        _CACHE[key] = profile
        _CACHE.move_to_end(key)
        while len(_CACHE) > _MAX_ENTRIES:
            _CACHE.popitem(last=False)


def get(filename: str) -> dict[str, Any] | None:
    key = _normalize(filename)
    if not key:
        return None
    with _LOCK:
        profile = _CACHE.get(key)
        if profile is not None:
            _CACHE.move_to_end(key)
        return profile


def get_for_collection(collection: str | None) -> dict[str, Any] | None:
    """Look up by Qdrant collection name (e.g. 'chunk_code_Archive')."""
    if not collection:
        return None
    return get(collection)


def clear() -> None:
    with _LOCK:
        _CACHE.clear()


def size() -> int:
    with _LOCK:
        return len(_CACHE)
