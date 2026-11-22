"""Durable contract sources, versions, and attachments.

Spec: @.archcore/frontend-assist/api-contract-sources-and-versions.spec.md.

A **source** is one configured origin of a contract document and belongs to one
project. Each ingestion is one row in ``contract_versions``: ``valid`` rows are
immutable contract versions; ``failed`` and ``missing`` rows record an attempt
that produced none, and never replace the newest valid version. An
**attachment** links a source to a project as its provider or a consumer.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from api_contracts.model import diff_models

SOURCE_KINDS = ("upload", "repo_path", "url")
ROLES = ("provider", "consumer")
MODES = ("follow_latest", "pinned")
MAX_SOURCES_PER_PROJECT = 50
MAX_ATTACHMENTS_PER_PROJECT = 100
MAX_LISTED_VERSIONS = 100
MAX_LISTED_EVENTS = 200
EVENT_KINDS = ("contract", "code")


class ContractNotFound(LookupError):
    pass


class ContractConflict(ValueError):
    """The change would break something that depends on the contract."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(value: str | None, default: Any = None) -> Any:
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


class ContractStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS contract_sources (
                source_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
                config TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'empty', last_errors TEXT,
                last_checked_at TEXT, created_by TEXT, created_at TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS contract_sources_project ON contract_sources(project_id)")
            db.execute("""CREATE TABLE IF NOT EXISTS contract_versions (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, version_id TEXT NOT NULL UNIQUE, source_id TEXT NOT NULL,
                state TEXT NOT NULL, content_hash TEXT, dialect TEXT, title TEXT, api_version TEXT,
                operation_count INTEGER, schema_count INTEGER, revision_id TEXT, model TEXT, changes TEXT,
                errors TEXT, created_at TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS contract_versions_source ON contract_versions(source_id, seq)")
            # Which contract version (or failure) each back-end revision produced, for repo_path sources.
            db.execute("""CREATE TABLE IF NOT EXISTS contract_revisions (
                source_id TEXT NOT NULL, revision_id TEXT NOT NULL, version_id TEXT, state TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(source_id, revision_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS contract_attachments (
                attachment_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, project_id TEXT NOT NULL,
                role TEXT NOT NULL, mode TEXT NOT NULL, pinned_version_id TEXT, created_by TEXT,
                created_at TEXT NOT NULL, UNIQUE(source_id, project_id, role))""")
            db.execute("CREATE INDEX IF NOT EXISTS contract_attachments_project ON contract_attachments(project_id)")
            # What changed for consumers: a new contract version, or provider code that moved without one.
            # Spec: @.archcore/frontend-assist/contract-change-notifications.spec.md.
            db.execute("""CREATE TABLE IF NOT EXISTS contract_change_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, source_id TEXT NOT NULL,
                provider_project_id TEXT NOT NULL, kind TEXT NOT NULL, from_version_id TEXT, to_version_id TEXT,
                from_revision_id TEXT, to_revision_id TEXT, max_severity TEXT, summary TEXT NOT NULL,
                items TEXT NOT NULL, created_at TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS contract_change_events_source ON contract_change_events(source_id, seq)")
            db.execute("""CREATE TABLE IF NOT EXISTS contract_change_acks (
                event_id TEXT NOT NULL, user_id TEXT NOT NULL, acked_at TEXT NOT NULL, PRIMARY KEY(event_id, user_id))""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.root / "contracts.sqlite", timeout=30)
        db.row_factory = sqlite3.Row
        return db

    # ── sources ──────────────────────────────────────────────────────────────

    @staticmethod
    def _source(row: sqlite3.Row) -> dict[str, Any]:
        return {"source_id": row["source_id"], "project_id": row["project_id"], "kind": row["kind"],
                "name": row["name"], "config": _load(row["config"], {}), "status": row["status"],
                "last_errors": _load(row["last_errors"], []), "last_checked_at": row["last_checked_at"],
                "created_by": row["created_by"], "created_at": row["created_at"]}

    def create_source(self, project_id: str, kind: str, name: str, config: dict[str, Any],
                      created_by: str | None = None) -> dict[str, Any]:
        if kind not in SOURCE_KINDS:
            raise ValueError("kind must be upload, repo_path, or url.")
        source_id = "cs_" + uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM contract_sources WHERE project_id=?", (project_id,)).fetchone()[0]
            if count >= MAX_SOURCES_PER_PROJECT:
                raise ContractConflict("This project already has the maximum number of contract sources.")
            db.execute("""INSERT INTO contract_sources(source_id, project_id, kind, name, config, created_by, created_at)
                          VALUES (?, ?, ?, ?, ?, ?, ?)""",
                       (source_id, project_id, kind, name, json.dumps(config, sort_keys=True), created_by, _now()))
        return self.get_source(source_id)

    def get_source(self, source_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM contract_sources WHERE source_id=?", (source_id,)).fetchone()
        if row is None:
            raise ContractNotFound("No such contract source.")
        return self._source(row)

    def list_sources(self, project_id: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        clauses, values = [], []
        for column, value in (("project_id", project_id), ("kind", kind)):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as db:
            rows = db.execute(f"SELECT * FROM contract_sources {where} ORDER BY created_at, source_id", values).fetchall()
        return [self._source(row) for row in rows]

    def delete_source(self, source_id: str) -> None:
        """Remove a source with its versions; refused while another project is attached to it."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            source = db.execute("SELECT project_id FROM contract_sources WHERE source_id=?", (source_id,)).fetchone()
            if source is None:
                raise ContractNotFound("No such contract source.")
            others = db.execute("SELECT DISTINCT project_id FROM contract_attachments WHERE source_id=? AND project_id!=?",
                                (source_id, source["project_id"])).fetchall()
            if others:
                raise ContractConflict("Other projects are attached to this contract: "
                                       + ", ".join(sorted(row["project_id"] for row in others)) + ". Detach them first.")
            db.execute("""DELETE FROM contract_change_acks WHERE event_id IN
                          (SELECT event_id FROM contract_change_events WHERE source_id=?)""", (source_id,))
            for table in ("contract_change_events", "contract_attachments", "contract_revisions", "contract_versions",
                          "contract_sources"):
                db.execute(f"DELETE FROM {table} WHERE source_id=?", (source_id,))

    # ── versions ─────────────────────────────────────────────────────────────

    @staticmethod
    def _version(row: sqlite3.Row, *, with_model: bool = False) -> dict[str, Any]:
        result = {"version_id": row["version_id"], "source_id": row["source_id"], "state": row["state"],
                  "content_hash": row["content_hash"], "dialect": row["dialect"], "title": row["title"],
                  "api_version": row["api_version"], "operation_count": row["operation_count"],
                  "schema_count": row["schema_count"], "revision_id": row["revision_id"],
                  "changes": _load(row["changes"]), "errors": _load(row["errors"], []), "created_at": row["created_at"]}
        if with_model:
            result["model"] = _load(row["model"])
        return result

    @staticmethod
    def _newest_valid(db: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
        return db.execute("SELECT * FROM contract_versions WHERE source_id=? AND state='valid' ORDER BY seq DESC LIMIT 1",
                          (source_id,)).fetchone()

    def record_valid(self, source_id: str, model: dict[str, Any], content_hash: str,
                     revision_id: str | None = None) -> tuple[dict[str, Any], bool]:
        """Store a validated document. Returns ``(version, created)``.

        An unchanged document reuses the newest valid version instead of creating one.
        """
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM contract_sources WHERE source_id=?", (source_id,)).fetchone() is None:
                raise ContractNotFound("No such contract source.")
            newest = self._newest_valid(db, source_id)
            created = newest is None or newest["content_hash"] != content_hash
            if created:
                version_id = "cv_" + uuid4().hex
                changes = diff_models(_load(newest["model"]) if newest else None, model)
                db.execute("""INSERT INTO contract_versions(version_id, source_id, state, content_hash, dialect, title,
                              api_version, operation_count, schema_count, revision_id, model, changes, created_at)
                              VALUES (?, ?, 'valid', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (version_id, source_id, content_hash, model["dialect"], model["title"], model["api_version"],
                            len(model["operations"]), len(model["schemas"]), revision_id,
                            json.dumps(model, sort_keys=True, separators=(",", ":")), json.dumps(changes), now))
            else:
                version_id = newest["version_id"]
            if revision_id:
                db.execute("""INSERT OR REPLACE INTO contract_revisions(source_id, revision_id, version_id, state, created_at)
                              VALUES (?, ?, ?, 'valid', ?)""", (source_id, revision_id, version_id, now))
            db.execute("UPDATE contract_sources SET status='ok', last_errors=NULL, last_checked_at=? WHERE source_id=?",
                       (now, source_id))
            row = db.execute("SELECT * FROM contract_versions WHERE version_id=?", (version_id,)).fetchone()
        return self._version(row), created

    def record_failure(self, source_id: str, state: str, errors: list[str], revision_id: str | None = None) -> dict[str, Any]:
        """Record an attempt that produced no version: ``failed`` (invalid or unreachable) or ``missing``."""
        if state not in ("failed", "missing"):
            raise ValueError("Invalid attempt state")
        now, version_id = _now(), "cv_" + uuid4().hex
        errors = [str(error)[:500] for error in errors][:50]
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM contract_sources WHERE source_id=?", (source_id,)).fetchone() is None:
                raise ContractNotFound("No such contract source.")
            last = db.execute("SELECT * FROM contract_versions WHERE source_id=? ORDER BY seq DESC LIMIT 1",
                              (source_id,)).fetchone()
            if last is not None and last["state"] == state and _load(last["errors"], []) == errors:
                # The same failure again (a scheduled refresh of a broken URL): keep one row, not one per attempt.
                db.execute("UPDATE contract_versions SET created_at=?, revision_id=? WHERE version_id=?",
                           (now, revision_id, last["version_id"]))
                version_id = last["version_id"]
            else:
                db.execute("""INSERT INTO contract_versions(version_id, source_id, state, revision_id, errors, created_at)
                              VALUES (?, ?, ?, ?, ?, ?)""",
                           (version_id, source_id, state, revision_id, json.dumps(errors), now))
            if revision_id:
                db.execute("""INSERT OR REPLACE INTO contract_revisions(source_id, revision_id, version_id, state, created_at)
                              VALUES (?, ?, NULL, ?, ?)""", (source_id, revision_id, state, now))
            db.execute("UPDATE contract_sources SET status=?, last_errors=?, last_checked_at=? WHERE source_id=?",
                       (state, json.dumps(errors), now, source_id))
            row = db.execute("SELECT * FROM contract_versions WHERE version_id=?", (version_id,)).fetchone()
        return self._version(row)

    def list_versions(self, source_id: str) -> list[dict[str, Any]]:
        """Newest first, valid versions and failed attempts together, without models."""
        with self._connect() as db:
            rows = db.execute("SELECT * FROM contract_versions WHERE source_id=? ORDER BY seq DESC LIMIT ?",
                              (source_id, MAX_LISTED_VERSIONS)).fetchall()
        return [self._version(row) for row in rows]

    def get_version(self, version_id: str, *, with_model: bool = True) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM contract_versions WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise ContractNotFound("No such contract version.")
        return self._version(row, with_model=with_model)

    def newest_valid_version(self, source_id: str, *, with_model: bool = False) -> dict[str, Any] | None:
        with self._connect() as db:
            row = self._newest_valid(db, source_id)
        return self._version(row, with_model=with_model) if row else None

    def version_for_revision(self, source_id: str, revision_id: str) -> dict[str, Any] | None:
        """What one back-end revision produced: ``{"state", "version_id"}``, or None when never ingested."""
        with self._connect() as db:
            row = db.execute("SELECT version_id, state FROM contract_revisions WHERE source_id=? AND revision_id=?",
                             (source_id, revision_id)).fetchone()
        return {"state": row["state"], "version_id": row["version_id"]} if row else None

    def delete_version(self, source_id: str, version_id: str) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM contract_versions WHERE version_id=? AND source_id=?",
                             (version_id, source_id)).fetchone()
            if row is None:
                raise ContractNotFound("No such contract version.")
            pins = db.execute("SELECT project_id, role FROM contract_attachments WHERE pinned_version_id=?",
                              (version_id,)).fetchall()
            if pins:
                raise ContractConflict("This version is pinned by: "
                                       + ", ".join(sorted(f"{pin['project_id']} ({pin['role']})" for pin in pins))
                                       + ". Change those attachments first.")
            db.execute("DELETE FROM contract_versions WHERE version_id=?", (version_id,))
            db.execute("DELETE FROM contract_revisions WHERE version_id=?", (version_id,))

    # ── attachments ──────────────────────────────────────────────────────────

    @staticmethod
    def _attachment(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in ("attachment_id", "source_id", "project_id", "role", "mode",
                                          "pinned_version_id", "created_by", "created_at")}

    @staticmethod
    def _check_pin(db: sqlite3.Connection, source_id: str, mode: str, pinned_version_id: str | None) -> str | None:
        if mode not in MODES:
            raise ValueError("mode must be follow_latest or pinned.")
        if mode == "follow_latest":
            return None
        row = db.execute("SELECT state FROM contract_versions WHERE version_id=? AND source_id=?",
                         (pinned_version_id or "", source_id)).fetchone()
        if row is None or row["state"] != "valid":
            raise ValueError("A pinned attachment needs a valid version of this contract.")
        return pinned_version_id

    def attach(self, source_id: str, project_id: str, role: str, mode: str = "follow_latest",
               pinned_version_id: str | None = None, created_by: str | None = None) -> dict[str, Any]:
        if role not in ROLES:
            raise ValueError("role must be provider or consumer.")
        attachment_id = "ca_" + uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM contract_sources WHERE source_id=?", (source_id,)).fetchone() is None:
                raise ContractNotFound("No such contract source.")
            pin = self._check_pin(db, source_id, mode, pinned_version_id)
            if db.execute("SELECT 1 FROM contract_attachments WHERE source_id=? AND project_id=? AND role=?",
                          (source_id, project_id, role)).fetchone():
                raise ContractConflict("This project is already attached to the contract in that role.")
            count = db.execute("SELECT COUNT(*) FROM contract_attachments WHERE project_id=?", (project_id,)).fetchone()[0]
            if count >= MAX_ATTACHMENTS_PER_PROJECT:
                raise ContractConflict("This project already has the maximum number of contract attachments.")
            db.execute("""INSERT INTO contract_attachments(attachment_id, source_id, project_id, role, mode,
                          pinned_version_id, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                       (attachment_id, source_id, project_id, role, mode, pin, created_by, _now()))
        return self.get_attachment(attachment_id)

    def get_attachment(self, attachment_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM contract_attachments WHERE attachment_id=?", (attachment_id,)).fetchone()
        if row is None:
            raise ContractNotFound("No such contract attachment.")
        return self._attachment(row)

    def update_attachment(self, attachment_id: str, mode: str, pinned_version_id: str | None = None) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT source_id FROM contract_attachments WHERE attachment_id=?", (attachment_id,)).fetchone()
            if row is None:
                raise ContractNotFound("No such contract attachment.")
            pin = self._check_pin(db, row["source_id"], mode, pinned_version_id)
            db.execute("UPDATE contract_attachments SET mode=?, pinned_version_id=? WHERE attachment_id=?",
                       (mode, pin, attachment_id))
        return self.get_attachment(attachment_id)

    def detach(self, attachment_id: str) -> None:
        with self._connect() as db:
            if db.execute("DELETE FROM contract_attachments WHERE attachment_id=?", (attachment_id,)).rowcount == 0:
                raise ContractNotFound("No such contract attachment.")

    def list_attachments(self, *, project_id: str | None = None, source_id: str | None = None) -> list[dict[str, Any]]:
        clauses, values = [], []
        for column, value in (("project_id", project_id), ("source_id", source_id)):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as db:
            rows = db.execute(f"SELECT * FROM contract_attachments {where} ORDER BY created_at, attachment_id",
                              values).fetchall()
        return [self._attachment(row) for row in rows]

    def resolve(self, attachment: dict[str, Any], *, with_model: bool = False) -> dict[str, Any] | None:
        """The version an attachment reads: its pin, or the source's newest valid version. None when unavailable."""
        if attachment["mode"] == "pinned":
            try:
                version = self.get_version(attachment["pinned_version_id"] or "", with_model=with_model)
            except ContractNotFound:
                return None
            return version if version["state"] == "valid" else None
        return self.newest_valid_version(attachment["source_id"], with_model=with_model)

    # ── change events ────────────────────────────────────────────────────────

    @staticmethod
    def _event(row: sqlite3.Row, *, with_items: bool = True) -> dict[str, Any]:
        result = {key: row[key] for key in ("seq", "event_id", "source_id", "provider_project_id", "kind",
                                            "from_version_id", "to_version_id", "from_revision_id", "to_revision_id",
                                            "max_severity", "created_at")}
        result["summary"] = _load(row["summary"], {})
        if with_items:
            result["items"] = _load(row["items"], [])
        return result

    def record_event(self, source_id: str, provider_project_id: str, kind: str, items: list[dict[str, Any]], *,
                     max_severity: str | None, summary: dict[str, int], from_version_id: str | None = None,
                     to_version_id: str | None = None, from_revision_id: str | None = None,
                     to_revision_id: str | None = None) -> dict[str, Any]:
        if kind not in EVENT_KINDS:
            raise ValueError("kind must be contract or code.")
        event_id = "ce_" + uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO contract_change_events(event_id, source_id, provider_project_id, kind,
                          from_version_id, to_version_id, from_revision_id, to_revision_id, max_severity, summary,
                          items, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (event_id, source_id, provider_project_id, kind, from_version_id, to_version_id,
                        from_revision_id, to_revision_id, max_severity, json.dumps(summary), json.dumps(items), _now()))
            row = db.execute("SELECT * FROM contract_change_events WHERE event_id=?", (event_id,)).fetchone()
        return self._event(row)

    def get_event(self, event_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM contract_change_events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise ContractNotFound("No such contract change.")
        return self._event(row)

    def list_events(self, source_ids: list[str], *, after_seq: int = 0, limit: int = MAX_LISTED_EVENTS,
                    with_items: bool = False) -> list[dict[str, Any]]:
        """Events of the given sources with ``seq`` above ``after_seq``, newest first."""
        if not source_ids:
            return []
        marks = ",".join("?" for _ in source_ids)
        with self._connect() as db:
            rows = db.execute(f"""SELECT * FROM contract_change_events WHERE source_id IN ({marks}) AND seq>?
                                  ORDER BY seq DESC LIMIT ?""",
                              (*source_ids, after_seq, max(1, min(limit, MAX_LISTED_EVENTS)))).fetchall()
        return [self._event(row, with_items=with_items) for row in rows]

    def acknowledge(self, event_id: str, user_id: str) -> None:
        with self._connect() as db:
            if db.execute("SELECT 1 FROM contract_change_events WHERE event_id=?", (event_id,)).fetchone() is None:
                raise ContractNotFound("No such contract change.")
            db.execute("INSERT OR REPLACE INTO contract_change_acks(event_id, user_id, acked_at) VALUES (?, ?, ?)",
                       (event_id, user_id, _now()))

    def acknowledged(self, user_id: str, event_ids: list[str]) -> set[str]:
        if not event_ids:
            return set()
        marks = ",".join("?" for _ in event_ids)
        with self._connect() as db:
            rows = db.execute(f"SELECT event_id FROM contract_change_acks WHERE user_id=? AND event_id IN ({marks})",
                              (user_id, *event_ids)).fetchall()
        return {row["event_id"] for row in rows}


def default_store() -> ContractStore:
    from infra.settings import settings

    root = os.environ.get("TELEMETRY_SNAPSHOT_ROOT") or str(Path(settings.workspace_root).parent / "snapshots")
    return ContractStore(root)
