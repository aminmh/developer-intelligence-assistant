"""Durable snapshot state, independent of legacy saved-analysis persistence."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from typing import Iterator

from telemetry.snapshot import (SOURCE_KINDS, Snapshot, is_git_reference, storage_id, validate_project_id,
                                validate_revision_id, validate_scope, validate_source_ref)


# The host reporter reconciles every 60 seconds. Treat a running row without a
# recent heartbeat as stale so a stopped Docker container cannot remain visible
# indefinitely when the reporter or its event stream is interrupted.
ACTIVE_DEPLOYMENT_MAX_AGE_SECONDS = 180


class SnapshotConflict(ValueError):
    """A clean project/commit already owns different source bytes."""


class SnapshotRegistry:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS snapshots (
                project_id TEXT NOT NULL, commit_sha TEXT NOT NULL,
                checksum TEXT NOT NULL, metadata TEXT NOT NULL,
                snapshot_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL, archive_key TEXT, error_code TEXT,
                revision_id TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(project_id, commit_sha))""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(snapshots)")}
            if "revision_id" not in columns:
                db.execute("ALTER TABLE snapshots ADD COLUMN revision_id TEXT")
            missing_revisions = db.execute(
                "SELECT project_id, commit_sha FROM snapshots WHERE revision_id IS NULL OR revision_id=''"
            ).fetchall()
            for row in missing_revisions:
                db.execute(
                    "UPDATE snapshots SET revision_id=? WHERE project_id=? AND commit_sha=?",
                    ("rev_" + uuid4().hex, row["project_id"], row["commit_sha"]),
                )
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS snapshots_revision_id_unique ON snapshots(revision_id)")
            # Revision lineage: what kind of source a revision is, when it arrived, the
            # commit a working tree started from, and the project revision it followed.
            for column in ("source_kind", "base_commit_sha", "parent_revision_id", "created_at"):
                if column not in columns:
                    db.execute(f"ALTER TABLE snapshots ADD COLUMN {column} TEXT")
            db.execute("UPDATE snapshots SET created_at=updated_at WHERE created_at IS NULL")
            db.execute("""UPDATE snapshots SET source_kind=CASE
                WHEN commit_sha LIKE 'upload\\_%' ESCAPE '\\' THEN 'upload'
                WHEN commit_sha LIKE 'wt\\_%' ESCAPE '\\' THEN 'working_tree' ELSE 'git' END
                WHERE source_kind IS NULL""")
            db.execute("CREATE INDEX IF NOT EXISTS snapshots_project_created ON snapshots(project_id, created_at)")
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
                source_only INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(project_id, commit_sha))""")
            job_columns = {row[1] for row in db.execute("PRAGMA table_info(index_jobs)")}
            if "source_only" not in job_columns:
                db.execute("ALTER TABLE index_jobs ADD COLUMN source_only INTEGER NOT NULL DEFAULT 0")
            db.execute("""CREATE TABLE IF NOT EXISTS git_sources (
                user_id TEXT NOT NULL, project_id TEXT NOT NULL, repository TEXT NOT NULL UNIQUE,
                branch TEXT NOT NULL, credential_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'configured',
                last_commit_sha TEXT, last_revision_id TEXT, error_code TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, project_id), UNIQUE(repository, project_id))""")
            # Revocable working-tree upload grants: a desktop helper syncs one folder to one
            # project on behalf of the user who granted it (sync plan phase 6).
            db.execute("""CREATE TABLE IF NOT EXISTS working_tree_grants (
                grant_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_json TEXT NOT NULL,
                token_sha256 TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                revoked_at TEXT, last_upload_at TEXT, last_status TEXT, last_revision_id TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS runtime_settings (
                setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.root / "snapshots.sqlite", timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def get_runtime_setting(self, key: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT setting_value, updated_at FROM runtime_settings WHERE setting_key=?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["setting_value"])
        except (TypeError, ValueError):
            return None
        return {"value": value, "updated_at": row["updated_at"]}

    def update_runtime_setting(self, key: str, change) -> dict:
        """Read-modify-write one setting under an immediate lock, so concurrent writers cannot lose updates."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT setting_value FROM runtime_settings WHERE setting_key=?", (key,)).fetchone()
            try:
                current = json.loads(row["setting_value"]) if row else None
            except (TypeError, ValueError):
                current = None
            value = change(current)
            db.execute(
                """INSERT INTO runtime_settings(setting_key, setting_value) VALUES (?, ?)
                   ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,
                   updated_at=CURRENT_TIMESTAMP""",
                (key, json.dumps(value, separators=(",", ":"), sort_keys=True)))
        return value

    def set_runtime_setting(self, key: str, value: dict) -> None:
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True)
        with self._connect() as db:
            db.execute(
                """INSERT INTO runtime_settings(setting_key, setting_value) VALUES (?, ?)
                   ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,
                   updated_at=CURRENT_TIMESTAMP""",
                (key, encoded),
            )

    @contextmanager
    def lock(self, project_id: str, commit_sha: str, *, blocking: bool = True) -> Iterator[None]:
        # File locks serialize retries across threads/processes and release on
        # process death. A retry can safely resume interrupted indexing.
        path = self.root / (storage_id(project_id, commit_sha, "") + ".lock")
        with path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def get(self, project_id: str, commit_sha: str) -> dict:
        # The historical SQL key column stores source references, including uploads.
        validate_source_ref(project_id, commit_sha)
        with self._connect() as db:
            row = db.execute("SELECT * FROM snapshots WHERE project_id=? AND commit_sha=?",
                             (project_id, commit_sha)).fetchone()
        if row is None:
            return {"project_id": project_id, "source_ref": commit_sha,
                    "commit_sha": commit_sha if is_git_reference(commit_sha) else None, "state": "awaiting_source"}
        return self._row_result(row)

    @staticmethod
    def _row_result(row: sqlite3.Row) -> dict:
        metadata = json.loads(row["metadata"])
        keys = row.keys()
        result = {**metadata, "revision_id": row["revision_id"], "state": row["state"], "error_code": row["error_code"],
                  "updated_at": row["updated_at"],
                  **{key: row[key] for key in ("source_kind", "base_commit_sha", "parent_revision_id", "created_at")
                     if key in keys}}
        if row["state"] == "ready":
            result["collection_name"] = "snapshot_" + storage_id(row["project_id"], row["commit_sha"], "")
        return result

    def latest_ready(self, project_id: str, kinds: tuple[str, ...] = SOURCE_KINDS) -> dict | None:
        """The project's newest ready revision of the given source kinds, or None."""
        validate_project_id(project_id)
        if not kinds or any(kind not in SOURCE_KINDS for kind in kinds):
            raise ValueError("Invalid source kinds")
        marks = ",".join("?" for _ in kinds)
        with self._connect() as db:
            row = db.execute(
                f"""SELECT * FROM snapshots WHERE project_id=? AND state='ready' AND source_kind IN ({marks})
                    ORDER BY created_at DESC, rowid DESC LIMIT 1""", (project_id, *kinds)).fetchone()
        return self._row_result(row) if row else None

    def ready_revisions_by_project(self, per_project: int = 10) -> dict[str, list[dict]]:
        """Each project's ready revisions, newest first, for the chat revision picker."""
        with self._connect() as db:
            rows = db.execute("SELECT * FROM snapshots WHERE state='ready' ORDER BY project_id, created_at DESC, rowid DESC").fetchall()
        result: dict[str, list[dict]] = {}
        for row in rows:
            items = result.setdefault(row["project_id"], [])
            if len(items) < per_project:
                items.append(self._row_result(row))
        return result

    def revisions_by_project(self, per_project: int = 20) -> dict[str, list[dict]]:
        """Each project's revisions in any state, newest first, for the app-wide project switcher."""
        with self._connect() as db:
            rows = db.execute("SELECT * FROM snapshots ORDER BY project_id, created_at DESC, rowid DESC").fetchall()
        result: dict[str, list[dict]] = {}
        for row in rows:
            items = result.setdefault(row["project_id"], [])
            if len(items) < per_project:
                items.append(self._row_result(row))
        return result

    def project_revisions(self, project_id: str) -> list[dict]:
        """All revisions of a project, newest first."""
        validate_project_id(project_id)
        with self._connect() as db:
            rows = db.execute("SELECT * FROM snapshots WHERE project_id=? ORDER BY created_at DESC, rowid DESC",
                              (project_id,)).fetchall()
        return [self._row_result(row) for row in rows]

    def get_by_revision_id(self, project_id: str, revision_id: str) -> dict | None:
        validate_project_id(project_id)
        validate_revision_id(revision_id)
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM snapshots WHERE project_id=? AND revision_id=?",
                (project_id, revision_id),
            ).fetchone()
        if row is None:
            return None
        return self._row_result(row)

    def archive_key(self, project_id: str, commit_sha: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT archive_key FROM snapshots WHERE project_id=? AND commit_sha=?", (project_id, commit_sha)).fetchone()
        return row["archive_key"] if row else None

    def has_deployment(self, project_id: str, commit_sha: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM deployments WHERE project_id=? AND commit_sha=? AND state='running' "
                              "AND datetime(updated_at) >= datetime('now', ?) LIMIT 1",
                              (project_id, commit_sha, f"-{ACTIVE_DEPLOYMENT_MAX_AGE_SECONDS} seconds")).fetchone() is not None

    def runtime_projects(self) -> list[dict]:
        """List source snapshots with their current, allow-listed deployments."""
        with self._connect() as db:
            snapshots = db.execute("""SELECT project_id, commit_sha, revision_id, state, updated_at, metadata
                FROM snapshots ORDER BY project_id, updated_at DESC""").fetchall()
            deployments = db.execute("""SELECT d.container_id, d.project_id, d.environment, d.service,
                d.commit_sha, s.revision_id, d.image_id, d.state, d.updated_at FROM deployments d
                LEFT JOIN snapshots s ON s.project_id=d.project_id AND s.commit_sha=d.commit_sha
                WHERE d.state='running' AND datetime(d.updated_at) >= datetime('now', ?)
                ORDER BY d.project_id, d.service, d.container_id""",
                (f"-{ACTIVE_DEPLOYMENT_MAX_AGE_SECONDS} seconds",)).fetchall()
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
                "revision_id": row["revision_id"],
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
                "revision_id": None,
                "state": "awaiting_source",
                "containers": containers,
                "running_containers": 0,
                "running_project_containers": len(containers),
                "running_commit_shas": sorted({item["commit_sha"] for item in containers}),
                "deployment_mismatch": True,
            })
        return result

    def running_deployments(self, project_id: str, commit_sha: str | None = None) -> list[dict]:
        query = """SELECT d.container_id, d.project_id, d.environment, d.service, d.commit_sha,
            s.revision_id, d.image_id, d.state, d.updated_at FROM deployments d
            LEFT JOIN snapshots s ON s.project_id=d.project_id AND s.commit_sha=d.commit_sha
            WHERE d.project_id=? AND d.state='running'
            AND datetime(d.updated_at) >= datetime('now', ?)"""
        values: tuple = (project_id, f"-{ACTIVE_DEPLOYMENT_MAX_AGE_SECONDS} seconds")
        if commit_sha:
            query += " AND d.commit_sha=?"
            values += (commit_sha,)
        query += " ORDER BY environment, service, container_id"
        with self._connect() as db:
            return [dict(row) for row in db.execute(query, values).fetchall()]

    def report_deployments(self, host_id: str, containers: list[dict], *, inventory: bool = False) -> list[tuple[str, str]]:
        """Idempotently upsert allow-listed records; inventory also closes missing host mappings."""
        return self.report_deployment_changes(host_id, containers, inventory=inventory)[0]

    def report_deployment_changes(
        self, host_id: str, containers: list[dict], *, inventory: bool = False,
    ) -> tuple[list[tuple[str, str]], bool]:
        """Like report_deployments, and also says whether any deployment record actually changed."""
        now = datetime.now(timezone.utc).isoformat()
        fields = ("project_id", "environment", "service", "commit_sha", "image_id", "state")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            seen = {item["container_id"] for item in containers}
            existing = {row["container_id"]: tuple(row[name] for name in fields) for row in db.execute(
                "SELECT container_id, project_id, environment, service, commit_sha, image_id, state FROM deployments WHERE host_id=?",
                (host_id,)).fetchall()}
            changed = any(existing.get(item["container_id"]) != tuple(item[name] for name in fields) for item in containers)
            if inventory:
                rows = db.execute("SELECT container_id, project_id, commit_sha FROM deployments WHERE host_id=? AND state='running'", (host_id,)).fetchall()
                for row in rows:
                    if row["container_id"] not in seen:
                        changed = True
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
            return list(dict.fromkeys((item["project_id"], item["commit_sha"]) for item in containers)), changed

    def mark_event(self, host_id: str, event_id: str) -> bool:
        with self._connect() as db:
            cur = db.execute("INSERT OR IGNORE INTO deployment_events(host_id,event_id) VALUES(?,?)", (host_id, event_id))
            return cur.rowcount == 1

    def has_event(self, host_id: str, event_id: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM deployment_events WHERE host_id=? AND event_id=?", (host_id, event_id)).fetchone() is not None

    def queue_if_deployable(
        self, project_id: str, commit_sha: str, *, retry_failed: bool = False
    ) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT s.state,s.archive_key,j.state AS job_state "
                "FROM snapshots s LEFT JOIN index_jobs j "
                "ON j.project_id=s.project_id AND j.commit_sha=s.commit_sha "
                "WHERE s.project_id=? AND s.commit_sha=?",
                (project_id, commit_sha),
            ).fetchone()
            if not row or not row["archive_key"] or row["state"] in ("ready", "deleting"):
                return False
            if row["job_state"] == "running":
                return False
            if (row["state"] == "failed" or row["job_state"] == "failed") and not retry_failed:
                return False
            if not db.execute("SELECT 1 FROM deployments WHERE project_id=? AND commit_sha=? AND state='running' "
                              "AND datetime(updated_at) >= datetime('now', ?) LIMIT 1",
                              (project_id, commit_sha, f"-{ACTIVE_DEPLOYMENT_MAX_AGE_SECONDS} seconds")).fetchone():
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
                AND s.state NOT IN ('ready','failed','deleting') AND EXISTS (SELECT 1 FROM deployments d
                    WHERE d.project_id=s.project_id AND d.commit_sha=s.commit_sha AND d.state='running')""").fetchall()
            for row in ready:
                db.execute("INSERT OR IGNORE INTO index_jobs(project_id,commit_sha) VALUES(?,?)", tuple(row))
                db.execute("UPDATE snapshots SET state='queued',updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND commit_sha=? AND state NOT IN ('ready','failed','deleting')", tuple(row))
            row = db.execute("""SELECT j.project_id,j.commit_sha FROM index_jobs j
                WHERE j.state IN ('queued','running') AND EXISTS (SELECT 1 FROM snapshots s WHERE s.project_id=j.project_id AND s.commit_sha=j.commit_sha AND s.state!='deleting') AND (j.source_only=1 OR EXISTS (
                    SELECT 1 FROM deployments d WHERE d.project_id=j.project_id
                    AND d.commit_sha=j.commit_sha AND d.state='running'))
                ORDER BY j.updated_at LIMIT 1""").fetchone()
            if row:
                db.execute("UPDATE index_jobs SET state='running',attempts=attempts+1,updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND commit_sha=?", tuple(row))
                db.execute("UPDATE snapshots SET state='indexing',updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND commit_sha=?", tuple(row))
                return tuple(row)
        return None

    def finish_job(self, project_id: str, commit_sha: str, *, success: bool) -> None:
        with self._connect() as db:
            job = db.execute("SELECT state FROM index_jobs WHERE project_id=? AND commit_sha=?",
                             (project_id, commit_sha)).fetchone()
            if job and job["state"] == "superseded":
                # A newer working tree replaced this one while it indexed; retention removes it.
                db.execute("UPDATE snapshots SET state='superseded',updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND commit_sha=? AND state!='deleting'",
                           (project_id, commit_sha))
                return
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

    def register(self, snapshot: Snapshot, *, base_commit_sha: str | None = None) -> None:
        """Record a revision once; repeats of the same source bytes are no-ops.

        ``base_commit_sha`` is the commit a working tree started from. It is lineage
        metadata only and never becomes the revision's ``commit_sha``.
        """
        if base_commit_sha is not None:
            if not snapshot.dirty:
                raise ValueError("Only working-tree revisions record a base commit")
            validate_scope(snapshot.project_id, base_commit_sha)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT checksum,state FROM snapshots WHERE project_id=? AND commit_sha=?",
                             (snapshot.project_id, snapshot.reference)).fetchone()
            if row and row["checksum"] != snapshot.source_archive_sha256:
                raise SnapshotConflict("This clean snapshot already has a different source archive")
            if row and row["state"] == "deleting":
                raise SnapshotConflict("This revision is being removed. Retry after removal finishes.")
            parent = db.execute("""SELECT revision_id FROM snapshots WHERE project_id=? AND state='ready'
                ORDER BY created_at DESC, rowid DESC LIMIT 1""", (snapshot.project_id,)).fetchone()
            db.execute("""INSERT OR IGNORE INTO snapshots
                (project_id, commit_sha, checksum, metadata, snapshot_key, state, revision_id,
                 source_kind, base_commit_sha, parent_revision_id, created_at)
                VALUES (?, ?, ?, ?, ?, 'awaiting_source', ?, ?, ?, ?, ?)""",
                (snapshot.project_id, snapshot.reference, snapshot.source_archive_sha256,
                 json.dumps(snapshot.metadata()), snapshot.key, "rev_" + uuid4().hex,
                 snapshot.source_kind, base_commit_sha, parent["revision_id"] if parent else None,
                 datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")))

    def save_git_source(self, user_id: str, project_id: str, repository: str, branch: str,
                        credential_id: str) -> dict:
        if not user_id or len(user_id) > 512:
            raise ValueError("Invalid user identity")
        validate_project_id(project_id)
        try:
            with self._connect() as db:
                db.execute("""INSERT INTO git_sources(user_id,project_id,repository,branch,credential_id,status)
                    VALUES(?,?,?,?,?,'configured') ON CONFLICT(user_id,project_id) DO UPDATE SET
                    repository=excluded.repository, branch=excluded.branch, credential_id=excluded.credential_id,
                    status='configured',
                    last_commit_sha=CASE WHEN git_sources.repository!=excluded.repository OR git_sources.branch!=excluded.branch THEN NULL ELSE git_sources.last_commit_sha END,
                    last_revision_id=CASE WHEN git_sources.repository!=excluded.repository OR git_sources.branch!=excluded.branch THEN NULL ELSE git_sources.last_revision_id END,
                    error_code=NULL, updated_at=CURRENT_TIMESTAMP""",
                    (user_id, project_id, repository, branch, credential_id))
        except sqlite3.IntegrityError as exc:
            raise ValueError("This GitHub repository is already connected to another project.") from exc
        return self.get_git_source(user_id, project_id)

    def get_git_source(self, user_id: str, project_id: str) -> dict | None:
        validate_project_id(project_id)
        with self._connect() as db:
            row = db.execute("SELECT * FROM git_sources WHERE user_id=? AND project_id=?",
                             (user_id, project_id)).fetchone()
        return dict(row) if row else None

    def list_git_sources(self) -> list[dict]:
        """Every configured Git source, for the scheduled reconcile of missed webhooks."""
        with self._connect() as db:
            rows = db.execute("SELECT * FROM git_sources ORDER BY project_id").fetchall()
        return [dict(row) for row in rows]

    def git_source_for_repository(self, repository: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM git_sources WHERE repository=? ORDER BY updated_at DESC LIMIT 1",
                             (repository,)).fetchone()
        return dict(row) if row else None

    def update_git_source_status(self, user_id: str, project_id: str, *, status: str,
                                 commit_sha: str | None = None, revision_id: str | None = None,
                                 error_code: str | None = None) -> None:
        with self._connect() as db:
            db.execute("""UPDATE git_sources SET status=?, last_commit_sha=COALESCE(?,last_commit_sha),
                last_revision_id=COALESCE(?,last_revision_id), error_code=?, updated_at=CURRENT_TIMESTAMP
                WHERE user_id=? AND project_id=?""",
                (status, commit_sha, revision_id, error_code, user_id, project_id))

    def is_superseded(self, project_id: str, source_ref: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT state FROM index_jobs WHERE project_id=? AND commit_sha=?",
                             (project_id, source_ref)).fetchone()
        return bool(row and row["state"] == "superseded")

    def supersede_working_trees(self, project_id: str, keep_ref: str) -> int:
        """Latest wins: every other unfinished working-tree job of the project stops.

        A queued job is never claimed; a running one sees the flag between embedding
        batches and stops. Its revision becomes 'superseded' and retention removes it.
        """
        validate_source_ref(project_id, keep_ref)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            refs = [row[0] for row in db.execute(
                """SELECT commit_sha FROM index_jobs WHERE project_id=? AND substr(commit_sha, 1, 3)='wt_'
                   AND commit_sha!=? AND state IN ('queued','running')""", (project_id, keep_ref))]
            for ref in refs:
                db.execute("UPDATE index_jobs SET state='superseded',updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND commit_sha=?",
                           (project_id, ref))
                db.execute("""UPDATE snapshots SET state='superseded',updated_at=CURRENT_TIMESTAMP
                    WHERE project_id=? AND commit_sha=? AND state IN ('queued','storing','uploaded')""", (project_id, ref))
        return len(refs)

    def touch_created(self, project_id: str, source_ref: str) -> None:
        """A working tree that returned to an earlier state becomes the latest again."""
        with self._connect() as db:
            db.execute("UPDATE snapshots SET created_at=? WHERE project_id=? AND commit_sha=?",
                       (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"), project_id, source_ref))

    def queue_source_index(self, project_id: str, commit_sha: str) -> bool:
        """Queue a Git or working-tree snapshot without asserting a Runtime deployment exists."""
        validate_source_ref(project_id, commit_sha)
        with self._connect() as db:
            row = db.execute("SELECT state,archive_key FROM snapshots WHERE project_id=? AND commit_sha=?",
                             (project_id, commit_sha)).fetchone()
            if not row or not row["archive_key"] or row["state"] in ("ready", "deleting"):
                return False
            db.execute("""INSERT INTO index_jobs(project_id,commit_sha,state,source_only)
                VALUES(?,?,'queued',1) ON CONFLICT(project_id,commit_sha) DO UPDATE SET
                state=CASE WHEN index_jobs.state='running' THEN index_jobs.state ELSE 'queued' END,
                source_only=1, updated_at=CURRENT_TIMESTAMP""", (project_id, commit_sha))
            db.execute("""UPDATE snapshots SET state='queued',error_code=NULL,updated_at=CURRENT_TIMESTAMP
                WHERE project_id=? AND commit_sha=? AND state!='ready'""", (project_id, commit_sha))
        return True

    def is_ready_collection(self, collection: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT state FROM snapshots WHERE snapshot_key=?",
                             (collection.removeprefix("snapshot_"),)).fetchone()
        return row is not None and row["state"] == "ready"

    def transition(self, snapshot: Snapshot, state: str, *, archive_key: str | None = None,
                   error_code: str | None = None) -> None:
        if state not in {"uploaded", "storing", "awaiting_deployment", "indexing", "failed", "ready"}:
            raise ValueError("Invalid snapshot state")
        with self._connect() as db:
            db.execute("""UPDATE snapshots SET state=?, archive_key=COALESCE(?, archive_key),
                error_code=?, updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND commit_sha=? AND state!='deleting'""",
                (state, archive_key, error_code, snapshot.project_id, snapshot.reference))


def default_registry() -> SnapshotRegistry:
    from infra.settings import settings
    root = os.environ.get("TELEMETRY_SNAPSHOT_ROOT") or str(Path(settings.workspace_root).parent / "snapshots")
    return SnapshotRegistry(root)
