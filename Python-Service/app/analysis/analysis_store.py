"""Persistent analysis-session registry (SQLite) + artifact file store.

Each row is one **saved analysis**: the durable record of running an uploaded
project through the sandbox/analysis pipeline, with pointers to the full LLM
context artifacts (profile JSON, grounding markdown, runtime probe results).

Backing DB shares the settings DB file (SETTINGS_DB_PATH, default
``/app/data/settings.db``) but is a separate table + connection + lock so it
can evolve independently of the KV ``settings`` store.

Artifacts live under ANALYSIS_DIR (default ``<db dir>/analyses``):
    <ANALYSIS_DIR>/<id>/{profile.json, grounding.md, runtime.json,
                       session.json}
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DB_PATH = os.getenv("SETTINGS_DB_PATH", "/app/data/settings.db")
ANALYSIS_DIR = os.getenv(
    "ANALYSIS_DIR", str(Path(DB_PATH).resolve().parent / "analyses")
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_sessions (
    id              TEXT PRIMARY KEY,
    job_id          TEXT UNIQUE,
    filename        TEXT NOT NULL,
    collection_name TEXT,
    label           TEXT,
    status          TEXT NOT NULL DEFAULT 'saved',
    created_at      TEXT,
    finished_at     TEXT,
    summary         TEXT,
    profile_json    TEXT,
    grounding_md    TEXT,
    runtime_json    TEXT,
    counts_json     TEXT
)
"""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class AnalysisStore:
    """Thread-safe SQLite registry for saved analyses."""

    def __init__(self, db_path: str = DB_PATH, analysis_dir: str = ANALYSIS_DIR) -> None:
        self._db_path = db_path
        self._analysis_dir = Path(analysis_dir)
        self._lock = threading.RLock()
        self._init_db()

    # -- helpers ------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    def artifacts_dir(self, session_id: str) -> Path:
        """Per-session artifact directory (created lazily by writers)."""
        return self._analysis_dir / session_id

    # -- rows ---------------------------------------------------------------

    def save(
        self,
        session_id: str,
        job_id: str,
        filename: str,
        *,
        collection_name: str | None = None,
        label: str | None = None,
        summary: str | None = None,
        profile: dict[str, Any] | None = None,
        grounding_md: str | None = None,
        runtime: dict[str, Any] | None = None,
        counts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Upsert a session row AND write its artifact files. Idempotent per
        session_id/job_id (re-saving overwrites). Returns the session dict."""
        row = {
            "id": session_id,
            "job_id": job_id,
            "filename": filename,
            "collection_name": collection_name,
            "label": label,
            "status": "saved",
            "created_at": _now_iso(),
            "summary": summary,
            "profile_json": json.dumps(profile) if profile is not None else None,
            "grounding_md": grounding_md,
            "runtime_json": json.dumps(runtime) if runtime is not None else None,
            "counts_json": json.dumps(counts) if counts is not None else None,
        }
        with self._lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    "SELECT created_at FROM analysis_sessions WHERE id=?",
                    (session_id,),
                ).fetchone()
                if existing:
                    row["created_at"] = existing["created_at"]
                conn.execute(
                    """
                    INSERT INTO analysis_sessions (
                        id, job_id, filename, collection_name, label, status,
                        created_at, summary, profile_json, grounding_md,
                        runtime_json, counts_json
                    ) VALUES (
                        :id, :job_id, :filename, :collection_name, :label, :status,
                        :created_at, :summary, :profile_json, :grounding_md,
                        :runtime_json, :counts_json
                    )
                    ON CONFLICT(id) DO UPDATE SET
                        job_id=excluded.job_id,
                        filename=excluded.filename,
                        collection_name=excluded.collection_name,
                        label=excluded.label,
                        status=excluded.status,
                        summary=excluded.summary,
                        profile_json=excluded.profile_json,
                        grounding_md=excluded.grounding_md,
                        runtime_json=excluded.runtime_json,
                        counts_json=excluded.counts_json
                    """,
                    row,
                )
                conn.commit()
            finally:
                conn.close()
        self._write_artifacts(session_id, profile, grounding_md, runtime)
        return self._row_to_dict(row)

    def _write_artifacts(
        self,
        session_id: str,
        profile: dict[str, Any] | None,
        grounding_md: str | None,
        runtime: dict[str, Any] | None,
    ) -> None:
        """Best-effort artifact files; failures never corrupt the registry."""
        try:
            d = self.artifacts_dir(session_id)
            d.mkdir(parents=True, exist_ok=True)
            if profile is not None:
                (d / "profile.json").write_text(
                    json.dumps(profile, indent=2), encoding="utf-8"
                )
            if grounding_md is not None:
                (d / "grounding.md").write_text(grounding_md, encoding="utf-8")
            if runtime is not None:
                (d / "runtime.json").write_text(
                    json.dumps(runtime, indent=2), encoding="utf-8"
                )
        except OSError:
            pass

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM analysis_sessions WHERE id=?", (session_id,)
                ).fetchone()
                return self._row_to_dict(dict(row)) if row else None
            finally:
                conn.close()

    def get_by_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM analysis_sessions WHERE job_id=?", (job_id,)
                ).fetchone()
                return self._row_to_dict(dict(row)) if row else None
            finally:
                conn.close()

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM analysis_sessions "
                    "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (max(1, min(limit, 500)),),
                ).fetchall()
                return [self._row_to_dict(dict(r)) for r in rows]
            finally:
                conn.close()

    def delete(self, session_id: str) -> bool:
        """Remove the row and the per-session artifact dir."""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "DELETE FROM analysis_sessions WHERE id=?", (session_id,)
                )
                conn.commit()
                deleted = cur.rowcount > 0
            finally:
                conn.close()
        if deleted:
            try:
                d = self.artifacts_dir(session_id)
                if d.is_dir():
                    for child in d.iterdir():
                        if child.is_file():
                            child.unlink()
                    d.rmdir()
            except OSError:
                pass
        return deleted

    # -- serialization ------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
        """Expand JSON columns; drop the raw json payloads that the web layer
        re-reads from artifact files."""
        out = dict(row)
        for key, loader in (
            ("profile_json", json.loads),
            ("runtime_json", json.loads),
            ("counts_json", json.loads),
        ):
            raw = out.pop(key, None)
            if raw:
                try:
                    out[key.replace("_json", "")] = loader(raw)
                except (json.JSONDecodeError, TypeError):
                    out[key.replace("_json", "")] = None
        return out


analysis_store = AnalysisStore()
