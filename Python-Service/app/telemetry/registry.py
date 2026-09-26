"""Durable snapshot state, independent of legacy saved-analysis persistence."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
from typing import Iterator

from telemetry.snapshot import Snapshot, storage_id, validate_scope


class SnapshotConflict(ValueError):
    """A clean project/commit already owns different source bytes."""


class SnapshotRegistry:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS snapshots (
                project_id TEXT NOT NULL, commit_sha TEXT NOT NULL,
                checksum TEXT NOT NULL, metadata TEXT NOT NULL,
                snapshot_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL, archive_key TEXT, error_code TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(project_id, commit_sha))""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.root / "snapshots.sqlite", timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def lock(self, project_id: str, commit_sha: str) -> Iterator[None]:
        # File locks serialize retries across threads/processes and release on
        # process death. A retry can safely resume interrupted indexing.
        path = self.root / (storage_id(project_id, commit_sha, "") + ".lock")
        with path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def get(self, project_id: str, commit_sha: str) -> dict:
        validate_scope(project_id, commit_sha)
        with self._connect() as db:
            row = db.execute("SELECT * FROM snapshots WHERE project_id=? AND commit_sha=?",
                             (project_id, commit_sha)).fetchone()
        if row is None:
            return {"project_id": project_id, "commit_sha": commit_sha, "state": "awaiting_source"}
        metadata = json.loads(row["metadata"])
        result = {**metadata, "state": row["state"], "error_code": row["error_code"],
                  "updated_at": row["updated_at"]}
        if row["state"] == "ready":
            result["collection_name"] = "snapshot_" + storage_id(project_id, commit_sha, "")
        return result

    def register(self, snapshot: Snapshot) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT checksum FROM snapshots WHERE project_id=? AND commit_sha=?",
                             (snapshot.project_id, snapshot.commit_sha)).fetchone()
            if row and row["checksum"] != snapshot.source_archive_sha256:
                raise SnapshotConflict("This clean snapshot already has a different source archive")
            db.execute("""INSERT OR IGNORE INTO snapshots
                (project_id, commit_sha, checksum, metadata, snapshot_key, state) VALUES (?, ?, ?, ?, ?, 'awaiting_source')""",
                (snapshot.project_id, snapshot.commit_sha, snapshot.source_archive_sha256,
                 json.dumps(snapshot.metadata()), snapshot.key))

    def is_ready_collection(self, collection: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT state FROM snapshots WHERE snapshot_key=?",
                             (collection.removeprefix("snapshot_"),)).fetchone()
        return row is not None and row["state"] == "ready"

    def transition(self, snapshot: Snapshot, state: str, *, archive_key: str | None = None,
                   error_code: str | None = None) -> None:
        if state not in {"storing", "indexing", "failed", "ready"}:
            raise ValueError("Invalid snapshot state")
        with self._connect() as db:
            db.execute("""UPDATE snapshots SET state=?, archive_key=COALESCE(?, archive_key),
                error_code=?, updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND commit_sha=?""",
                (state, archive_key, error_code, snapshot.project_id, snapshot.commit_sha))


def default_registry() -> SnapshotRegistry:
    from infra.settings import settings
    root = os.environ.get("TELEMETRY_SNAPSHOT_ROOT") or str(Path(settings.workspace_root).parent / "snapshots")
    return SnapshotRegistry(root)
