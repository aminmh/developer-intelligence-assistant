"""Excerpt cache (spec clause 11): keyed by provider, library, version or ref, topic and target.

Excerpts from a fixed version (the installed copy, files at a tag or commit) never change, so they are
kept without expiry. GitMCP excerpts follow a moving default branch and expire after a day; a framework
guide follows its version line's branch, which changes slowly, and expires after a week.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

LIFETIMES_S: dict[str, float | None] = {"vendor": None, "github_ref": None, "gitmcp": 24 * 3600,
                                        "doc_source": 7 * 24 * 3600}


def _default_path() -> Path:
    from infra.settings import settings

    root = Path(os.environ.get("TELEMETRY_SNAPSHOT_ROOT") or Path(settings.workspace_root).parent / "snapshots")
    root.mkdir(parents=True, exist_ok=True)
    return root / "knowledge_cache.sqlite"


def cache_key(provider: str, library: str, version: str, topic: str, target: str | None = None) -> str:
    raw = json.dumps([provider, library, version, " ".join(topic.lower().split()), target or ""])
    return sha256(raw.encode("utf-8")).hexdigest()


class KnowledgeCache:
    def __init__(self, path: Path | str | None = None, clock=time.time):
        self.path = Path(path) if path else _default_path()
        self.clock = clock
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS knowledge_cache (key TEXT PRIMARY KEY, provider TEXT NOT NULL,
                library TEXT NOT NULL, version TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL,
                payload_json TEXT NOT NULL)""")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def get(self, key: str) -> Any:
        with self._connect() as db:
            row = db.execute("SELECT payload_json, expires_at FROM knowledge_cache WHERE key=?", (key,)).fetchone()
        if row is None or (row[1] is not None and row[1] <= self.clock()):
            return None
        return json.loads(row[0])

    def put(self, key: str, provider: str, library: str, version: str, payload: Any) -> None:
        lifetime = LIFETIMES_S.get(provider, 24 * 3600)
        now = self.clock()
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO knowledge_cache VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (key, provider, library, version, now, None if lifetime is None else now + lifetime,
                        json.dumps(payload)))

    def purge_expired(self) -> int:
        with self._connect() as db:
            return db.execute("DELETE FROM knowledge_cache WHERE expires_at IS NOT NULL AND expires_at <= ?",
                              (self.clock(),)).rowcount
