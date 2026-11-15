"""Read-only synced copy of a project's Outline business rules, plus rule <-> code links.

Outline stays the source of truth: nothing here writes to Outline. Assessments still fetch rules
live; this index serves chat, links, diff impact checks, drift flags and runtime health, and every
row carries the time it was synced so the UI can say how fresh it is.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import sqlite3
from typing import Any, Iterable

MAX_SYMBOL_ID_CHARS = 512
LINK_STATES = ("suggested", "confirmed", "rejected", "manual")
USER_LINK_STATES = ("confirmed", "rejected", "manual")
_DOC_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class RuleIndexError(ValueError):
    """A safe, client-actionable rule index error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store_path():
    from code_quality.service import _store_path as assessment_store_path
    return assessment_store_path()


def validate_doc_id(value: Any) -> str:
    if not isinstance(value, str) or not _DOC_ID.fullmatch(value):
        raise RuleIndexError("Business rule identity is invalid.")
    return value


def validate_symbol_id(value: Any) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > MAX_SYMBOL_ID_CHARS
            or any(ord(char) < 32 for char in value)):
        raise RuleIndexError("Code symbol identity is invalid.")
    return value.strip()


class RuleIndex:
    def __init__(self, path=None):
        self.path = path or _store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS business_rule_documents (
                project_id TEXT NOT NULL, doc_id TEXT NOT NULL, url_id TEXT NOT NULL,
                title TEXT NOT NULL, text TEXT NOT NULL, revision INTEGER NOT NULL,
                content_hash TEXT NOT NULL, outline_updated_at TEXT NOT NULL,
                synced_at TEXT NOT NULL, deleted_at TEXT,
                PRIMARY KEY(project_id, doc_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS business_rule_sync_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, doc_id TEXT NOT NULL,
                kind TEXT NOT NULL, old_revision INTEGER, new_revision INTEGER, at TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS business_rule_sync_events_project "
                       "ON business_rule_sync_events(project_id, id)")
            db.execute("""CREATE TABLE IF NOT EXISTS business_rule_sync_status (
                project_id TEXT PRIMARY KEY, synced_at TEXT, status TEXT NOT NULL, reason TEXT,
                document_count INTEGER NOT NULL DEFAULT 0, limit_exceeded INTEGER NOT NULL DEFAULT 0,
                oversized_count INTEGER NOT NULL DEFAULT 0, instance_url TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS business_rule_links (
                project_id TEXT NOT NULL, doc_id TEXT NOT NULL, symbol_id TEXT NOT NULL,
                state TEXT NOT NULL, score REAL, file TEXT, line INTEGER, revision_id TEXT,
                rule_revision_at_link INTEGER, code_hash_at_link TEXT,
                latest_code_hash TEXT, latest_revision_id TEXT,
                updated_by TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, doc_id, symbol_id))""")
            db.execute("CREATE INDEX IF NOT EXISTS business_rule_links_symbol "
                       "ON business_rule_links(project_id, symbol_id)")
            db.execute("""CREATE TABLE IF NOT EXISTS business_rule_settings (
                project_id TEXT PRIMARY KEY, allow_remote INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    # ----- sync -------------------------------------------------------------------------------

    def sync(self, project_id: str, rules: list[dict[str, Any]], summary: dict[str, Any] | None = None,
             instance_url: str | None = None) -> dict[str, Any]:
        """Replace the project's copy with a complete fetch; record added, changed and removed documents."""
        now = _now()
        events: list[dict[str, Any]] = []
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = {row["doc_id"]: row for row in db.execute(
                "SELECT * FROM business_rule_documents WHERE project_id=?", (project_id,)).fetchall()}
            seen = set()
            for rule in rules:
                doc_id = rule.get("rule_id")
                if not isinstance(doc_id, str) or not _DOC_ID.fullmatch(doc_id) or doc_id in seen:
                    continue
                seen.add(doc_id)
                revision = int(rule.get("version") or 1)
                previous = existing.get(doc_id)
                if previous is None or previous["deleted_at"] is not None:
                    events.append({"doc_id": doc_id, "kind": "added", "old_revision": None, "new_revision": revision})
                elif previous["content_hash"] != rule.get("content_hash") or previous["revision"] != revision:
                    events.append({"doc_id": doc_id, "kind": "changed",
                                   "old_revision": previous["revision"], "new_revision": revision})
                db.execute("""INSERT INTO business_rule_documents
                    (project_id, doc_id, url_id, title, text, revision, content_hash, outline_updated_at, synced_at, deleted_at)
                    VALUES(?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(project_id, doc_id) DO UPDATE SET
                    url_id=excluded.url_id, title=excluded.title, text=excluded.text, revision=excluded.revision,
                    content_hash=excluded.content_hash, outline_updated_at=excluded.outline_updated_at,
                    synced_at=excluded.synced_at, deleted_at=NULL""",
                           (project_id, doc_id, str(rule.get("url_id") or doc_id)[:64], str(rule.get("title", ""))[:200],
                            str(rule.get("statement", "")), revision, str(rule.get("content_hash", "")),
                            str(rule.get("updated_at", ""))[:80], now))
            for doc_id, row in existing.items():
                if doc_id not in seen and row["deleted_at"] is None:
                    db.execute("UPDATE business_rule_documents SET deleted_at=? WHERE project_id=? AND doc_id=?",
                               (now, project_id, doc_id))
                    events.append({"doc_id": doc_id, "kind": "removed", "old_revision": row["revision"], "new_revision": None})
            for event in events:
                db.execute("""INSERT INTO business_rule_sync_events (project_id, doc_id, kind, old_revision, new_revision, at)
                    VALUES(?,?,?,?,?,?)""", (project_id, event["doc_id"], event["kind"],
                                             event["old_revision"], event["new_revision"], now))
            summary = summary or {}
            db.execute("""INSERT INTO business_rule_sync_status
                (project_id, synced_at, status, reason, document_count, limit_exceeded, oversized_count, instance_url)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET synced_at=excluded.synced_at,
                status=excluded.status, reason=NULL, document_count=excluded.document_count,
                limit_exceeded=excluded.limit_exceeded, oversized_count=excluded.oversized_count,
                instance_url=COALESCE(excluded.instance_url, business_rule_sync_status.instance_url)""",
                       (project_id, now, "synchronized", None, len(seen), int(bool(summary.get("limit_exceeded"))),
                        int(summary.get("oversized_count") or 0), instance_url))
        return {"synced_at": now, "document_count": len(seen), "events": events}

    def record_failure(self, project_id: str, reason: str) -> None:
        """Keep the last synced rows; only note why this attempt failed."""
        with self._connect() as db:
            db.execute("""INSERT INTO business_rule_sync_status (project_id, synced_at, status, reason)
                VALUES(?,NULL,'unavailable',?) ON CONFLICT(project_id) DO UPDATE SET status='unavailable',
                reason=excluded.reason""", (project_id, reason[:500]))

    def status(self, project_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM business_rule_sync_status WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            return {"status": "never_synced", "synced_at": None, "document_count": 0}
        return {"status": row["status"], "synced_at": row["synced_at"], "reason": row["reason"],
                "document_count": row["document_count"], "limit_exceeded": bool(row["limit_exceeded"]),
                "oversized_count": row["oversized_count"]}

    def instance_url(self, project_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT instance_url FROM business_rule_sync_status WHERE project_id=?",
                             (project_id,)).fetchone()
        return row["instance_url"] if row else None

    # ----- documents --------------------------------------------------------------------------

    @staticmethod
    def _document(row: sqlite3.Row, include_text: bool) -> dict[str, Any]:
        result = {"doc_id": row["doc_id"], "url_id": row["url_id"], "url": f"/doc/{row['url_id']}",
                  "title": row["title"], "revision": row["revision"], "content_hash": row["content_hash"],
                  "outline_updated_at": row["outline_updated_at"], "synced_at": row["synced_at"]}
        if include_text:
            result["text"] = row["text"]
        return result

    def documents(self, project_id: str, *, include_text: bool = False) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM business_rule_documents WHERE project_id=? AND deleted_at IS NULL "
                              "ORDER BY title COLLATE NOCASE, doc_id", (project_id,)).fetchall()
        return [self._document(row, include_text) for row in rows]

    def document(self, project_id: str, doc_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM business_rule_documents WHERE project_id=? AND doc_id=? "
                             "AND deleted_at IS NULL", (project_id, doc_id)).fetchone()
        return self._document(row, True) if row else None

    def events(self, project_id: str, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM business_rule_sync_events WHERE project_id=? AND id>? ORDER BY id LIMIT ?",
                              (project_id, after_id, limit)).fetchall()
        return [dict(row) for row in rows]

    # ----- links ------------------------------------------------------------------------------

    def _link(self, row: sqlite3.Row, revisions: dict[str, int]) -> dict[str, Any]:
        reviewed = row["state"] in {"confirmed", "manual"}
        current_revision = revisions.get(row["doc_id"])
        rule_changed = bool(reviewed and current_revision is not None and row["rule_revision_at_link"] is not None
                            and current_revision > row["rule_revision_at_link"])
        code_changed = bool(reviewed and row["code_hash_at_link"] and row["latest_code_hash"]
                            and row["code_hash_at_link"] != row["latest_code_hash"])
        return {"doc_id": row["doc_id"], "symbol_id": row["symbol_id"], "state": row["state"],
                "score": row["score"], "file": row["file"], "line": row["line"], "revision_id": row["revision_id"],
                "rule_revision_at_link": row["rule_revision_at_link"], "rule_changed": rule_changed,
                "code_changed": code_changed, "needs_review": rule_changed or code_changed,
                "updated_by": row["updated_by"], "updated_at": row["updated_at"]}

    def _revisions(self, db, project_id: str) -> dict[str, int]:
        return {row["doc_id"]: row["revision"] for row in db.execute(
            "SELECT doc_id, revision FROM business_rule_documents WHERE project_id=? AND deleted_at IS NULL",
            (project_id,)).fetchall()}

    def links(self, project_id: str, *, doc_ids: Iterable[str] | None = None,
              symbol_ids: Iterable[str] | None = None, states: Iterable[str] | None = None,
              files: Iterable[str] | None = None) -> list[dict[str, Any]]:
        clauses, params = ["l.project_id=?", "d.deleted_at IS NULL"], [project_id]
        for column, values in (("l.doc_id", doc_ids), ("l.symbol_id", symbol_ids), ("l.state", states), ("l.file", files)):
            if values is not None:
                values = list(values)
                if not values:
                    return []
                clauses.append(f"{column} IN ({','.join('?' * len(values))})")
                params.extend(values)
        with self._connect() as db:
            revisions = self._revisions(db, project_id)
            rows = db.execute(f"""SELECT l.* FROM business_rule_links l JOIN business_rule_documents d
                ON d.project_id=l.project_id AND d.doc_id=l.doc_id WHERE {' AND '.join(clauses)}
                ORDER BY l.doc_id, CASE l.state WHEN 'confirmed' THEN 0 WHEN 'manual' THEN 1
                WHEN 'suggested' THEN 2 ELSE 3 END, l.score DESC, l.symbol_id""", params).fetchall()
        return [self._link(row, revisions) for row in rows]

    def add_suggestions(self, project_id: str, doc_id: str, items: list[dict[str, Any]]) -> int:
        """Add or refresh suggested links; never touch confirmed, rejected or manual ones."""
        now, added = _now(), 0
        with self._connect() as db:
            for item in items:
                symbol_id = validate_symbol_id(item.get("symbol_id"))
                result = db.execute("""INSERT INTO business_rule_links
                    (project_id, doc_id, symbol_id, state, score, file, line, revision_id, updated_at)
                    VALUES(?,?,?,'suggested',?,?,?,?,?) ON CONFLICT(project_id, doc_id, symbol_id) DO UPDATE SET
                    score=MAX(COALESCE(business_rule_links.score, 0), COALESCE(excluded.score, 0)),
                    file=excluded.file, line=excluded.line, revision_id=excluded.revision_id,
                    updated_at=excluded.updated_at WHERE business_rule_links.state='suggested'""",
                                    (project_id, doc_id, symbol_id, item.get("score"), item.get("file"),
                                     item.get("line"), item.get("revision_id"), now))
                added += result.rowcount
        return added

    def set_link_state(self, project_id: str, doc_id: str, symbol_id: str, state: str, user_id: str, *,
                       code_hash: str | None = None, file: str | None = None, line: int | None = None,
                       revision_id: str | None = None) -> dict[str, Any]:
        if state not in USER_LINK_STATES:
            raise RuleIndexError("Choose confirmed, rejected or manual for a business rule link.")
        doc_id, symbol_id = validate_doc_id(doc_id), validate_symbol_id(symbol_id)
        document = self.document(project_id, doc_id)
        if document is None:
            raise RuleIndexError("The business rule is not in the synced index. Sync Outline and retry.")
        now = _now()
        with self._connect() as db:
            db.execute("""INSERT INTO business_rule_links
                (project_id, doc_id, symbol_id, state, file, line, revision_id, rule_revision_at_link,
                 code_hash_at_link, latest_code_hash, latest_revision_id, updated_by, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id, doc_id, symbol_id) DO UPDATE SET
                state=excluded.state, file=COALESCE(excluded.file, business_rule_links.file),
                line=COALESCE(excluded.line, business_rule_links.line),
                revision_id=COALESCE(excluded.revision_id, business_rule_links.revision_id),
                rule_revision_at_link=excluded.rule_revision_at_link,
                code_hash_at_link=excluded.code_hash_at_link, latest_code_hash=excluded.latest_code_hash,
                latest_revision_id=COALESCE(excluded.latest_revision_id, business_rule_links.latest_revision_id),
                updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                       (project_id, doc_id, symbol_id, state, file, line, revision_id, document["revision"],
                        code_hash, code_hash, revision_id, user_id[:200], now))
            revisions = self._revisions(db, project_id)
            row = db.execute("SELECT * FROM business_rule_links WHERE project_id=? AND doc_id=? AND symbol_id=?",
                             (project_id, doc_id, symbol_id)).fetchone()
        return self._link(row, revisions)

    def reviewed_symbols(self, project_id: str) -> list[str]:
        with self._connect() as db:
            rows = db.execute("SELECT DISTINCT symbol_id FROM business_rule_links WHERE project_id=? "
                              "AND state IN ('confirmed','manual')", (project_id,)).fetchall()
        return [row["symbol_id"] for row in rows]

    def record_code_hashes(self, project_id: str, revision_id: str, hashes: dict[str, str]) -> int:
        """Store the latest indexed source hash of linked symbols so code drift can be reported."""
        now, updated = _now(), 0
        with self._connect() as db:
            for symbol_id, digest in hashes.items():
                result = db.execute("""UPDATE business_rule_links SET latest_code_hash=?, latest_revision_id=?,
                    updated_at=? WHERE project_id=? AND symbol_id=? AND state IN ('confirmed','manual')""",
                                    (digest, revision_id, now, project_id, symbol_id))
                updated += result.rowcount
        return updated

    # ----- settings ---------------------------------------------------------------------------

    def allow_remote(self, project_id: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT allow_remote FROM business_rule_settings WHERE project_id=?",
                             (project_id,)).fetchone()
        return bool(row and row["allow_remote"])

    def set_allow_remote(self, project_id: str, value: bool) -> bool:
        if type(value) is not bool:
            raise RuleIndexError("allow_remote must be true or false.")
        with self._connect() as db:
            db.execute("""INSERT INTO business_rule_settings (project_id, allow_remote, updated_at) VALUES(?,?,?)
                ON CONFLICT(project_id) DO UPDATE SET allow_remote=excluded.allow_remote, updated_at=excluded.updated_at""",
                       (project_id, int(value), _now()))
        return value
