"""SQLite store for regression checks: one row per check holding its state and report."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from regression.model import STATES, new_report


class CheckNotFound(LookupError):
    """No check with this id exists for the project."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_path() -> Path:
    from infra.settings import settings

    root = Path(os.environ.get("TELEMETRY_SNAPSHOT_ROOT") or Path(settings.workspace_root).parent / "snapshots")
    root.mkdir(parents=True, exist_ok=True)
    return root / "regression.sqlite"


class RegressionStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else _default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS regression_checks (
                check_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, base_revision_id TEXT NOT NULL,
                candidate_revision_id TEXT NOT NULL, created_by TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, state TEXT NOT NULL, error TEXT, report_json TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS regression_checks_project "
                       "ON regression_checks(project_id, created_at DESC)")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {"check_id": row["check_id"], "project_id": row["project_id"],
                "base_revision_id": row["base_revision_id"], "candidate_revision_id": row["candidate_revision_id"],
                "created_by": row["created_by"], "created_at": row["created_at"], "updated_at": row["updated_at"],
                "state": row["state"], "error": row["error"], "report": json.loads(row["report_json"])}

    def create(self, project_id: str, base_revision_id: str, candidate_revision_id: str,
               created_by: str | None = None) -> dict[str, Any]:
        check_id = "rc_" + uuid4().hex
        now = _now()
        report = new_report(check_id, project_id, base_revision_id, candidate_revision_id)
        with self._connect() as db:
            db.execute("""INSERT INTO regression_checks (check_id, project_id, base_revision_id,
                candidate_revision_id, created_by, created_at, updated_at, state, error, report_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?)""",
                       (check_id, project_id, base_revision_id, candidate_revision_id, created_by, now, now,
                        json.dumps(report)))
        return self.get(project_id, check_id)

    def get(self, project_id: str, check_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM regression_checks WHERE check_id=? AND project_id=?",
                             (check_id, project_id)).fetchone()
        if row is None:
            raise CheckNotFound(f"Regression check {check_id} does not exist for the project.")
        return self._row(row)

    def list(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM regression_checks WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                              (project_id, max(1, min(limit, 200)))).fetchall()
        return [self._row(row) for row in rows]

    def save(self, project_id: str, check_id: str, *, state: str, report: dict[str, Any] | None = None,
             error: str | None = None) -> dict[str, Any]:
        if state not in STATES:
            raise ValueError(f"Unknown state {state}.")
        current = self.get(project_id, check_id)
        with self._connect() as db:
            db.execute("UPDATE regression_checks SET state=?, error=?, report_json=?, updated_at=? "
                       "WHERE check_id=? AND project_id=?",
                       (state, error, json.dumps(report if report is not None else current["report"]), _now(),
                        check_id, project_id))
        return self.get(project_id, check_id)

    def fail_interrupted(self) -> int:
        """Checks left pending or running by a stopped API can never finish; mark them failed."""
        with self._connect() as db:
            return db.execute("UPDATE regression_checks SET state='failed', error=?, updated_at=? "
                              "WHERE state IN ('pending', 'running')",
                              ("The API restarted before the check finished. Run it again.", _now())).rowcount

    def delete(self, project_id: str, check_id: str) -> None:
        with self._connect() as db:
            deleted = db.execute("DELETE FROM regression_checks WHERE check_id=? AND project_id=?",
                                 (check_id, project_id)).rowcount
        if not deleted:
            raise CheckNotFound(f"Regression check {check_id} does not exist for the project.")
