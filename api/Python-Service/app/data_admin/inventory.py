"""Read-only inventory of every store. One collector per store; a failing store is reported, never fatal.

Every item has the same shape so the UI can render one table for all stores:
``{store, id, name, kind, owner, project_id, revision_id, reason, count, bytes, updated_at, detail, deletable}``.
``deletable`` marks items an administrator may remove on their own; data a revision or project owns is
removed only through revision removal or project purge, so the registry never points at missing data.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from data_admin import paths
from data_admin.ownership import Owners, classify_collection, classify_revision_ref, load_owners

logger = logging.getLogger(__name__)

STORES = ("qdrant", "neo4j", "minio", "clickhouse", "sqlite", "files")
MAX_OBJECTS = 5000
# Raw tus uploads and unregistered workspaces may belong to an upload still in progress.
IN_USE_GRACE_SECONDS = 24 * 3600
_CACHE_TTL_SECONDS = 30
_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


def _item(store: str, item_id: str, name: str, kind: str, ownership: dict[str, Any], *, count: int | None = None,
          size: int | None = None, updated_at: Any = None, detail: dict | None = None,
          deletable: bool | None = None) -> dict[str, Any]:
    owner = ownership["owner"]
    return {
        "store": store, "id": item_id, "name": name, "kind": kind, **ownership,
        "count": count, "bytes": size, "updated_at": updated_at, "detail": detail or {},
        "deletable": owner in ("orphan", "legacy") if deletable is None else deletable,
    }


# --- clients -----------------------------------------------------------------------------------------

def qdrant_client():
    from qdrant_client import QdrantClient

    from infra.settings import settings

    kwargs: dict[str, Any] = {"url": settings.qdrant_url, "timeout": 10}
    if settings.qdrant_api_key:
        kwargs["api_key"] = settings.qdrant_api_key
    return QdrantClient(**kwargs)


def neo4j_driver():
    from neo4j import GraphDatabase

    from infra.settings import settings

    return GraphDatabase.driver(settings.neo4j_url, auth=(settings.neo4j_user, settings.neo4j_password))


def neo4j_run(query: str, **params: Any) -> list[dict[str, Any]]:
    from infra.settings import settings

    driver = neo4j_driver()
    try:
        records, _, _ = driver.execute_query(query, params, database_=settings.neo4j_database)
        return [record.data() for record in records]
    finally:
        driver.close()


# --- collectors --------------------------------------------------------------------------------------

def collect_qdrant(owners: Owners, client: Any = None) -> list[dict[str, Any]]:
    client = client or qdrant_client()
    items = []
    for entry in client.get_collections().collections:
        name = entry.name
        ownership = classify_collection(name, owners)
        detail: dict[str, Any] = {}
        count = None
        try:
            info = client.get_collection(name)
            count = int(info.points_count or 0)
            vectors = info.config.params.vectors
            detail["vector_size"] = getattr(vectors, "size", None) if not isinstance(vectors, dict) else {
                key: value.size for key, value in vectors.items()}
        except Exception as exc:  # noqa: BLE001 — one unreadable collection must not hide the rest
            detail["error"] = str(exc)[:200]
        if ownership["owner"] in ("shared", "legacy", "orphan") and count:
            detail["projects"] = _facet_projects(client, name)
        items.append(_item("qdrant", name, name, "collection", ownership, count=count, detail=detail,
                           deletable=ownership["owner"] in ("orphan", "legacy", "shared")))
    return items


def _facet_projects(client: Any, name: str) -> dict[str, int]:
    try:
        result = client.facet(collection_name=name, key="project_id", limit=500, exact=False)
        return {str(hit.value): int(hit.count) for hit in result.hits}
    except Exception:  # noqa: BLE001 — facets need a payload index; counts are a convenience
        return {}


def collect_neo4j(owners: Owners, run: Callable[..., list[dict]] | None = None) -> list[dict[str, Any]]:
    run = run or neo4j_run
    nodes = run("MATCH (n) WHERE n.collection IS NOT NULL "
                "RETURN n.collection AS collection, labels(n) AS labels, count(*) AS nodes")
    edges = run("MATCH ()-[r]->() WHERE r.collection IS NOT NULL RETURN r.collection AS collection, count(*) AS edges")
    tagged = run("MATCH (n) WHERE n.collection IS NULL AND n.project_id IS NOT NULL "
                 "RETURN n.project_id AS project_id, n.revision_id AS revision_id, count(*) AS nodes")
    loose = run("MATCH (n) WHERE n.collection IS NULL AND n.project_id IS NULL RETURN count(*) AS nodes")
    by_collection: dict[str, dict[str, Any]] = {}
    for row in nodes:
        entry = by_collection.setdefault(row["collection"], {"nodes": 0, "labels": {}, "edges": 0})
        entry["nodes"] += row["nodes"]
        for label in row["labels"] or []:
            entry["labels"][label] = entry["labels"].get(label, 0) + row["nodes"]
    for row in edges:
        by_collection.setdefault(row["collection"], {"nodes": 0, "labels": {}, "edges": 0})["edges"] += row["edges"]
    items = []
    for name, entry in sorted(by_collection.items()):
        items.append(_item("neo4j", f"collection:{name}", name, "graph", classify_collection(name, owners),
                           count=entry["nodes"], detail={"labels": entry["labels"], "edges": entry["edges"]}))
    for row in tagged:
        ownership = classify_revision_ref(row["project_id"], row["revision_id"], owners)
        label = f"{row['project_id']} / {row['revision_id'] or 'project'}"
        items.append(_item("neo4j", f"tagged:{row['project_id']}:{row['revision_id'] or ''}", label, "graph",
                           ownership, count=row["nodes"]))
    if loose and loose[0]["nodes"]:
        items.append(_item("neo4j", "untagged", "Untagged nodes", "graph",
                           {"owner": "legacy", "project_id": None, "revision_id": None,
                            "reason": "Nodes with neither a collection nor a project id."},
                           count=loose[0]["nodes"]))
    return items


def collect_minio(owners: Owners, client: Any = None, bucket: str | None = None) -> list[dict[str, Any]]:
    from index.storage import RAG_UPLOADS_PREFIX, storage

    client = client or storage._get_client()
    bucket = bucket or storage.tus_bucket
    items = []
    for index, obj in enumerate(client.list_objects(bucket, recursive=True)):
        if index >= MAX_OBJECTS:
            break
        key = obj.object_name
        if key.startswith(RAG_UPLOADS_PREFIX + "/"):
            try:
                tags = dict(client.get_object_tags(bucket, key) or {})
            except Exception:  # noqa: BLE001
                tags = {}
            ownership = classify_revision_ref(tags.get("project-id"), tags.get("revision-id"), owners)
            kind, detail = "upload", {"tags": tags}
        elif key.startswith("telemetry-sources/"):
            owner = owners.archive_keys.get(key)
            ownership = ({"owner": "revision", "project_id": owner[0], "revision_id": owner[1],
                          "reason": "Source archive of a registered revision."} if owner else
                         {"owner": "orphan", "project_id": None, "revision_id": None,
                          "reason": "Source archive no registered revision points to."})
            kind, detail = "archive", {}
        else:
            ownership = {"owner": "legacy", "project_id": None, "revision_id": None,
                         "reason": "Raw upload object outside the managed prefixes."}
            kind, detail = "object", {}
        modified = getattr(obj, "last_modified", None)
        updated = modified.isoformat() if modified else None
        deletable = None
        if kind == "object" and (not modified or time.time() - modified.timestamp() < IN_USE_GRACE_SECONDS):
            ownership = {**ownership, "reason": "Recent raw upload; it may still be in progress."}
            deletable = False
        items.append(_item("minio", key, key, kind, ownership, count=1, size=obj.size, updated_at=updated, detail=detail,
                           deletable=deletable))
    return items


def _runtime_tables(runtime: Any) -> list[str]:
    from telemetry.rollups import ROLLUP_TABLE

    return sorted({runtime.table, runtime.trace_table, runtime.profile_table, "unresolved_symbols", ROLLUP_TABLE})


def runtime_scope_columns(runtime: Any, table: str) -> tuple[str, str] | None:
    """Expressions that read project and revision ids from a runtime table, or None when it cannot be scoped."""
    rows = runtime.rows("SELECT name FROM system.columns WHERE database={database:String} AND table={table_name:String}",
                        "", "", {"param_database": runtime.database, "param_table_name": table})
    fields = {row["name"] for row in rows}
    if {"project_id", "revision_id"} <= fields:
        return "project_id", "revision_id"
    if "ResourceAttributes" in fields:
        return "ResourceAttributes['indexer.project_id']", "ResourceAttributes['indexer.revision_id']"
    return None


def collect_clickhouse(owners: Owners, runtime: Any = None) -> list[dict[str, Any]]:
    if runtime is None:
        from telemetry.runtime import ClickHouseRuntime
        runtime = ClickHouseRuntime()
    if not runtime.configured:
        return []
    sizes = {row["table"]: row for row in runtime.rows(
        "SELECT table, sum(rows) AS rows, sum(bytes_on_disk) AS bytes FROM system.parts "
        "WHERE database={database:String} AND active GROUP BY table", "", "", {"param_database": runtime.database})}
    items = []
    for table in _runtime_tables(runtime):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", table):
            continue
        scope = runtime_scope_columns(runtime, table)
        if scope is None:
            continue
        total_rows = int(sizes.get(table, {}).get("rows") or 0) or 1
        total_bytes = int(sizes.get(table, {}).get("bytes") or 0)
        rows = runtime.rows(f"SELECT {scope[0]} AS project_id, {scope[1]} AS revision_id, count() AS n "
                            f"FROM `{runtime.database}`.`{table}` GROUP BY project_id, revision_id", "", "")
        for row in rows:
            n = int(row["n"])
            ownership = classify_revision_ref(row["project_id"] or None, row["revision_id"] or None, owners)
            items.append(_item("clickhouse", f"{table}|{row['project_id']}|{row['revision_id']}",
                               f"{table} · {row['project_id'] or '—'} / {row['revision_id'] or '—'}", "rows",
                               ownership, count=n, size=total_bytes * n // total_rows, detail={"table": table}))
    return items


def sqlite_tables(path: Path) -> list[tuple[str, set[str]]]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10) as db:
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return [(name, {row[1] for row in db.execute(f'PRAGMA table_info("{name}")')}) for name in names]


def collect_sqlite(owners: Owners, files: dict[str, Path] | None = None) -> list[dict[str, Any]]:
    files = files or paths.sqlite_files()
    items = []
    for logical, path in files.items():
        if not path.is_file():
            continue
        size = path.stat().st_size
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10) as db:
            for table, columns in sqlite_tables(path):
                count = db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                detail: dict[str, Any] = {"file": str(path), "file_bytes": size, "columns": sorted(columns)}
                if "project_id" in columns:
                    detail["projects"] = {str(p): n for p, n in db.execute(
                        f'SELECT project_id, count(*) FROM "{table}" GROUP BY project_id')}
                    orphaned = [p for p in detail["projects"] if p and p not in owners.projects]
                    if orphaned:
                        detail["orphan_projects"] = orphaned
                items.append(_item("sqlite", f"{logical}:{table}", f"{logical} · {table}", "table",
                                   {"owner": "shared" if "project_id" in columns else "global", "project_id": None,
                                    "revision_id": None, "reason": "Rows are removed per project by project purge."
                                    if "project_id" in columns else "Application-wide table."},
                                   count=count, detail=detail, deletable=False))
    items.extend(_orphan_registry_rows(owners, files.get("snapshots")))
    return items


def _orphan_registry_rows(owners: Owners, path: Path | None) -> list[dict[str, Any]]:
    """Queue and deployment rows that point at revisions no longer registered."""
    if not path or not path.is_file():
        return []
    items = []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10) as db:
        for table in ("index_jobs", "deployments"):
            try:
                count = db.execute(f"SELECT count(*) FROM {table} t WHERE NOT EXISTS (SELECT 1 FROM snapshots s "
                                   "WHERE s.project_id=t.project_id AND s.commit_sha=t.commit_sha)"
                                   + (" AND t.state!='running'" if table == "deployments" else "")).fetchone()[0]
            except sqlite3.Error:
                continue
            if count:
                items.append(_item("sqlite", f"orphan-rows:{table}", f"snapshots · {table} (orphan rows)", "rows",
                                   {"owner": "orphan", "project_id": None, "revision_id": None,
                                    "reason": "Rows that point at a revision no longer registered."}, count=count))
    return items


def _tree_size(path: Path) -> tuple[int, int]:
    if path.is_file():
        return path.stat().st_size, 1
    total = files = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                total += (Path(root) / name).stat().st_size
                files += 1
            except OSError:
                continue
    return total, files


def collect_files(owners: Owners, directories: dict[str, Path] | None = None) -> list[dict[str, Any]]:
    from projects.workspace_registry import list_workspaces

    directories = directories or paths.directories()
    try:
        registered = {os.path.realpath(str(entry.get("path"))) for entry in list_workspaces()}
    except Exception:  # noqa: BLE001
        registered = set()
    items = []
    for logical, root in directories.items():
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.name.startswith(".") or child.is_symlink():
                continue
            size, files = _tree_size(child)
            ownership = _file_owner(logical, child, owners, registered)
            mtime = child.stat().st_mtime
            deletable = ownership["owner"] in ("orphan", "legacy", "transient")
            if logical == "workspaces" and time.time() - mtime < IN_USE_GRACE_SECONDS:
                ownership = {**ownership, "reason": ownership["reason"] + " Changed recently; it may be in use."}
                deletable = False
            items.append(_item("files", f"{logical}/{child.name}", f"{logical}/{child.name}", logical, ownership,
                               count=files, size=size, updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime)),
                               deletable=deletable))
    return items


def _file_owner(logical: str, child: Path, owners: Owners, registered: set[str]) -> dict[str, Any]:
    none = {"project_id": None, "revision_id": None}
    if logical in ("repo_maps", "code_tools_cache"):
        checksum = child.name.removesuffix(".json")
        if re.fullmatch(r"[0-9a-f]{64}", checksum):
            if checksum in owners.checksums:
                return {"owner": "revision", **none, "reason": "Derived from a registered revision's source archive."}
            return {"owner": "orphan", **none, "reason": "Derived from a source archive no revision uses."}
    if logical == "workspaces":
        if os.path.realpath(child) in registered:
            return {"owner": "legacy", **none, "reason": "Retained extracted upload (most recent workspaces)."}
        return {"owner": "orphan", **none, "reason": "Extracted upload not in the workspace registry."}
    if logical == "logs":
        return {"owner": "transient", **none, "reason": "Developer log file."}
    return {"owner": "legacy", **none, "reason": "Saved analysis artifacts."}


COLLECTORS: dict[str, Callable[[Owners], list[dict[str, Any]]]] = {
    "qdrant": collect_qdrant, "neo4j": collect_neo4j, "minio": collect_minio,
    "clickhouse": collect_clickhouse, "sqlite": collect_sqlite, "files": collect_files,
}


def snapshot(*, refresh: bool = False, registry: Any = None) -> dict[str, Any]:
    """Collect every store (cached briefly; deletion clears the cache)."""
    with _cache_lock:
        cached = _cache.get("snapshot")
        if cached and not refresh and time.monotonic() - cached["at"] < _CACHE_TTL_SECONDS:
            return cached["value"]
    if registry is None:
        from telemetry.registry import default_registry
        registry = default_registry()
    owners = load_owners(registry)
    stores: dict[str, Any] = {}
    for name, collect in COLLECTORS.items():
        started = time.monotonic()
        try:
            items = collect(owners)
            stores[name] = {"status": "ok", "error": None, "items": items}
        except Exception as exc:  # noqa: BLE001 — an unavailable store is reported, not fatal
            logger.warning("Data inventory of %s failed: %s", name, exc)
            stores[name] = {"status": "unavailable", "error": str(exc)[:300], "items": []}
        stores[name]["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    value = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "stores": stores,
             "projects": sorted(owners.projects), "owners": owners}
    with _cache_lock:
        _cache["snapshot"] = {"at": time.monotonic(), "value": value}
    return value


def invalidate() -> None:
    with _cache_lock:
        _cache.clear()


def summarize(inventory: dict[str, Any]) -> dict[str, Any]:
    stores = {}
    orphans = 0
    for name, store in inventory["stores"].items():
        items = store["items"]
        orphan_items = [item for item in items if item["owner"] in ("orphan", "legacy")]
        orphans += len(orphan_items)
        stores[name] = {"status": store["status"], "error": store["error"], "items": len(items),
                        "count": sum(item["count"] or 0 for item in items),
                        "bytes": sum(item["bytes"] or 0 for item in items),
                        "orphans": len(orphan_items), "elapsed_ms": store.get("elapsed_ms")}
    return {"generated_at": inventory["generated_at"], "stores": stores, "orphans": orphans,
            "projects": len(inventory["projects"])}


def project_totals(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    """Per project: items and counts per store, including shared rows attributed through facets."""
    from telemetry.registry import default_registry

    registry = default_registry()
    totals: dict[str, dict[str, Any]] = {}

    def bucket(project_id: str) -> dict[str, Any]:
        return totals.setdefault(project_id, {"project_id": project_id, "registered": project_id in inventory["projects"],
                                              "stores": {name: {"items": 0, "count": 0, "bytes": 0} for name in STORES}})

    for project_id in inventory["projects"]:
        bucket(project_id)
    for name, store in inventory["stores"].items():
        for item in store["items"]:
            if item["project_id"]:
                entry = bucket(item["project_id"])["stores"][name]
                entry["items"] += 1
                entry["count"] += item["count"] or 0
                entry["bytes"] += item["bytes"] or 0
            for project_id, n in (item["detail"].get("projects") or {}).items():
                if project_id and project_id != "None":
                    bucket(project_id)["stores"][name]["count"] += n
    for project_id, entry in totals.items():
        entry["revisions"] = registry.project_revisions(project_id) if entry["registered"] else []
        if entry["revisions"]:
            from telemetry.retention import project_revision_usage

            try:
                usage = project_revision_usage(project_id, entry["revisions"], registry=registry)
            except Exception:  # One unavailable protection source must never produce an "unused" label.
                logger.warning("Revision usage is unavailable for project %s", project_id, exc_info=True)
                usage = {}
            entry["revisions"] = [{**row, "usage": usage.get(row["revision_id"], {
                "status": "unknown", "reasons": ["Could not check revision protections. Refresh to retry."]})}
                for row in entry["revisions"]]

    return sorted(totals.values(), key=lambda row: row["project_id"])


def find_item(inventory: dict[str, Any], store: str, item_id: str) -> dict[str, Any] | None:
    for item in inventory["stores"].get(store, {}).get("items", []):
        if item["id"] == item_id:
            return item
    return None
