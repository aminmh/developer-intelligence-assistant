"""SQLite concept store for the domain-concept recognition layer.

Each row is one **concept** (or alias/source/relationship/index-state row) for
a project. The DB is a dedicated canonical file independent of the analysis
store:

    DOMAIN_DB_PATH  (default ``/app/data/domain_concepts.db``)

Conventions:
  * per-operation connections, each guarded by one ``threading.RLock`` and
    closed in a ``finally`` (short transactions; nothing held between calls),
  * every connection applies ``journal_mode=WAL``, ``foreign_keys=ON`` and
    ``busy_timeout=10000`` before any work,
  * schema migrations are idempotent ``PRAGMA user_version`` steps,
  * row ids are app-generated ``uuid4`` hex strings; timestamps are UTC ISO
    strings, never derived from the clock of the connection layer twice.

Public write methods are total: lookups that cannot resolve (missing concept,
cross-project relationship) return ``None``/``False``/empty instead of raising.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from projects.domain.normalizer import ConceptNormalizer
from projects.domain.types import (
    DomainAlias,
    DomainConcept,
    DomainExtraction,
    DomainRelationship,
    DomainSource,
)

DEFAULT_DB_PATH = "/app/data/domain_concepts.db"
DB_PATH = os.getenv("DOMAIN_DB_PATH", DEFAULT_DB_PATH)

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS concepts (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    identity_key    TEXT NOT NULL,
    concept_type    TEXT NOT NULL,
    canonical_name  TEXT NOT NULL,
    qualified_name  TEXT,
    normalized_name TEXT NOT NULL,
    description     TEXT,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(project_id, identity_key)
);

CREATE TABLE IF NOT EXISTS concept_aliases (
    id               TEXT PRIMARY KEY,
    concept_id       TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    alias            TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    alias_type       TEXT NOT NULL DEFAULT 'generated',
    confidence       REAL NOT NULL DEFAULT 0.5,
    provenance       TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE(concept_id, alias)
);

CREATE TABLE IF NOT EXISTS concept_sources (
    id                TEXT PRIMARY KEY,
    concept_id        TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    source_file       TEXT NOT NULL,
    source_hash       TEXT NOT NULL,
    start_line        INTEGER,
    end_line          INTEGER,
    extraction_method TEXT NOT NULL,
    confidence        REAL NOT NULL DEFAULT 1.0,
    metadata          TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS concept_relationships (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL,
    relation_type     TEXT NOT NULL,
    source_concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    target_concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    confidence        REAL NOT NULL DEFAULT 1.0,
    metadata          TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE(project_id, relation_type, source_concept_id, target_concept_id)
);

CREATE TABLE IF NOT EXISTS index_state (
    concept_id  TEXT PRIMARY KEY REFERENCES concepts(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_concepts_project
    ON concepts(project_id);
CREATE INDEX IF NOT EXISTS idx_concepts_project_norm
    ON concepts(project_id, normalized_name);
CREATE INDEX IF NOT EXISTS idx_concept_aliases_concept
    ON concept_aliases(concept_id);
CREATE INDEX IF NOT EXISTS idx_concept_aliases_norm
    ON concept_aliases(normalized_alias);
CREATE INDEX IF NOT EXISTS idx_concept_relationships_project
    ON concept_relationships(project_id);
"""

_SCHEMA_STEPS: dict[int, str] = {1: _SCHEMA_V1}
_MAX_VERSION = max(_SCHEMA_STEPS)

_NORMALIZE = ConceptNormalizer().normalize


# --- module helpers ----------------------------------------------------------


