"""Dry-run planned deletion across every store, executed as one background job at a time.

A plan is a list of steps derived from the current inventory. Its hash covers each step's action and
arguments (not the row estimates), and execution re-plans and refuses when the hash changed, so an
administrator never runs a deletion they did not preview.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import threading
from typing import Any, Callable

from data_admin import inventory, paths
from data_admin.journal import Journal
from data_admin.preview import is_within

logger = logging.getLogger(__name__)

PROJECT_SETTINGS_KEYS = ("revisionRetention", "remoteSourceConsent")
# Revisions and their deployments are removed only through remove_revision, which takes the locks.
_REGISTRY_TABLES = {"snapshots", "deployments", "deployment_events", "runtime_settings"}
_start_lock = threading.Lock()


class PlanError(ValueError):
    """The target cannot be planned (unknown, protected, or not deletable)."""


class PlanChanged(RuntimeError):
    """The data changed since the dry run; preview again."""


class JobRunning(RuntimeError):
    """Another deletion job is running."""


def _step(action: str, store: str, description: str, args: dict[str, Any], *, estimate: int | None = None,
          project_id: str | None = None) -> dict[str, Any]:
    return {"action": action, "store": store, "description": description, "args": args,
            "estimate": estimate, "project_id": project_id, "status": "pending", "error": None}


def plan_hash(target: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    canonical = json.dumps({"target": target, "steps": [[s["action"], s["args"]] for s in steps]}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def plan(target: dict[str, Any], inv: dict[str, Any] | None = None, registry: Any = None) -> dict[str, Any]:
    if registry is None:
        from telemetry.registry import default_registry
        registry = default_registry()
    inv = inv or inventory.snapshot(refresh=True, registry=registry)
    kind = target.get("kind")
    warnings: list[str] = []
    if kind == "revision":
        steps = _plan_revision(str(target.get("project_id") or ""), str(target.get("revision_id") or ""), inv, registry, warnings)
    elif kind == "project":
        steps = _plan_project(str(target.get("project_id") or ""), inv, registry, warnings)
    elif kind == "items":
        steps = _plan_items(target.get("items"), inv, warnings)
    else:
        raise PlanError("Unknown deletion target.")
    if not steps:
        raise PlanError("Nothing to delete.")
    unavailable = [name for name, store in inv["stores"].items() if store["status"] != "ok"]
    if unavailable:
        warnings.append("Unavailable stores were not inspected: " + ", ".join(unavailable) + ". Their data stays until a retry.")
    return {"target": target, "steps": steps, "warnings": warnings, "hash": plan_hash(target, steps),
            "totals": _totals(steps)}


def _totals(steps: list[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for step in steps:
        totals[step["store"]] = totals.get(step["store"], 0) + (step["estimate"] or 0)
    return totals


def _count_project_points(collection: str, project_id: str) -> int | None:
    """Exact count when the facet was unavailable (no payload index); None when Qdrant cannot say."""
    from qdrant_client import models

    try:
        result = inventory.qdrant_client().count(collection_name=collection, exact=True, count_filter=models.Filter(must=[
            models.FieldCondition(key="project_id", match=models.MatchValue(value=project_id))]))
        return int(result.count)
    except Exception:  # noqa: BLE001 — keep the step; the filtered delete is safe either way
        return None


def _items(inv: dict[str, Any]):
    for store in inv["stores"].values():
        yield from store["items"]


def _plan_revision(project_id: str, revision_id: str, inv: dict[str, Any], registry: Any,
                   warnings: list[str]) -> list[dict[str, Any]]:
    from telemetry.snapshot import source_reference

    try:
        row = registry.get_by_revision_id(project_id, revision_id)
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    if row is None:
        raise PlanError("Revision not found.")
    if registry.has_deployment(project_id, source_reference(row)):
        warnings.append("A running deployment uses this revision; removal will be refused until it stops.")
    owned = [item for item in _items(inv) if item["revision_id"] == revision_id and item["project_id"] == project_id]
    breakdown = {}
    for item in owned:
        breakdown[item["store"]] = breakdown.get(item["store"], 0) + (item["count"] or 1)
    detail = ", ".join(f"{store}: {n}" for store, n in sorted(breakdown.items())) or "registry record only"
    return [_step("remove_revision", "all", f"Remove revision {revision_id} ({detail})",
                  {"project_id": project_id, "revision_id": revision_id},
                  estimate=sum(breakdown.values()), project_id=project_id)]


def _plan_project(project_id: str, inv: dict[str, Any], registry: Any, warnings: list[str]) -> list[dict[str, Any]]:
    from telemetry.snapshot import validate_project_id

    try:
        validate_project_id(project_id)
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    steps: list[dict[str, Any]] = []
    for row in registry.project_revisions(project_id) if project_id in inv["projects"] else []:
        steps += _plan_revision(project_id, row["revision_id"], inv, registry, warnings)
    owners = inv["owners"]
    for item in inv["stores"]["qdrant"]["items"]:
        if item["owner"] == "revision":
            continue  # dropped with its revision, or another project's
        if item["owner"] == "project" and item["project_id"] == project_id:
            steps.append(_step("qdrant_drop", "qdrant", f"Drop collection {item['name']}", {"collection": item["name"]},
                               estimate=item["count"], project_id=project_id))
            continue
        if item["owner"] not in ("shared", "legacy", "orphan") or not item["count"]:
            continue
        projects = item["detail"].get("projects")
        points = projects.get(project_id) if projects else _count_project_points(item["name"], project_id)
        if points == 0 or (projects and not points):
            continue  # this collection holds no point of the project
        steps.append(_step("qdrant_delete_points", "qdrant", f"Delete this project's points in {item['name']}",
                           {"collection": item["name"], "project_id": project_id}, estimate=points, project_id=project_id))
    for item in inv["stores"]["neo4j"]["items"]:
        if item["id"].startswith("tagged:") and item["project_id"] == project_id and item["owner"] != "revision":
            steps.append(_step("neo4j_delete_tagged", "neo4j", f"Delete graph nodes tagged {item['name']}",
                               {"project_id": project_id, "revision_id": item["revision_id"]},
                               estimate=item["count"], project_id=project_id))
    if any(item["project_id"] == project_id for item in inv["stores"]["clickhouse"]["items"]):
        rows = sum(item["count"] or 0 for item in inv["stores"]["clickhouse"]["items"] if item["project_id"] == project_id)
        steps.append(_step("clickhouse_delete", "clickhouse", "Delete this project's runtime telemetry",
                           {"project_id": project_id, "revision_id": None}, estimate=rows, project_id=project_id))
    for item in inv["stores"]["minio"]["items"]:
        if item["project_id"] == project_id and item["owner"] != "revision":
            steps.append(_step("minio_remove", "minio", f"Remove object {item['id']}", {"key": item["id"]},
                               estimate=1, project_id=project_id))
    approvals = 0
    for item in inv["stores"]["sqlite"]["items"]:
        logical, _, table = item["id"].partition(":")
        rows = (item["detail"].get("projects") or {}).get(project_id)
        if not rows or (logical == "snapshots" and table in _REGISTRY_TABLES):
            continue
        if table == "tool_approvals":
            approvals = rows
        steps.append(_step("sqlite_delete_project", "sqlite", f"Delete {rows} row(s) from {item['name']}",
                           {"database": logical, "table": table, "project_id": project_id},
                           estimate=rows, project_id=project_id))
    if approvals:
        # Checkpoints are keyed by run id, so they must go before the approvals that name those runs.
        steps.insert(next(i for i, s in enumerate(steps) if s["action"] == "sqlite_delete_project"),
                     _step("sqlite_delete_checkpoints", "sqlite", "Delete paused agent-run checkpoints of this project",
                           {"project_id": project_id}, estimate=approvals, project_id=project_id))
    steps.append(_step("settings_forget_project", "sqlite", "Forget retention and model-consent settings",
                       {"project_id": project_id}, estimate=0, project_id=project_id))
    if project_id not in owners.projects:
        warnings.append("This project has no registered revisions; only leftover data is removed.")
    warnings.append("Project memberships are removed when the job succeeds.")
    return steps


def _plan_items(raw: Any, inv: dict[str, Any], warnings: list[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw or len(raw) > 500:
        raise PlanError("Select between 1 and 500 items.")
    steps = []
    for ref in raw:
        if not isinstance(ref, dict):
            raise PlanError("Invalid item reference.")
        item = inventory.find_item(inv, str(ref.get("store")), str(ref.get("id")))
        if item is None:
            raise PlanError(f"Item {ref.get('store')}:{ref.get('id')} no longer exists. Refresh and retry.")
        if not item["deletable"]:
            raise PlanError(f"{item['name']} belongs to a revision or project. Remove it with revision removal or project purge.")
        steps.append(_item_step(item))
        if item["owner"] == "shared":
            warnings.append(f"{item['name']} is shared by every project; all its data is removed.")
    return steps


def _item_step(item: dict[str, Any]) -> dict[str, Any]:
    store, item_id, name = item["store"], item["id"], item["name"]
    if store == "qdrant":
        return _step("qdrant_drop", store, f"Drop collection {name}", {"collection": item_id}, estimate=item["count"])
    if store == "neo4j":
        if item_id.startswith("collection:"):
            return _step("neo4j_delete_collection", store, f"Delete graph {name}", {"collection": name}, estimate=item["count"])
        if item_id.startswith("tagged:"):
            return _step("neo4j_delete_tagged", store, f"Delete graph nodes tagged {name}",
                         {"project_id": item["project_id"], "revision_id": item["revision_id"]}, estimate=item["count"])
        return _step("neo4j_delete_untagged", store, "Delete untagged graph nodes", {}, estimate=item["count"])
    if store == "minio":
        return _step("minio_remove", store, f"Remove object {name}", {"key": item_id}, estimate=1)
    if store == "clickhouse":
        return _step("clickhouse_delete", store, f"Delete runtime rows {name}",
                     {"project_id": item["project_id"], "revision_id": item["revision_id"], "table": item["detail"]["table"]},
                     estimate=item["count"])
    if store == "sqlite" and item_id.startswith("orphan-rows:"):
        return _step("sqlite_delete_orphan_rows", store, f"Delete {name}", {"table": item_id.split(":", 1)[1]}, estimate=item["count"])
    if store == "files":
        logical, child = item_id.split("/", 1)
        return _step("files_remove", store, f"Delete {name}", {"directory": logical, "name": child}, estimate=item["count"])
    raise PlanError(f"{name} cannot be deleted on its own.")


# --- execution -----------------------------------------------------------------------------------------

def start(target: dict[str, Any], expected_hash: str, actor: dict[str, str | None], *, journal: Journal | None = None,
          on_success: Callable[[dict[str, Any]], None] | None = None, background: bool = True) -> dict[str, Any]:
    journal = journal or Journal()
    with _start_lock:
        if journal.running_job():
            raise JobRunning("Another deletion is running. Wait for it to finish.")
        current = plan(target)
        if current["hash"] != expected_hash:
            journal.audit(actor=actor, action="execute", target=target, plan=current, result="refused",
                          error="The data changed since the dry run.")
            raise PlanChanged("The data changed since the dry run. Review the new plan and confirm again.")
        job_id = journal.create_job(actor_sub=actor.get("sub"), target=target, steps=current["steps"])
    journal.audit(actor=actor, action="execute", target=target, plan=current, result="started", job_id=job_id)
    runner = threading.Thread(target=_run, args=(job_id, current["steps"], target, actor, journal, on_success),
                              name=f"data-admin-{job_id}", daemon=True)
    if background:
        runner.start()
    else:
        runner.run()
    return journal.job(job_id) or {}


def _run(job_id: str, steps: list[dict[str, Any]], target: dict[str, Any], actor: dict[str, str | None],
         journal: Journal, on_success: Callable[[dict[str, Any]], None] | None) -> None:
    error = None
    for step in steps:
        step["status"] = "running"
        journal.update_job(job_id, steps=steps)
        try:
            step["result"] = EXECUTORS[step["action"]](**step["args"])
            step["status"] = "done"
        except Exception as exc:  # noqa: BLE001 — every failure is recorded on the job for retry
            logger.warning("Data deletion step %s failed: %s", step["action"], exc)
            step["status"] = "failed"
            step["error"] = error = str(exc)[:500] or exc.__class__.__name__
            break
    for step in steps:
        if step["status"] == "pending":
            step["status"] = "skipped"
    inventory.invalidate()
    state = "failed" if error else "succeeded"
    journal.update_job(job_id, steps=steps, state=state, error=error)
    journal.audit(actor=actor, action="execute", target=target, result=state, error=error, job_id=job_id,
                  plan={"steps": steps})
    if not error and on_success:
        try:
            on_success(target)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Post-deletion hook failed: %s", exc)


def _remove_revision(project_id: str, revision_id: str) -> str:
    from code_quality.revision_deletion import remove_revision

    return "removed" if remove_revision(project_id, revision_id) else "already removed"


def _qdrant_drop(collection: str) -> str:
    client = inventory.qdrant_client()
    if client.collection_exists(collection):
        client.delete_collection(collection)
        return "dropped"
    return "already removed"


def _qdrant_delete_points(collection: str, project_id: str) -> str:
    from qdrant_client import models

    client = inventory.qdrant_client()
    if not client.collection_exists(collection):
        return "already removed"
    selector = models.FilterSelector(filter=models.Filter(must=[
        models.FieldCondition(key="project_id", match=models.MatchValue(value=project_id))]))
    client.delete(collection_name=collection, points_selector=selector, wait=True)
    return "deleted"


def _neo4j_batched(match: str, **params: Any) -> int:
    total = 0
    while True:
        rows = inventory.neo4j_run(f"{match} WITH n LIMIT 5000 DETACH DELETE n RETURN count(*) AS n", **params)
        removed = rows[0]["n"] if rows else 0
        total += removed
        if removed < 5000:
            return total


def _neo4j_delete_collection(collection: str) -> int:
    removed = _neo4j_batched("MATCH (n) WHERE n.collection = $c", c=collection)
    inventory.neo4j_run("MATCH ()-[r]->() WHERE r.collection = $c DELETE r", c=collection)
    return removed


def _neo4j_delete_tagged(project_id: str, revision_id: str | None) -> int:
    if revision_id:
        return _neo4j_batched("MATCH (n) WHERE n.collection IS NULL AND n.project_id = $p AND n.revision_id = $r",
                              p=project_id, r=revision_id)
    return _neo4j_batched("MATCH (n) WHERE n.collection IS NULL AND n.project_id = $p", p=project_id)


def _neo4j_delete_untagged() -> int:
    return _neo4j_batched("MATCH (n) WHERE n.collection IS NULL AND n.project_id IS NULL")


def _minio_remove(key: str) -> str:
    from index.storage import storage

    storage._get_client().remove_object(storage.tus_bucket, key)
    return "removed"


def _clickhouse_delete(project_id: str | None, revision_id: str | None, table: str | None = None) -> str:
    from telemetry.runtime import ClickHouseRuntime

    runtime = ClickHouseRuntime()
    if not runtime.configured:
        return "not configured"
    tables = [table] if table else inventory._runtime_tables(runtime)
    for name in tables:
        if name not in inventory._runtime_tables(runtime):
            raise PlanError("Unknown runtime table.")
        scope = inventory.runtime_scope_columns(runtime, name)
        if scope is None:
            continue
        clause = f"{scope[0]}={{project_id:String}}"
        if revision_id is not None or table:
            clause += f" AND {scope[1]}={{revision_id:String}}"
        runtime.execute(f"ALTER TABLE `{runtime.database}`.`{name}` DELETE WHERE {clause}", timeout=120,
                        extra_parameters={"mutations_sync": "1", "param_project_id": project_id or "",
                                          "param_revision_id": revision_id or ""})
    return "deleted"


def _sqlite_path(database: str):
    files = paths.sqlite_files()
    if database not in files:
        raise PlanError("Unknown database.")
    return files[database]


def _sqlite_delete_project(database: str, table: str, project_id: str) -> int:
    path = _sqlite_path(database)
    if table not in {name for name, columns in inventory.sqlite_tables(path) if "project_id" in columns}:
        raise PlanError("Table has no project column.")
    if database == "snapshots" and table in _REGISTRY_TABLES:
        raise PlanError("Registry tables are cleaned through revision removal.")
    with sqlite3.connect(path, timeout=30) as db:
        db.execute("PRAGMA foreign_keys=ON")  # domain concepts cascade to aliases, sources and relationships
        return db.execute(f'DELETE FROM "{table}" WHERE project_id=?', (project_id,)).rowcount


def _sqlite_delete_checkpoints(project_id: str) -> int:
    approvals = _sqlite_path("agent_runs")
    checkpoints = _sqlite_path("agent_checkpoints")
    if not approvals.is_file() or not checkpoints.is_file():
        return 0
    with sqlite3.connect(approvals, timeout=30) as db:
        runs = [row[0] for row in db.execute("SELECT DISTINCT run_id FROM tool_approvals WHERE project_id=?", (project_id,))]
    removed = 0
    with sqlite3.connect(checkpoints, timeout=30) as db:
        for table, columns in inventory.sqlite_tables(checkpoints):
            if "thread_id" in columns:
                for run_id in runs:
                    removed += db.execute(f'DELETE FROM "{table}" WHERE thread_id=?', (run_id,)).rowcount
    return removed


def _sqlite_delete_orphan_rows(table: str) -> int:
    if table not in ("index_jobs", "deployments"):
        raise PlanError("Unknown orphan table.")
    with sqlite3.connect(_sqlite_path("snapshots"), timeout=30) as db:
        return db.execute(f"DELETE FROM {table} WHERE NOT EXISTS (SELECT 1 FROM snapshots s WHERE s.project_id={table}.project_id "
                          f"AND s.commit_sha={table}.commit_sha)" + (" AND state!='running'" if table == "deployments" else "")).rowcount


def _settings_forget_project(project_id: str, store: Any = None) -> list[str]:
    if store is None:
        from infra.settings_store import settings_store as store
    forgotten = []
    for key in PROJECT_SETTINGS_KEYS:
        value = store.get(key)
        if isinstance(value, dict) and isinstance(value.get("projects"), dict) and project_id in value["projects"]:
            projects = dict(value["projects"])
            projects.pop(project_id)
            store.set(key, {**value, "projects": projects})
            forgotten.append(key)
    return forgotten


def _files_remove(directory: str, name: str) -> str:
    roots = paths.directories()
    if directory not in roots or "/" in name or name in ("", ".", ".."):
        raise PlanError("Unknown file location.")
    root = roots[directory]
    target = root / name
    if not is_within(root, target) or target.is_symlink():
        raise PlanError("Refusing to delete outside the managed directory.")
    if directory == "workspaces":
        from projects.workspace_registry import forget
        forget(name)
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    return "removed"


EXECUTORS: dict[str, Callable[..., Any]] = {
    "remove_revision": _remove_revision,
    "qdrant_drop": _qdrant_drop,
    "qdrant_delete_points": _qdrant_delete_points,
    "neo4j_delete_collection": _neo4j_delete_collection,
    "neo4j_delete_tagged": _neo4j_delete_tagged,
    "neo4j_delete_untagged": _neo4j_delete_untagged,
    "minio_remove": _minio_remove,
    "clickhouse_delete": _clickhouse_delete,
    "sqlite_delete_project": _sqlite_delete_project,
    "sqlite_delete_checkpoints": _sqlite_delete_checkpoints,
    "sqlite_delete_orphan_rows": _sqlite_delete_orphan_rows,
    "settings_forget_project": _settings_forget_project,
    "files_remove": _files_remove,
}
