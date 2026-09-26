"""Durable snapshot state, independent of legacy saved-analysis persistence."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timedelta, timezone
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
            db.execute("""CREATE TABLE IF NOT EXISTS deployments (
                host_id TEXT NOT NULL, container_id TEXT NOT NULL,
                project_id TEXT NOT NULL, environment TEXT NOT NULL, service TEXT NOT NULL,
                commit_sha TEXT NOT NULL, image_id TEXT NOT NULL,
                state TEXT NOT NULL, started_at TEXT, stopped_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(host_id, container_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS deployment_events (
                host_id TEXT NOT NULL, event_id TEXT NOT NULL,
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(host_id, event_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS index_jobs (
                project_id TEXT NOT NULL, commit_sha TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
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

    def archive_key(self, project_id: str, commit_sha: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT archive_key FROM snapshots WHERE project_id=? AND commit_sha=?", (project_id, commit_sha)).fetchone()
        return row["archive_key"] if row else None

    def has_deployment(self, project_id: str, commit_sha: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM deployments WHERE project_id=? AND commit_sha=? AND state='running' LIMIT 1",
                              (project_id, commit_sha)).fetchone() is not None

    def runtime_projects(self) -> list[dict]:
        """List source snapshots with their current, allow-listed deployments."""
        with self._connect() as db:
            snapshots = db.execute("""SELECT project_id, commit_sha, state, updated_at, metadata
                FROM snapshots ORDER BY project_id, updated_at DESC""").fetchall()
            deployments = db.execute("""SELECT container_id, project_id, environment, service,
                commit_sha, image_id, state, updated_at FROM deployments
                WHERE state='running' ORDER BY project_id, service, container_id""").fetchall()
        active: dict[str, list[dict]] = {}
        for row in deployments:
            active.setdefault(row["project_id"], []).append(dict(row))
        result = []
        for row in snapshots:
            containers = active.get(row["project_id"], [])
            metadata = json.loads(row["metadata"])
            matching = [item for item in containers if item["commit_sha"] == row["commit_sha"]]
            result.append({
                **metadata,
                "state": row["state"],
                "updated_at": row["updated_at"],
                "containers": matching,
                "running_containers": len(matching),
                "running_project_containers": len(containers),
                "running_commit_shas": sorted({item["commit_sha"] for item in containers}),
                "deployment_mismatch": bool(containers) and not matching,
            })
        known = {row["project_id"] for row in snapshots}
        for project_id, containers in active.items():
            if project_id in known:
                continue
            result.append({
                "project_id": project_id,
                "commit_sha": None,
                "state": "awaiting_source",
                "containers": containers,
                "running_containers": 0,
                "running_project_containers": len(containers),
                "running_commit_shas": sorted({item["commit_sha"] for item in containers}),
                "deployment_mismatch": True,
            })
        return result

    def running_deployments(self, project_id: str, commit_sha: str | None = None) -> list[dict]:
        query = """SELECT container_id, project_id, environment, service, commit_sha,
            image_id, state, updated_at FROM deployments WHERE project_id=? AND state='running'"""
        values: tuple = (project_id,)
        if commit_sha:
            query += " AND commit_sha=?"
            values += (commit_sha,)
        query += " ORDER BY environment, service, container_id"
        with self._connect() as db:
            return [dict(row) for row in db.execute(query, values).fetchall()]

    def report_deployments(self, host_id: str, containers: list[dict], *, inventory: bool = False) -> list[tuple[str, str]]:
        """Idempotently upsert allow-listed records; inventory also closes missing host mappings."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            seen = {item["container_id"] for item in containers}
            if inventory:
                rows = db.execute("SELECT container_id, project_id, commit_sha FROM deployments WHERE host_id=? AND state='running'", (host_id,)).fetchall()
                for row in rows:
                    if row["container_id"] not in seen:
                        db.execute("UPDATE deployments SET state='stopped', stopped_at=?, updated_at=CURRENT_TIMESTAMP WHERE host_id=? AND container_id=? AND state='running'",
                                   (now, host_id, row["container_id"]))
            for item in containers:
                db.execute("""INSERT INTO deployments(host_id, container_id, project_id, environment, service, commit_sha,
                           image_id, state, started_at, stopped_at) VALUES(?,?,?,?,?,?,?,?, ?, ?)
                           ON CONFLICT(host_id,container_id) DO UPDATE SET project_id=excluded.project_id,
                           environment=excluded.environment,service=excluded.service,commit_sha=excluded.commit_sha,image_id=excluded.image_id,
                           started_at=CASE WHEN deployments.state!='running' THEN excluded.started_at ELSE deployments.started_at END,
                           state=excluded.state,stopped_at=excluded.stopped_at,updated_at=CURRENT_TIMESTAMP""",
                           (host_id, item["container_id"], item["project_id"], item["environment"], item["service"],
                            item["commit_sha"], item["image_id"], item["state"],
                            now if item["state"] == "running" else None,
                            now if item["state"] == "stopped" else None))
            return list(dict.fromkeys((item["project_id"], item["commit_sha"]) for item in containers))

    def mark_event(self, host_id: str, event_id: str) -> bool:
        with self._connect() as db:
            cur = db.execute("INSERT OR IGNORE INTO deployment_events(host_id,event_id) VALUES(?,?)", (host_id, event_id))
            return cur.rowcount == 1

    def has_event(self, host_id: str, event_id: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM deployment_events WHERE host_id=? AND event_id=?", (host_id, event_id)).fetchone() is not None

    def queue_if_deployable(self, project_id: str, commit_sha: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT state,archive_key FROM snapshots WHERE project_id=? AND commit_sha=?", (project_id, commit_sha)).fetchone()
            if not row or not row["archive_key"] or row["state"] == "ready":
                return False
            if not db.execute("SELECT 1 FROM deployments WHERE project_id=? AND commit_sha=? AND state='running' LIMIT 1", (project_id, commit_sha)).fetchone():
                db.execute("UPDATE snapshots SET state='awaiting_deployment',updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND commit_sha=? AND state NOT IN ('awaiting_deployment','failed','ready')", (project_id, commit_sha))
                return False
            db.execute("INSERT OR IGNORE INTO index_jobs(project_id,commit_sha) VALUES(?,?)", (project_id, commit_sha))
            db.execute("UPDATE index_jobs SET state='queued',updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND commit_sha=? AND state='failed'", (project_id, commit_sha))
            db.execute("UPDATE snapshots SET state='queued',updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND commit_sha=? AND state!='ready'", (project_id, commit_sha))
            return True

    def claim_job(self) -> tuple[str, str] | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ready = db.execute("""SELECT s.project_id,s.commit_sha FROM snapshots s WHERE s.archive_key IS NOT NULL
                AND s.state NOT IN ('ready','failed') AND EXISTS (SELECT 1 FROM deployments d
                    WHERE d.project_id=s.project_id AND d.commit_sha=s.commit_sha AND d.state='running')""").fetchall()
            for row in ready:
                db.execute("INSERT OR IGNORE INTO index_jobs(project_id,commit_sha) VALUES(?,?)", tuple(row))
                db.execute("UPDATE snapshots SET state='queued',updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND commit_sha=? AND state NOT IN ('ready','failed')", tuple(row))
            row = db.execute("SELECT project_id,commit_sha FROM index_jobs WHERE state IN ('queued','running') ORDER BY updated_at LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE index_jobs SET state='running',attempts=attempts+1,updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND commit_sha=?", tuple(row))
                db.execute("UPDATE snapshots SET state='indexing',updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND commit_sha=?", tuple(row))
                return tuple(row)
        return None

    def finish_job(self, project_id: str, commit_sha: str, *, success: bool) -> None:
        with self._connect() as db:
            state = 'done' if success else 'failed'
            db.execute("UPDATE index_jobs SET state=?,updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND commit_sha=?", (state, project_id, commit_sha))
            db.execute("UPDATE snapshots SET state=?,error_code=?,updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND commit_sha=?",
                       ('ready' if success else 'failed', None if success else 'snapshot_indexing_failed', project_id, commit_sha))

    def expired_archives(self) -> list[tuple[str, str, str]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        with self._connect() as db:
            rows = db.execute("SELECT project_id,commit_sha,archive_key FROM snapshots WHERE state IN ('awaiting_deployment','awaiting_source') AND archive_key IS NOT NULL AND updated_at < ?", (cutoff,)).fetchall()
            return [(r['project_id'],r['commit_sha'],r['archive_key']) for r in rows]

    def forget_expired_archive(self, project_id: str, commit_sha: str, key: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE snapshots SET archive_key=NULL,state='awaiting_source',updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND commit_sha=? AND archive_key=? AND state IN ('awaiting_deployment','awaiting_source')",
                       (project_id, commit_sha, key))

    def stopped_deployments_before(self, days: int = 7) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as db:
            db.execute("DELETE FROM deployments WHERE state='stopped' AND stopped_at < ?", (cutoff,))
            db.execute("DELETE FROM deployment_events WHERE received_at < datetime('now','-7 days')")

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
        if state not in {"storing", "awaiting_deployment", "indexing", "failed", "ready"}:
            raise ValueError("Invalid snapshot state")
        with self._connect() as db:
            db.execute("""UPDATE snapshots SET state=?, archive_key=COALESCE(?, archive_key),
                error_code=?, updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND commit_sha=?""",
                (state, archive_key, error_code, snapshot.project_id, snapshot.commit_sha))


def default_registry() -> SnapshotRegistry:
    from infra.settings import settings
    root = os.environ.get("TELEMETRY_SNAPSHOT_ROOT") or str(Path(settings.workspace_root).parent / "snapshots")
    return SnapshotRegistry(root)
