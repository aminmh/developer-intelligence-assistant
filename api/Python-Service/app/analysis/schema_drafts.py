"""Transactional schema drafts; baselines and analysis profiles stay unchanged."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
import json

from analysis.schema_model import Schema
from analysis.schema_runtime import RuntimeProvenance

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
    result = {"id": row["id"], "analysis_id": row["analysis_id"],
            "source_kind": row["source_kind"], "baseline": baseline, "schema": schema,
            "layout": row["layout"], "revision": row["revision"]}
    if row['provenance_json'] is not None:
        result['source_kind'] = 'runtime'
        result['provenance'] = RuntimeProvenance.model_validate_json(row['provenance_json']).model_dump(mode='json')
    return result


_DRAFT_WITH_PROVENANCE = ("SELECT d.*, p.provenance_json FROM schema_drafts d "
                          "LEFT JOIN schema_draft_runtime_provenance p ON p.draft_id=d.id")


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
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_draft_runtime_provenance (
                draft_id TEXT PRIMARY KEY REFERENCES schema_drafts(id) ON DELETE CASCADE,
                provenance_json TEXT NOT NULL
            )""")
            conn.execute("""CREATE TRIGGER IF NOT EXISTS schema_runtime_provenance_immutable
                BEFORE UPDATE ON schema_draft_runtime_provenance
                BEGIN SELECT RAISE(ABORT, 'Runtime provenance is immutable'); END""")

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
               layout: str = "", provenance: RuntimeProvenance | dict | None = None) -> dict[str, Any]:
        """Create a draft from a baseline already derived from the selected source."""
        encoded = _schema_json(baseline)
        _check_layout(layout)
        encoded_provenance = None
        if provenance is not None:
            if analysis_id is not None:
                raise ValueError('Runtime provenance cannot replace a saved-analysis baseline')
            encoded_provenance = RuntimeProvenance.model_validate(provenance).model_dump_json()
            if len(encoded_provenance.encode('utf-8')) > MAX_SCHEMA_BYTES:
                raise ValueError('Runtime provenance exceeds the draft size limit')
        draft_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO schema_drafts VALUES (?, ?, ?, ?, ?, ?, 1)",
                (draft_id, analysis_id, "analysis" if analysis_id is not None else "import",
                 encoded, encoded, layout),
            )
            if encoded_provenance is not None:
                conn.execute('INSERT INTO schema_draft_runtime_provenance VALUES (?, ?)',
                             (draft_id, encoded_provenance))
        return self.get(draft_id)

    def copy(self, draft_id: str, schema: Schema | dict[str, Any], layout: str) -> dict[str, Any]:
        """A new draft holding ``schema`` and ``layout`` that keeps the baseline and runtime provenance of ``draft_id``.

        The original is untouched. A saved-analysis draft is unique per analysis, so its copy is an import draft;
        migrations still compare against the same baseline.
        """
        encoded = _schema_json(schema)
        _check_layout(layout)
        with self._connect() as conn:
            row = conn.execute(_DRAFT_WITH_PROVENANCE + " WHERE d.id = ?", (draft_id,)).fetchone()
            if row is None:
                raise DraftNotFound(draft_id)
            copy_id = str(uuid.uuid4())
            conn.execute("INSERT INTO schema_drafts VALUES (?, ?, ?, ?, ?, ?, 1)",
                         (copy_id, None, "import", row['baseline_json'], encoded, layout))
            if row['provenance_json'] is not None:
                conn.execute('INSERT INTO schema_draft_runtime_provenance VALUES (?, ?)', (copy_id, row['provenance_json']))
        return self.get(copy_id)

    def get(self, draft_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(_DRAFT_WITH_PROVENANCE + " WHERE d.id = ?", (draft_id,)).fetchone()
        if row is None:
            raise DraftNotFound(draft_id)
        return _read_row(row)

    def delete(self, draft_id: str) -> None:
        """Remove a draft; its runtime provenance goes with it, the source container is untouched."""
        with self._connect() as conn:
            if conn.execute("DELETE FROM schema_drafts WHERE id = ?", (draft_id,)).rowcount == 0:
                raise DraftNotFound(draft_id)

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT d.id, d.analysis_id, d.source_kind, d.revision, p.provenance_json "
                "FROM schema_drafts d LEFT JOIN schema_draft_runtime_provenance p ON p.draft_id=d.id "
                "ORDER BY d.rowid DESC"
            ).fetchall()
        result = []
        for row in rows:
            item = {key: row[key] for key in ('id', 'analysis_id', 'source_kind', 'revision')}
            if row['provenance_json'] is not None:
                item.update(source_kind='runtime', provenance=json.loads(row['provenance_json']))
            result.append(item)
        return result

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
            row = conn.execute(_DRAFT_WITH_PROVENANCE + " WHERE d.id = ?", (draft_id,)).fetchone()
            return _read_row(row)
