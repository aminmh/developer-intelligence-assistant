"""Persistent analysis-session registry (SQLite) + artifact file store.

Each row is one **saved analysis**: the durable record of running an uploaded
    project through the analysis pipeline, with pointers to the full LLM
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
    kind            TEXT NOT NULL DEFAULT 'analysis',
    created_at      TEXT,
    finished_at     TEXT,
    summary         TEXT,
    profile_json    TEXT,
    grounding_md    TEXT,
    runtime_json    TEXT,
    counts_json     TEXT
)
"""

#: Canonical kinds for saved sessions (default analysis keeps legacy rows).
_KIND_COLUMN = "kind"


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
                self._migrate_kind(conn)
            finally:
                conn.close()

    def _migrate_kind(self, conn: sqlite3.Connection) -> None:
        """Add the ``kind`` column to a pre-kind legacy database (idempotent).

        Existing rows keep the ``'analysis'`` default, so re-saving an old
        analysis after an upgrade never changes its meaning. Never raises — a
        failed migration must not break ``AnalysisStore()`` construction.
        """
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(analysis_sessions)")}
            if _KIND_COLUMN in columns:
                return
            conn.execute(
                "ALTER TABLE analysis_sessions "
                f"ADD COLUMN {_KIND_COLUMN} TEXT NOT NULL DEFAULT 'analysis'"
            )
            conn.commit()
        except sqlite3.Error as exc:  # pragma: no cover — migration is best-effort
            from infra.exception_logger import get_exception_logger

            get_exception_logger().warning(
                "analysis_sessions kind migration skipped: %s", exc
            )

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
        kind: str = "analysis",
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
            "kind": kind,
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
                        kind, created_at, summary, profile_json, grounding_md,
                        runtime_json, counts_json
                    ) VALUES (
                        :id, :job_id, :filename, :collection_name, :label, :status,
                        :kind, :created_at, :summary, :profile_json, :grounding_md,
                        :runtime_json, :counts_json
                    )
                    ON CONFLICT(id) DO UPDATE SET
                        job_id=excluded.job_id,
                        filename=excluded.filename,
                        collection_name=excluded.collection_name,
                        label=excluded.label,
                        status=excluded.status,
                        kind=excluded.kind,
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

    def list_by_filename(self, filename: str, limit: int = 100) -> list[dict[str, Any]]:
        """Saved-session rows for one exact filename (case-sensitive), newest
        first. Mirrors ``list`` ordering (created_at DESC, rowid tie-break)."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM analysis_sessions "
                    "WHERE filename = ? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (filename, max(1, min(limit, 500))),
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

    # -- evaluator artifacts ----------------------------------------------

    def save_evaluator_reports(
        self,
        session_id: str,
        reports: list[dict[str, Any]],
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Persist evaluator output under the existing analysis authority.

        No evaluator-specific table is introduced: reports are stored in the
        session's profile JSON and as a bounded artifact for durable reads.
        """
        row = self.get(session_id)
        if row is None:
            return None
        profile = dict(row.get("profile") or {})
        profile["evaluator_reports"] = list(reports)[:500]
        if context is not None:
            profile["evaluator_context"] = context
        self.save(
            session_id=session_id,
            job_id=str(row.get("job_id") or session_id),
            filename=str(row.get("filename") or "unknown.zip"),
            collection_name=row.get("collection_name"),
            label=row.get("label"),
            summary=row.get("summary"),
            profile=profile,
            grounding_md=row.get("grounding_md"),
            runtime=row.get("runtime"),
            counts=row.get("counts"),
            kind="evaluator",
        )
        try:
            artifact = self.artifacts_dir(session_id) / "evaluator_reports.json"
            artifact.write_text(json.dumps({"reports": profile["evaluator_reports"], "context": context or {}}, indent=2), encoding="utf-8")
        except OSError:
            pass
        return self.get(session_id)

    def evaluator_reports(self, session_id: str) -> list[dict[str, Any]]:
        row = self.get(session_id)
        if not row:
            return []
        profile = row.get("profile") or {}
        reports = profile.get("evaluator_reports") if isinstance(profile, dict) else None
        return list(reports or []) if isinstance(reports, list) else []

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
