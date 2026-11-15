"""Bounded retention registry for extracted workspaces (cons F17).

Orchestrator job workspaces used to be deleted unconditionally after every
run — which made post-job ``/api/inspect`` file reads (skeleton/attributes/
method) impossible: the ``sym_<stem>`` graph survives, the source is gone.
Analysis jobs pin their workspace here so inspection keeps working; the
registry is a bounded LRU (cap 3)
and eviction removes the oldest directory on disk.

All mutations are best-effort and race-tolerant: a stale registry entry never
breaks a job. Only directories that were recorded by the orchestrator (already
validated to live inside the configured workspace root) are ever removed here.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

KEEPER_CAP = 3


def registry_path() -> Path:
    """JSON store next to settings.db (/app/data/workspace_registry.json)."""
    return Path(
        os.environ.get(
            "WORKSPACE_REGISTRY_PATH",
            str(Path(__file__).resolve().parents[1] / "data" / "workspace_registry.json"),
        )
    )


def _load() -> list[dict[str, Any]]:
    path = registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save(entries: list[dict[str, Any]]) -> None:
    path = registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entries), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("workspace registry write failed: %s", exc)


def _remove_dir(path: str) -> None:
    """Delete a recorded workspace dir; ignore failures (best-effort)."""
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 — eviction must never raise
        logger.debug("workspace eviction skipped for %s: %s", path, exc)


def register(path: str, filename: str) -> dict[str, Any] | None:
    """Pin ``path`` to ``filename`` (newest-first, cap LRU).

    Returns the entry, or None when the path is not a directory. Evicts the
    oldest pinned workspace when the cap is exceeded (filesystem included).
    """
    if not path or not os.path.isdir(path):
        return None
    normalized = os.path.realpath(path)
    entries = _load()
    entries = [e for e in entries if e.get("path") != normalized]
    entry: dict[str, Any] = {
        "id": os.path.basename(normalized.rstrip("/")),
        "filename": filename or "",
        "path": normalized,
        "ts": time.time(),
    }
    entries.insert(0, entry)
    while len(entries) > KEEPER_CAP:
        victim = entries.pop()
        if victim.get("path") != normalized:
            _remove_dir(str(victim.get("path") or ""))
    _save(entries)
    return entry


def forget(workspace_id: str) -> bool:
    """Drop the registry entry (filesystem removal is the caller's job)."""
    entries = _load()
    kept = [e for e in entries if e.get("id") != workspace_id]
    if len(kept) == len(entries):
        return False
    _save(kept)
    return True


def list_workspaces() -> list[dict[str, Any]]:
    """Newest-first entries that still exist on disk."""
    return [e for e in _load() if os.path.isdir(str(e.get("path") or ""))]
