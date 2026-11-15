"""Small, bounded samples of one inventory item. Values that look like secrets are masked."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from data_admin import inventory, paths

_SECRET = re.compile(r"secret|token|password|passwd|api_?key|credential|private|cookie|session", re.I)
_MAX_TEXT = 600


def _clip(value: Any, key: str = "") -> Any:
    if key and _SECRET.search(key):
        return "••••••"
    if isinstance(value, str):
        return value if len(value) <= _MAX_TEXT else value[:_MAX_TEXT] + "…"
    if isinstance(value, dict):
        return {k: _clip(v, str(k)) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        return [_clip(v) for v in list(value)[:20]]
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value


def preview(item: dict[str, Any], limit: int = 8) -> dict[str, Any]:
    store = item["store"]
    if store == "qdrant":
        points, _ = inventory.qdrant_client().scroll(collection_name=item["id"], limit=limit, with_payload=True, with_vectors=False)
        return {"samples": [{"id": str(point.id), "payload": _clip(point.payload or {})} for point in points]}
    if store == "neo4j":
        if item["id"].startswith("collection:"):
            rows = inventory.neo4j_run("MATCH (n) WHERE n.collection=$c RETURN labels(n) AS labels, properties(n) AS props LIMIT $l",
                                       c=item["name"], l=limit)
        elif item["id"].startswith("tagged:"):
            rows = inventory.neo4j_run("MATCH (n) WHERE n.collection IS NULL AND n.project_id=$p AND coalesce(n.revision_id,'')=$r "
                                       "RETURN labels(n) AS labels, properties(n) AS props LIMIT $l",
                                       p=item["project_id"], r=item["revision_id"] or "", l=limit)
        else:
            rows = inventory.neo4j_run("MATCH (n) WHERE n.collection IS NULL AND n.project_id IS NULL "
                                       "RETURN labels(n) AS labels, properties(n) AS props LIMIT $l", l=limit)
        return {"samples": [{"labels": row["labels"], "properties": _clip(row["props"])} for row in rows]}
    if store == "minio":
        return {"samples": [{"key": item["id"], "bytes": item["bytes"], "tags": item["detail"].get("tags", {})}]}
    if store == "sqlite":
        if item["id"].startswith("orphan-rows:"):
            return {"samples": []}
        logical, table = item["id"].split(":", 1)
        path = paths.sqlite_files()[logical]
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10) as db:
            db.row_factory = sqlite3.Row
            if table not in {name for name, _ in inventory.sqlite_tables(path)}:
                return {"samples": []}
            rows = db.execute(f'SELECT * FROM "{table}" LIMIT ?', (limit,)).fetchall()
        return {"samples": [{key: _clip(row[key], key) for key in row.keys()} for row in rows]}
    if store == "files":
        logical, name = item["id"].split("/", 1)
        target = paths.directories()[logical] / name
        if target.is_dir():
            children = sorted(str(p.relative_to(target)) for p in list(target.rglob("*"))[:200] if p.is_file())[:50]
            return {"samples": [{"path": child} for child in children]}
        return {"samples": [{"path": name, "bytes": target.stat().st_size if target.exists() else None}]}
    if store == "clickhouse":
        return {"samples": [{"table": item["detail"].get("table"), "rows": item["count"]}]}
    return {"samples": []}


def is_within(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
