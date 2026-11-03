"""Transactional schema drafts; baselines and analysis profiles stay unchanged."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from analysis.schema_model import Schema

MAX_SCHEMA_BYTES = 4 * 1024 * 1024
MAX_LAYOUT_BYTES = 4 * 1024 * 1024


class DraftConflict(ValueError):
    """A draft changed since the caller read it."""


class DraftNotFound(LookupError):
    """The requested draft does not exist."""


def _schema_json(value: Schema | dict[str, Any]) -> str:
    schema = Schema.model_validate(value)
    encoded = schema.model_dump_json()
    if len(encoded.encode("utf-8")) > MAX_SCHEMA_BYTES:
        raise ValueError("Schema exceeds the draft size limit")
    return encoded


def _check_layout(layout: str) -> None:
    if not isinstance(layout, str) or len(layout.encode("utf-8")) > MAX_LAYOUT_BYTES:
        raise ValueError("Diagram layout must be text within the draft size limit")


def _read_row(row: sqlite3.Row) -> dict[str, Any]:
    if any(len(row[key].encode("utf-8")) > MAX_SCHEMA_BYTES for key in ("baseline_json", "schema_json")):
        raise ValueError("Stored schema exceeds the draft size limit")
    baseline = Schema.model_validate_json(row["baseline_json"]).model_dump(mode="json")
    schema = Schema.model_validate_json(row["schema_json"]).model_dump(mode="json")
    _check_layout(row["layout"])
    return {"id": row["id"], "analysis_id": row["analysis_id"],
            "source_kind": row["source_kind"], "baseline": baseline, "schema": schema,
            "layout": row["layout"], "revision": row["revision"]}


class SchemaDraftStore:
    """SQLite owner for saved-analysis and imported-schema drafts."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_drafts (
                    id TEXT PRIMARY KEY,
                    analysis_id TEXT UNIQUE REFERENCES analysis_sessions(id) ON DELETE CASCADE,
                    source_kind TEXT NOT NULL CHECK(source_kind IN ('analysis', 'import')),
                    baseline_json TEXT NOT NULL,
                    schema_json TEXT NOT NULL,
                    layout TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    CHECK((source_kind = 'analysis' AND analysis_id IS NOT NULL)
                       OR (source_kind = 'import' AND analysis_id IS NULL))
                )
            """)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, baseline: Schema | dict[str, Any], *, analysis_id: str | None = None,
               layout: str = "") -> dict[str, Any]:
        """Create a draft from a baseline already derived from the selected source."""
        encoded = _schema_json(baseline)
        _check_layout(layout)
        draft_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO schema_drafts VALUES (?, ?, ?, ?, ?, ?, 1)",
                (draft_id, analysis_id, "analysis" if analysis_id is not None else "import",
                 encoded, encoded, layout),
            )
        return self.get(draft_id)

    def get(self, draft_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM schema_drafts WHERE id = ?", (draft_id,)).fetchone()
        if row is None:
            raise DraftNotFound(draft_id)
        return _read_row(row)

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, analysis_id, source_kind, revision FROM schema_drafts ORDER BY rowid DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def save(self, draft_id: str, schema: Schema | dict[str, Any], layout: str,
             expected_revision: int) -> dict[str, Any]:
        encoded = _schema_json(schema)
        _check_layout(layout)
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE schema_drafts SET schema_json = ?, layout = ?, revision = revision + 1 "
                "WHERE id = ? AND revision = ?",
                (encoded, layout, draft_id, expected_revision),
            )
            if cursor.rowcount == 0:
                exists = conn.execute("SELECT 1 FROM schema_drafts WHERE id = ?", (draft_id,)).fetchone()
                if exists:
                    raise DraftConflict("Draft changed; reload before saving")
                raise DraftNotFound(draft_id)
            row = conn.execute("SELECT * FROM schema_drafts WHERE id = ?", (draft_id,)).fetchone()
            return _read_row(row)
