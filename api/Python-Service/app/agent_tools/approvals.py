"""Pending tool-call approvals and agent run checkpoints.

Decision: @.archcore/agent-tools/paused-agent-runs-resume-from-checkpoint-on-new-stream.adr.md.
When the agent asks for a side-effecting tool, LangGraph's checkpointer saves
the run in SQLite and this store records a pending approval with everything
needed to rebuild the agent later. A decision is accepted once, only while the
approval is pending and unexpired, and only from the user who started the run.
Both survive an API restart.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

APPROVAL_TTL = timedelta(minutes=int(os.environ.get("TOOL_APPROVAL_TTL_MINUTES", "10")))
RETENTION = timedelta(days=1)

_lock = threading.Lock()
_checkpoint_conn: sqlite3.Connection | None = None


def _data_dir() -> Path:
    return Path(os.environ.get("AGENT_RUNS_DIR", "/app/data"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ApprovalError(Exception):
    """A decision that cannot be accepted; ``code`` maps to an HTTP status."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PendingApproval:
    approval_id: str
    run_id: str
    owner: str | None
    project_id: str | None
    status: str
    calls: list[dict[str, Any]]
    run: dict[str, Any]
    created_at: str
    expires_at: str


class ApprovalStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (_data_dir() / "agent_runs.sqlite")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tool_approvals (
                approval_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, owner TEXT, project_id TEXT,
                status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'denied')),
                calls_json TEXT NOT NULL, run_json TEXT NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL, decided_at TEXT)""")
            db.execute("CREATE INDEX IF NOT EXISTS tool_approvals_run ON tool_approvals(run_id)")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def create(self, *, run_id: str, owner: str | None, project_id: str | None,
               calls: list[dict[str, Any]], run: dict[str, Any]) -> PendingApproval:
        approval_id = uuid.uuid4().hex
        created = _now()
        with _lock, self._connect() as db:
            db.execute("INSERT INTO tool_approvals VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                       (approval_id, run_id, owner, project_id, "pending", json.dumps(calls),
                        json.dumps(run, default=str), created.isoformat(), (created + APPROVAL_TTL).isoformat()))
        self.prune()
        return self.get(approval_id)  # type: ignore[return-value]

    def get(self, approval_id: str) -> PendingApproval | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM tool_approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None:
            return None
        return PendingApproval(row["approval_id"], row["run_id"], row["owner"], row["project_id"], row["status"],
                               json.loads(row["calls_json"]), json.loads(row["run_json"]),
                               row["created_at"], row["expires_at"])

    def decide(self, approval_id: str, decision: str, user: str | None) -> PendingApproval:
        """Consume a pending approval once; raise ``ApprovalError`` otherwise."""
        if decision not in {"approve", "deny"}:
            raise ApprovalError("invalid_decision", "decision must be approve or deny.")
        status = "approved" if decision == "approve" else "denied"
        now = _now().isoformat()
        with _lock, self._connect() as db:
            updated = db.execute(
                "UPDATE tool_approvals SET status=?, decided_at=? WHERE approval_id=? AND status='pending' "
                "AND expires_at>? AND (owner IS NULL OR owner=?)",
                (status, now, approval_id, now, user),
            ).rowcount
        if updated == 1:
            return self.get(approval_id)  # type: ignore[return-value]
        approval = self.get(approval_id)
        if approval is None:
            raise ApprovalError("not_found", "This approval does not exist.")
        if approval.owner is not None and approval.owner != user:
            raise ApprovalError("forbidden", "Only the user who started this chat can decide.")
        if approval.status != "pending":
            raise ApprovalError("already_decided", f"This tool call was already {approval.status}.")
        raise ApprovalError("expired", "This approval expired. Ask the question again.")

    def prune(self) -> None:
        cutoff = (_now() - RETENTION).isoformat()
        with self._connect() as db:
            stale = [r[0] for r in db.execute("SELECT run_id FROM tool_approvals WHERE created_at<?", (cutoff,))]
            db.execute("DELETE FROM tool_approvals WHERE created_at<?", (cutoff,))
        if stale:
            saver = checkpointer()
            for run_id in set(stale):
                try:
                    saver.delete_thread(run_id)
                except Exception:  # noqa: BLE001 — pruning is best-effort
                    pass


def checkpointer():
    """Process-wide LangGraph SQLite checkpointer for paused agent runs."""
    global _checkpoint_conn
    from langgraph.checkpoint.sqlite import SqliteSaver

    with _lock:
        if _checkpoint_conn is None:
            path = _data_dir() / "agent_checkpoints.sqlite"
            path.parent.mkdir(parents=True, exist_ok=True)
            _checkpoint_conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        return SqliteSaver(_checkpoint_conn)


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex
