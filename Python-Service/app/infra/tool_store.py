"""Persistent registry for developer-managed LLM tools."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from infra.settings_store import settings_store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ToolStore:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or settings_store._db_path
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS llm_tools ("
                "id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL UNIQUE, "
                "description TEXT NOT NULL DEFAULT '', config TEXT NOT NULL, "
                "enabled INTEGER NOT NULL DEFAULT 1, deleted_at TEXT, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "last_status TEXT, last_error TEXT, last_run_at TEXT)"
            )
            conn.commit(); conn.close()

    def list(self, include_deleted: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            where = "" if include_deleted else "WHERE deleted_at IS NULL"
            rows = conn.execute(f"SELECT * FROM llm_tools {where} ORDER BY name").fetchall()
            conn.close()
        return [self._row(r) for r in rows]

    def get(self, tool_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect(); row = conn.execute("SELECT * FROM llm_tools WHERE id=?", (tool_id,)).fetchone(); conn.close()
        return self._row(row) if row else None

    def save(self, payload: dict[str, Any], tool_id: str | None = None) -> dict[str, Any]:
        tool_type = str(payload.get("type") or "").lower()
        if tool_type not in {"shell", "mcp", "rest"}:
            raise ValueError("type must be shell, mcp, or rest")
        name = str(payload.get("name") or "").strip()
        if not name or len(name) > 100:
            raise ValueError("name is required and must be at most 100 characters")
        description = str(payload.get("description") or "").strip()
        config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        now = _now(); ident = tool_id or str(uuid.uuid4())
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO llm_tools(id,type,name,description,config,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET type=excluded.type,name=excluded.name,description=excluded.description,config=excluded.config,enabled=excluded.enabled,updated_at=excluded.updated_at,deleted_at=NULL",
                    (ident, tool_type, name, description, json.dumps(config), 1 if payload.get("enabled", True) else 0, now, now),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get(ident) or {}

    def set_enabled(self, tool_id: str, enabled: bool) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect(); conn.execute("UPDATE llm_tools SET enabled=?,updated_at=? WHERE id=?", (int(enabled), _now(), tool_id)); conn.commit(); conn.close()
        return self.get(tool_id)

    def soft_delete(self, tool_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect(); conn.execute("UPDATE llm_tools SET deleted_at=?,enabled=0,updated_at=? WHERE id=?", (_now(), _now(), tool_id)); conn.commit(); conn.close()
        return self.get(tool_id)

    def record_run(self, tool_id: str, status: str, error: str | None = None) -> None:
        with self._lock:
            conn = self._connect(); conn.execute("UPDATE llm_tools SET last_status=?,last_error=?,last_run_at=? WHERE id=?", (status, (error or "")[:500], _now(), tool_id)); conn.commit(); conn.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["config"] = json.loads(out.get("config") or "{}")
        out["enabled"] = bool(out.get("enabled"))
        return out


tool_store = ToolStore()