def _new_id() -> str:
    return uuid.uuid4().hex


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _decode_json(raw: Any) -> dict[str, Any]:
    """Expand a stored JSON column; corrupt/None values degrade to {}."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _like_literal(pattern: str) -> str:
    """Escape a LIKE pattern so it matches literally (no wildcards)."""
    return pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ConceptStore:
    """Thread-safe, transactional SQLite store for domain concepts."""

    def __init__(self, db_path: str | None = None) -> None:
        # Construction is side-effect free: no DB is opened until first use.
        self._db_path = db_path or DB_PATH
        self._lock = threading.RLock()

    # -- connection + migration ---------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for step in range(version + 1, _MAX_VERSION + 1):
            sql = _SCHEMA_STEPS.get(step)
            if sql:
                conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {step}")
            conn.commit()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """One short, locked transaction with a dedicated connection."""
        with self._lock:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _resolve_concept_id(
        conn: sqlite3.Connection, project_id: str, identity_key: str
    ) -> str | None:
        row = conn.execute(
            "SELECT id FROM concepts WHERE project_id=? AND identity_key=?",
            (project_id, identity_key),
        ).fetchone()
        return str(row["id"]) if row else None

    # -- row decoding --------------------------------------------------------

    @staticmethod
    def _concept_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        metadata = _decode_json(d.get("metadata"))
        d["metadata"] = metadata
        d["roles"] = list(metadata.get("roles") or [])
        return d

    @staticmethod
    def _alias_dict(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _source_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["metadata"] = _decode_json(d.get("metadata"))
        return d

    @staticmethod
    def _relationship_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["metadata"] = _decode_json(d.get("metadata"))
        return d

    # -- concepts ------------------------------------------------------------

    def _upsert_concept_on(
        self, conn: sqlite3.Connection, project_id: str, concept: DomainConcept
    ) -> dict[str, Any]:
        """Insert or refresh one concept row on an open connection."""
        metadata = dict(concept.metadata or {})
        metadata["roles"] = list(concept.roles)
        now = _now_iso()
        concept_id = _new_id()
        conn.execute(
            """
            INSERT INTO concepts (
                id, project_id, identity_key, concept_type, canonical_name,
                qualified_name, normalized_name, description, metadata,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, identity_key) DO UPDATE SET
                concept_type = excluded.concept_type,
                canonical_name = excluded.canonical_name,
                qualified_name = excluded.qualified_name,
                normalized_name = excluded.normalized_name,
                description = excluded.description,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                concept_id,
                project_id,
                concept.identity_key,
                concept.concept_type,
                concept.canonical_name,
                concept.qualified_name,
                concept.normalized_name,
                concept.description,
                _dump_json(metadata),
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM concepts WHERE project_id=? AND identity_key=?",
            (project_id, concept.identity_key),
        ).fetchone()
        return self._concept_dict(row)

    def upsert_concept(
        self, project_id: str, concept: DomainConcept
    ) -> dict[str, Any]:
        """Insert or refresh one concept row.

        On conflict ``created_at`` is preserved; classification content is
        replaced by the freshest extraction. ``roles`` live inside the
        ``metadata`` JSON column. Returns the resulting concept row dict.
        """
        with self._transaction() as conn:
            return self._upsert_concept_on(conn, project_id, concept)

    def find_by_identity(
        self, project_id: str, identity_key: str
    ) -> dict[str, Any] | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM concepts WHERE project_id=? AND identity_key=?",
                (project_id, identity_key),
            ).fetchone()
            return self._concept_dict(row) if row else None

    def find_by_id(self, project_id: str, concept_id: str) -> dict[str, Any] | None:
        """Find one canonical concept by its project-scoped row id."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM concepts WHERE project_id=? AND id=?",
                (project_id, concept_id),
            ).fetchone()
            return self._concept_dict(row) if row else None

    def find_by_identity_prefix(
        self, project_id: str, prefix: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        pattern = _like_literal(prefix) + "%"
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM concepts "
                "WHERE project_id=? AND identity_key LIKE ? ESCAPE '\\' "
                "ORDER BY identity_key ASC LIMIT ?",
                (project_id, pattern, limit),
            ).fetchall()
            return [self._concept_dict(r) for r in rows]

    def exact_matches(
        self, project_id: str, normalized_name: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM concepts "
                "WHERE project_id=? AND normalized_name=? "
                "ORDER BY identity_key ASC LIMIT ?",
                (project_id, normalized_name, limit),
            ).fetchall()
            return [self._concept_dict(r) for r in rows]

    def alias_matches(
        self, project_id: str, queried_alias: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Concepts whose stored alias normalizes to the queried alias.

        Lookup keeps the store's normalized-equality semantics: the queried
        alias is normalized and matched against each alias's
        ``normalized_alias`` key, so every concept the previous normalized
        query found is still found. Each returned concept row additionally
        carries ``matched_alias`` = the stored spelling of a matching alias
        that equals the queried alias exactly (case-insensitive). When only a
        normalized-equivalent spelling matched, the key is absent so the caller
        can distinguish an exact alias hit from a merely normalized one.
        """
        limit = max(1, min(int(limit), 1000))
        target = _NORMALIZE(queried_alias)
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT c.*, (
                    SELECT a2.alias FROM concept_aliases a2
                    WHERE a2.concept_id = c.id
                      AND a2.normalized_alias = ?
                      AND a2.alias COLLATE NOCASE = ?
                    ORDER BY a2.alias ASC
                    LIMIT 1
                ) AS matched_alias
                FROM concepts c
                WHERE c.project_id = ?
                  AND EXISTS (
                      SELECT 1 FROM concept_aliases a
                      WHERE a.concept_id = c.id AND a.normalized_alias = ?
                  )
                ORDER BY c.identity_key ASC LIMIT ?
                """,
                (target, queried_alias, project_id, target, limit),
            ).fetchall()
            results = [self._concept_dict(r) for r in rows]
            for row in results:
                if row.get("matched_alias") is None:
                    row.pop("matched_alias", None)
            return results

    def list_concepts(
        self,
        project_id: str,
        limit: int = 1000,
        *,
        search: str | None = None,
        concept_type: str | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List canonical concepts with bounded, deterministic filtering.

        The optional filters are intentionally read-only query concerns. The
        legacy call shape remains unchanged for index/reconcile callers.
        """
        limit = max(1, min(int(limit), 10000))
        offset = max(0, int(offset))
        clauses = ["project_id=?"]
        params: list[Any] = [project_id]
        if search and str(search).strip():
            needle = f"%{_like_literal(str(search).strip())}%"
            clauses.append(
                "(canonical_name LIKE ? ESCAPE '\\' "
                "OR qualified_name LIKE ? ESCAPE '\\' "
                "OR normalized_name LIKE ? ESCAPE '\\' "
                "OR identity_key LIKE ? ESCAPE '\\')"
            )
            params.extend([needle] * 4)
        if concept_type and str(concept_type).strip():
            clauses.append("concept_type=?")
            params.append(str(concept_type).strip())
        params.extend([limit, offset])
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM concepts WHERE "
                + " AND ".join(clauses)
                + " ORDER BY identity_key ASC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
            return [self._concept_dict(r) for r in rows]

    def count_concepts(
        self,
        project_id: str,
        *,
        search: str | None = None,
        concept_type: str | None = None,
    ) -> int:
        """Count concepts using the same filters as :meth:`list_concepts`."""
        clauses = ["project_id=?"]
        params: list[Any] = [project_id]
        if search and str(search).strip():
            needle = f"%{_like_literal(str(search).strip())}%"
            clauses.append(
                "(canonical_name LIKE ? ESCAPE '\\' "
                "OR qualified_name LIKE ? ESCAPE '\\' "
                "OR normalized_name LIKE ? ESCAPE '\\' "
                "OR identity_key LIKE ? ESCAPE '\\')"
            )
            params.extend([needle] * 4)
        if concept_type and str(concept_type).strip():
            clauses.append("concept_type=?")
            params.append(str(concept_type).strip())
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM concepts WHERE " + " AND ".join(clauses),
                params,
            ).fetchone()
            return int(row[0] or 0)

    def aliases_for_concept(self, concept_id: str, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM concept_aliases WHERE concept_id=? ORDER BY alias ASC LIMIT ?",
                (concept_id, limit),
            ).fetchall()
            return [self._alias_dict(row) for row in rows]

    def remove_concept(self, project_id: str, identity_key: str) -> bool:
        with self._transaction() as conn:
            cur = conn.execute(
                "DELETE FROM concepts WHERE project_id=? AND identity_key=?",
                (project_id, identity_key),
            )
            return cur.rowcount > 0

    def remove_concepts_not_in(
        self,
        project_id: str,
        keep_identity_keys: set[str] | list[str] | None,
    ) -> int:
        """Remove stored concepts whose identity_key is not in the keep set.

        Reconcile helper for a full rebuild: batch, project-scoped removal of
        every concept the fresh extraction no longer knows. Relationships
        touching a dropped concept are deleted first; aliases, sources and
        index-state rows cascade with their concept. Never raises (a store
        failure removes nothing and returns 0) and returns the number of
        concepts removed.
        """
        keep = {str(key) for key in (keep_identity_keys or [])}
        try:
            with self._transaction() as conn:
                rows = conn.execute(
                    "SELECT id, identity_key FROM concepts WHERE project_id=?",
                    (project_id,),
                ).fetchall()
                drop_ids = [
                    str(row["id"])
                    for row in rows
                    if str(row["identity_key"]) not in keep
                ]
                if not drop_ids:
                    return 0
                placeholders = ",".join("?" * len(drop_ids))
                conn.execute(
                    "DELETE FROM concept_relationships "
                    f"WHERE source_concept_id IN ({placeholders}) "
                    f"OR target_concept_id IN ({placeholders})",
                    drop_ids * 2,
                )
                cur = conn.execute(
                    f"DELETE FROM concepts WHERE id IN ({placeholders})",
                    drop_ids,
                )
                return int(cur.rowcount)
        except Exception:  # noqa: BLE001 — reconcile helper stays total
            return 0

    # -- aliases + sources (replace semantics) --------------------------------

    def _replace_aliases_on(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        identity_key: str,
        aliases: list[DomainAlias],
    ) -> None:
        """Replace an alias set on an open connection (delete + insert)."""
        concept_id = self._resolve_concept_id(conn, project_id, identity_key)
        if concept_id is None:
            return
        conn.execute(
            "DELETE FROM concept_aliases WHERE concept_id=?", (concept_id,)
        )
        now = _now_iso()
        rows = [
            (
                _new_id(),
                concept_id,
                alias.alias,
                _NORMALIZE(alias.alias),
                alias.alias_type,
                float(alias.confidence),
                alias.provenance,
                now,
            )
            for alias in aliases
        ]
        if rows:
            conn.executemany(
                """
                INSERT INTO concept_aliases (
                    id, concept_id, alias, normalized_alias, alias_type,
                    confidence, provenance, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def replace_aliases(
        self, project_id: str, identity_key: str, aliases: list[DomainAlias]
    ) -> None:
        """Delete existing aliases then insert the fresh set (one txn)."""
        with self._transaction() as conn:
            self._replace_aliases_on(conn, project_id, identity_key, aliases)

    def _replace_sources_on(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        identity_key: str,
        sources: list[DomainSource],
    ) -> None:
        """Replace a source set on an open connection (delete + insert)."""
        concept_id = self._resolve_concept_id(conn, project_id, identity_key)
        if concept_id is None:
            return
        conn.execute(
            "DELETE FROM concept_sources WHERE concept_id=?", (concept_id,)
        )
        now = _now_iso()
        rows = [
            (
                _new_id(),
                concept_id,
                source.file,
                source.source_hash,
                source.start_line,
                source.end_line,
                source.method,
                float(source.confidence),
                _dump_json(source.metadata or {}),
                now,
            )
            for source in sources
        ]
        if rows:
            conn.executemany(
                """
                INSERT INTO concept_sources (
                    id, concept_id, source_file, source_hash, start_line,
                    end_line, extraction_method, confidence, metadata,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def replace_sources(
        self, project_id: str, identity_key: str, sources: list[DomainSource]
    ) -> None:
        """Delete existing sources then insert the fresh set (one txn)."""
        with self._transaction() as conn:
            self._replace_sources_on(conn, project_id, identity_key, sources)

    def find_sources_for_concept(
        self, project_id: str, identity_key: str
    ) -> list[dict[str, Any]]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT s.* FROM concept_sources s "
                "JOIN concepts c ON c.id = s.concept_id "
                "WHERE c.project_id=? AND c.identity_key=? "
                "ORDER BY s.source_file ASC, s.start_line ASC",
                (project_id, identity_key),
            ).fetchall()
            return [self._source_dict(r) for r in rows]

    # -- relationships --------------------------------------------------------

    def _upsert_relationship_on(
        self, conn: sqlite3.Connection, project_id: str, rel: DomainRelationship
    ) -> bool:
        """Persist one directed relationship on an open connection."""
        source_id = self._resolve_concept_id(conn, project_id, rel.source_identity)
        target_id = self._resolve_concept_id(conn, project_id, rel.target_identity)
        if source_id is None or target_id is None:
            return False
        now = _now_iso()
        conn.execute(
            """
            INSERT INTO concept_relationships (
                id, project_id, relation_type, source_concept_id,
                target_concept_id, confidence, metadata, created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, relation_type, source_concept_id,
                        target_concept_id) DO NOTHING
            """,
            (
                _new_id(),
                project_id,
                rel.relation_type,
                source_id,
                target_id,
                float(rel.confidence),
                _dump_json(rel.metadata or {}),
                now,
                now,
            ),
        )
        return True

    def upsert_relationship(
        self, project_id: str, rel: DomainRelationship
    ) -> bool:
        """Persist a directed relationship between two existing concepts.

        Both endpoints must already exist in the SAME project; otherwise the
        call fails at the boundary and returns ``False`` doing nothing.
        """
        with self._transaction() as conn:
            return self._upsert_relationship_on(conn, project_id, rel)

    def _upsert_concept_bundle(
        self, project_id: str, concept: DomainConcept
    ) -> dict[str, Any]:
        """Upsert one concept plus its aliases and sources in ONE transaction.

        The per-concept trio (concept upsert, alias replace, source replace)
        commits together instead of three separate transactions.
        """
        with self._transaction() as conn:
            row = self._upsert_concept_on(conn, project_id, concept)
            self._replace_aliases_on(
                conn, project_id, concept.identity_key, list(concept.aliases or [])
            )
            self._replace_sources_on(
                conn, project_id, concept.identity_key, list(concept.sources or [])
            )
        return row

    def upsert_extraction(
        self, project_id: str, extraction: DomainExtraction
    ) -> dict[str, Any]:
        """Persist a full extraction: concepts then relationships (best-effort).

        Each concept is upserted with replace semantics for its aliases and
        sources, committed ONCE per concept (concept + aliases + sources in a
        single transaction); the whole relationship batch is committed in one
        further transaction. Relationships are inserted only when both
        endpoints resolved in the project. Total: an unexpected store failure
        returns the partial counts with ``error`` set instead of raising.
        """
        result: dict[str, Any] = {
            "concepts": 0,
            "relationships": 0,
            "error": None,
        }
        concepts = list(getattr(extraction, "concepts", None) or [])
        relationships = list(getattr(extraction, "relationships", None) or [])
        try:
            for concept in concepts:
                identity_key = getattr(concept, "identity_key", None)
                if not identity_key:
                    continue
                self._upsert_concept_bundle(project_id, concept)
                result["concepts"] += 1
            with self._transaction() as conn:
                for rel in relationships:
                    if rel is None:
                        continue
                    if self._upsert_relationship_on(conn, project_id, rel):
                        result["relationships"] += 1
        except Exception as exc:  # noqa: BLE001 — bulk persist stays total
            result["error"] = str(exc)[:300]
        return result

    def relationships_for(
        self,
        project_id: str,
        concept_id: str | None = None,
        relation_type: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Relationship rows; ``concept_id`` is a ``concepts.id`` row id."""
        limit = max(1, min(int(limit), 5000))
        clauses = ["r.project_id=?"]
        params: list[Any] = [project_id]
        if concept_id is not None:
            clauses.append("(r.source_concept_id=? OR r.target_concept_id=?)")
            params.extend([concept_id, concept_id])
        if relation_type is not None:
            clauses.append("r.relation_type=?")
            params.append(relation_type)
        params.append(limit)
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT r.*,
                       sc.identity_key AS source_identity,
                       sc.canonical_name AS source_canonical_name,
                       tc.identity_key AS target_identity,
                       tc.canonical_name AS target_canonical_name
                FROM concept_relationships r
                JOIN concepts sc ON sc.id = r.source_concept_id
                JOIN concepts tc ON tc.id = r.target_concept_id
                WHERE """ + " AND ".join(clauses) + """
                ORDER BY r.rowid ASC LIMIT ?
                """,
                params,
            ).fetchall()
            return [self._relationship_dict(r) for r in rows]

    def related_nodes(
        self,
        project_id: str,
        concept_id: str,
        depth: int = 1,
        node_cap: int = 50,
        edge_cap: int = 200,
    ) -> dict[str, Any]:
        """Bounded graph traversal around one concept (both directions).

        Expands the neighbourhood level by level up to ``depth`` hops. Cycles
        are safe (visited set tracks every node), and depth/node/edge caps keep
        the walk bounded. Returns ``{nodes, edges, truncated}``; each node
        carries its ``identity_key`` and ``distance``, each edge its stored
        direction plus resolved identity keys. ``concept_id`` is a
        ``concepts.id`` row id.
        """
        depth = max(0, int(depth))
        node_cap = max(1, int(node_cap))
        edge_cap = max(1, int(edge_cap))
        result: dict[str, Any] = {"nodes": [], "edges": [], "truncated": False}
        with self._transaction() as conn:
            seed = conn.execute(
                "SELECT * FROM concepts WHERE project_id=? AND id=?",
                (project_id, concept_id),
            ).fetchone()
            if seed is None:
                return result
            start = self._concept_dict(seed)
            start["distance"] = 0
            result["nodes"].append(start)

            visited: set[str] = {concept_id}
            added_edges: set[str] = set()
            frontier = [concept_id]
            remaining_depth = depth

            while frontier and remaining_depth > 0:
                remaining_depth -= 1
                placeholders = ",".join("?" * len(frontier))
                rows = conn.execute(
                    f"""
                    SELECT r.*,
                           sc.identity_key AS source_identity,
                           tc.identity_key AS target_identity
                    FROM concept_relationships r
                    JOIN concepts sc ON sc.id = r.source_concept_id
                    JOIN concepts tc ON tc.id = r.target_concept_id
                    WHERE r.project_id=? AND (
                        r.source_concept_id IN ({placeholders})
                        OR r.target_concept_id IN ({placeholders})
                    )
                    ORDER BY r.rowid ASC
                    """,
                    (project_id, *frontier, *frontier),
                ).fetchall()

                next_frontier: list[str] = []
                for row in rows:
                    if row["id"] in added_edges:
                        continue
                    if len(result["edges"]) >= edge_cap:
                        result["truncated"] = True
                        next_frontier = []
                        break
                    fresh = [
                        node_id
                        for node_id in (
                            row["source_concept_id"],
                            row["target_concept_id"],
                        )
                        if node_id not in visited
                    ]
                    if fresh:
                        if len(result["nodes"]) + len(fresh) > node_cap:
                            result["truncated"] = True
                            next_frontier = []
                            break
                        distance = depth - remaining_depth
                        for node_id in fresh:
                            node_row = conn.execute(
                                "SELECT * FROM concepts WHERE id=?",
                                (node_id,),
                            ).fetchone()
                            if node_row is None:
                                continue
                            node = self._concept_dict(node_row)
                            node["distance"] = distance
                            result["nodes"].append(node)
                            visited.add(node_id)
                            next_frontier.append(node_id)
                    result["edges"].append(self._relationship_dict(row))
                    added_edges.add(row["id"])
                frontier = next_frontier
                if result["truncated"]:
                    break
            return result

    # -- index state ----------------------------------------------------------

    def mark_index_state(
        self,
        project_id: str,
        identity_key: str,
        content_hash: str,
        status: str = "pending",
        last_error: str | None = None,
    ) -> None:
        """Record/refresh the index-state row for a concept (no-op if absent).

        ``last_error`` carries the reason of the most recent failed attempt and
        is cleared when a later ``done`` mark supersedes it.
        """
        with self._transaction() as conn:
            concept_id = self._resolve_concept_id(conn, project_id, identity_key)
            if concept_id is None:
                return
            conn.execute(
                """
                INSERT INTO index_state (
                    concept_id, content_hash, status, attempts, last_error,
                    updated_at
                ) VALUES (?, ?, ?, 0, ?, ?)
                ON CONFLICT(concept_id) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    status = excluded.status,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (concept_id, content_hash, status, last_error, _now_iso()),
            )

    def index_state_rows(
        self,
        project_id: str | None = None,
        status: str | None = "pending",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Index-state rows; ``status=None`` selects every state for a project."""
        limit = max(1, min(int(limit), 5000))
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("s.status=?")
            params.append(status)
        if project_id is not None:
            clauses.append("c.project_id=?")
            params.append(project_id)
        params.append(limit)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT s.*, c.project_id, c.identity_key
                FROM index_state s
                JOIN concepts c ON c.id = s.concept_id
                """ + where + """
                ORDER BY s.updated_at ASC, s.concept_id ASC LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def clear_index_states(self, project_id: str) -> int:
        with self._transaction() as conn:
            cur = conn.execute(
                "DELETE FROM index_state WHERE concept_id IN ("
                "SELECT id FROM concepts WHERE project_id=?)",
                (project_id,),
            )
            return cur.rowcount

    # -- project-level --------------------------------------------------------

    def delete_project(self, project_id: str) -> int:
        """Delete every concept of a project; aliases/sources/relationships and
        index rows cascade via foreign keys. Returns rows deleted."""
        with self._transaction() as conn:
            cur = conn.execute(
                "DELETE FROM concepts WHERE project_id=?", (project_id,)
            )
            return cur.rowcount

    def counts(self, project_id: str | None = None) -> dict[str, int]:
        """Row counts for concepts, aliases, relationships and pending index."""
        project_clause = "WHERE project_id=?" if project_id is not None else ""
        project_join = "JOIN concepts c ON c.id = a.concept_id" if project_id else ""
        pending_join = (
            "JOIN concepts c ON c.id = s.concept_id" if project_id else ""
        )

        def params(*extra: Any) -> tuple[Any, ...]:
            return (project_id, *extra) if project_id is not None else tuple(extra)

        with self._transaction() as conn:
            concepts = conn.execute(
                f"SELECT COUNT(*) FROM concepts {project_clause}", params()
            ).fetchone()[0]
            aliases = conn.execute(
                f"SELECT COUNT(*) FROM concept_aliases a {project_join} "
                + ("WHERE c.project_id=?" if project_id else ""),
                params(),
            ).fetchone()[0]
            relationships = conn.execute(
                f"SELECT COUNT(*) FROM concept_relationships {project_clause}",
                params(),
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM index_state s "
                + pending_join
                + (" WHERE s.status='pending' AND c.project_id=?"
                   if project_id
                   else " WHERE s.status='pending'"),
                params(),
            ).fetchone()[0]
            return {
                "concepts": int(concepts),
                "aliases": int(aliases),
                "relationships": int(relationships),
                "pending_index": int(pending),
            }


concept_store = ConceptStore()
