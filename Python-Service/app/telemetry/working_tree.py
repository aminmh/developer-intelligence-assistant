"""Working-tree sync: a desktop helper uploads a developer's uncommitted folder state.

Plan: @.archcore/knowledge-base/keep-the-index-in-sync-with-a-changing-codebase.plan.md (phase 6).

A project member with operator or administrator access creates a **grant** in the web
app. The desktop helper asks the user to choose a folder and approve it, keeps the grant
token in the OS keychain, and uploads a deterministic ZIP of the folder after edits
settle. Only the token's SHA-256 is stored here. The web server re-checks the granting
user's current project access on every upload, so removing that user's access stops
uploads, and so does revoking the grant.

Each upload becomes a working-tree revision (``wt_<sha256>``, ``dirty``, no commit SHA;
the folder's ``HEAD`` is lineage metadata only). One working tree per project is live:
a newer upload supersedes unfinished older ones (latest wins), and retention keeps only
the newest ready one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from typing import Any
from uuid import uuid4

from telemetry.registry import SnapshotRegistry, default_registry
from telemetry.snapshot import validate_project_id

_GRANT_ID = re.compile(r"wtg_[0-9a-f]{32}")
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
MAX_GRANTS_PER_PROJECT = 20


class GrantError(ValueError):
    pass


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _public(row: Any) -> dict[str, Any]:
    user = json.loads(row["user_json"])
    return {"grant_id": row["grant_id"], "project_id": row["project_id"],
            "user": {key: user.get(key) for key in ("sub", "email", "name") if user.get(key)},
            "created_at": row["created_at"], "revoked_at": row["revoked_at"],
            "last_upload_at": row["last_upload_at"], "last_status": row["last_status"],
            "last_revision_id": row["last_revision_id"]}


class WorkingTreeGrants:
    def __init__(self, registry: SnapshotRegistry | None = None):
        self.registry = registry or default_registry()

    def create(self, project_id: str, user: dict[str, Any]) -> dict[str, Any]:
        """A new grant and its one-time secret token (only the hash is stored)."""
        validate_project_id(project_id)
        if not isinstance(user, dict) or not isinstance(user.get("sub"), str) or not user["sub"]:
            raise GrantError("A signed-in user is required to grant working-tree sync.")
        stored_user = {key: user[key] for key in ("sub", "email", "name")
                       if isinstance(user.get(key), str) and len(user[key]) <= 512}
        grant_id, token = "wtg_" + uuid4().hex, secrets.token_urlsafe(32)
        with self.registry._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT count(*) FROM working_tree_grants WHERE project_id=? AND revoked_at IS NULL",
                                (project_id,)).fetchone()[0]
            if active >= MAX_GRANTS_PER_PROJECT:
                raise GrantError("This project has too many active working-tree grants. Revoke unused ones first.")
            db.execute("INSERT INTO working_tree_grants(grant_id,project_id,user_json,token_sha256) VALUES(?,?,?,?)",
                       (grant_id, project_id, json.dumps(stored_user), _hash(token)))
        return {**self.get(grant_id), "token": token}

    def get(self, grant_id: str) -> dict[str, Any] | None:
        if not isinstance(grant_id, str) or not _GRANT_ID.fullmatch(grant_id):
            return None
        with self.registry._connect() as db:
            row = db.execute("SELECT * FROM working_tree_grants WHERE grant_id=?", (grant_id,)).fetchone()
        return _public(row) if row else None

    def list(self, project_id: str) -> list[dict[str, Any]]:
        validate_project_id(project_id)
        with self.registry._connect() as db:
            rows = db.execute("SELECT * FROM working_tree_grants WHERE project_id=? ORDER BY created_at DESC",
                              (project_id,)).fetchall()
        return [_public(row) for row in rows]

    def revoke(self, project_id: str, grant_id: str) -> bool:
        validate_project_id(project_id)
        with self.registry._connect() as db:
            changed = db.execute("""UPDATE working_tree_grants SET revoked_at=CURRENT_TIMESTAMP
                WHERE grant_id=? AND project_id=? AND revoked_at IS NULL""", (grant_id, project_id)).rowcount
        return bool(changed)

    def verify(self, grant_id: str, token: str) -> dict[str, Any] | None:
        """The active grant with its full stored user, or None for any mismatch."""
        if not isinstance(token, str) or not 20 <= len(token) <= 200 or not isinstance(grant_id, str) \
                or not _GRANT_ID.fullmatch(grant_id):
            return None
        with self.registry._connect() as db:
            row = db.execute("SELECT * FROM working_tree_grants WHERE grant_id=?", (grant_id,)).fetchone()
        if row is None or row["revoked_at"] or not hmac.compare_digest(row["token_sha256"], _hash(token)):
            return None
        return {**_public(row), "user": json.loads(row["user_json"])}

    def record_upload(self, grant_id: str, status: str, revision_id: str | None) -> None:
        with self.registry._connect() as db:
            db.execute("""UPDATE working_tree_grants SET last_upload_at=CURRENT_TIMESTAMP, last_status=?,
                last_revision_id=COALESCE(?, last_revision_id) WHERE grant_id=?""", (status[:64], revision_id, grant_id))


def validate_base_commit(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        raise GrantError("The base commit must be a full lowercase Git SHA.")
    return value
