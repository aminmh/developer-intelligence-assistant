"""Deletion jobs and the audit log, kept next to the revision registry in snapshots.sqlite."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data_admin import paths


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Journal:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.snapshot_root() / "snapshots.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS data_admin_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor_sub TEXT, actor_email TEXT,
                action TEXT NOT NULL, target_json TEXT NOT NULL, plan_json TEXT, result TEXT NOT NULL,
                error TEXT, job_id TEXT, project_ids TEXT NOT NULL DEFAULT '')""")
            db.execute("""CREATE TABLE IF NOT EXISTS data_admin_jobs (
                job_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, finished_at TEXT, actor_sub TEXT,
                target_json TEXT NOT NULL, steps_json TEXT NOT NULL, state TEXT NOT NULL, error TEXT)""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    # --- audit -----------------------------------------------------------------------------------------
    def audit(self, *, actor: dict[str, str | None], action: str, target: dict[str, Any], result: str,
              plan: dict[str, Any] | None = None, error: str | None = None, job_id: str | None = None) -> None:
        projects = sorted({p for p in _target_projects(target, plan) if p})
        with self._connect() as db:
            db.execute("INSERT INTO data_admin_audit(at, actor_sub, actor_email, action, target_json, plan_json, result, "
                       "error, job_id, project_ids) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (_now(), actor.get("sub"), actor.get("email"), action, json.dumps(target),
                        json.dumps(plan) if plan else None, result, error, job_id, "," + ",".join(projects) + ","))

    def audit_entries(self, *, project_ids: list[str] | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM data_admin_audit", []
        if project_ids is not None:
            if not project_ids:
                return []
            sql += " WHERE " + " OR ".join("project_ids LIKE ?" for _ in project_ids)
            params = [f"%,{p},%" for p in project_ids]
        sql += " ORDER BY id DESC LIMIT ?"
        with self._connect() as db:
            rows = db.execute(sql, (*params, limit)).fetchall()
        return [{**dict(row), "target": json.loads(row["target_json"]),
                 "plan": json.loads(row["plan_json"]) if row["plan_json"] else None,
                 "project_ids": [p for p in row["project_ids"].split(",") if p]} for row in rows]

    # --- jobs ------------------------------------------------------------------------------------------
    def create_job(self, *, actor_sub: str | None, target: dict[str, Any], steps: list[dict[str, Any]]) -> str:
        job_id = "dj_" + uuid.uuid4().hex
        with self._connect() as db:
            db.execute("INSERT INTO data_admin_jobs(job_id, created_at, actor_sub, target_json, steps_json, state) "
                       "VALUES (?,?,?,?,?, 'running')", (job_id, _now(), actor_sub, json.dumps(target), json.dumps(steps)))
        return job_id

    def update_job(self, job_id: str, *, steps: list[dict[str, Any]], state: str | None = None, error: str | None = None) -> None:
        with self._connect() as db:
            if state and state != "running":
                db.execute("UPDATE data_admin_jobs SET steps_json=?, state=?, error=?, finished_at=? WHERE job_id=?",
                           (json.dumps(steps), state, error, _now(), job_id))
            else:
                db.execute("UPDATE data_admin_jobs SET steps_json=? WHERE job_id=?", (json.dumps(steps), job_id))

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM data_admin_jobs WHERE job_id=?", (job_id,)).fetchone()
        return _job(row) if row else None

    def running_job(self) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM data_admin_jobs WHERE state='running' ORDER BY created_at DESC LIMIT 1").fetchone()
        return _job(row) if row else None

    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM data_admin_jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [_job(row) for row in rows]

    def fail_interrupted(self) -> None:
        """A restart kills job threads; mark what was running so it can be retried."""
        with self._connect() as db:
            db.execute("UPDATE data_admin_jobs SET state='failed', error='Interrupted by a service restart. Retry.', "
                       "finished_at=? WHERE state='running'", (_now(),))


def _job(row: sqlite3.Row) -> dict[str, Any]:
    return {"job_id": row["job_id"], "created_at": row["created_at"], "finished_at": row["finished_at"],
            "actor_sub": row["actor_sub"], "target": json.loads(row["target_json"]),
            "steps": json.loads(row["steps_json"]), "state": row["state"], "error": row["error"]}


def _target_projects(target: dict[str, Any], plan: dict[str, Any] | None) -> list[str]:
    projects = [target.get("project_id")]
    for step in (plan or {}).get("steps", []):
        projects.append(step.get("project_id"))
    return [p for p in projects if isinstance(p, str)]
