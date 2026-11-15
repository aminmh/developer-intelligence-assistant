"""Revision retention: keep each project's latest N revisions and remove the rest.

Plan: @.archcore/knowledge-base/keep-the-index-in-sync-with-a-changing-codebase.plan.md (phase 5).
Every sync creates a new immutable revision, so old ones pile up. Per project,
``keep_latest`` (default 3, minimum 1) ready revisions are kept; working-tree
revisions roll separately, only the newest is kept, outside N. A revision is never
removed by retention when it is:

- the project's latest ready revision;
- one with a Code Quality report (so N is a floor, not an exact count);
- one with a running deployment;
- pinned by a project administrator (the ``pinned`` list);
- a Git source's last synced revision.

Only ready revisions are chosen: queued, indexing, failed and deployment-waiting
rows have their own lifecycles. A removal interrupted part-way (state ``deleting``)
is retried on every pass until it completes, and so is a working tree a newer one
superseded (state ``superseded``). Cleanup runs in the index worker loop between jobs,
because ``remove_revision`` refuses while a job holds the worker lock; a busy
revision is simply retried on the next pass.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

STORE_KEY = "revisionRetention"
DEFAULT_KEEP_LATEST = 3
MAX_KEEP_LATEST = 100
MAX_PINNED = 50
_lock = threading.Lock()
log = logging.getLogger(__name__)


class InvalidRetention(ValueError):
    pass


def _store(store: Any = None) -> Any:
    if store is not None:
        return store
    from infra.settings_store import settings_store

    return settings_store


def _projects(store: Any) -> dict[str, Any]:
    data = store.get(STORE_KEY)
    return dict(data["projects"]) if isinstance(data, dict) and isinstance(data.get("projects"), dict) else {}


def get_policy(project_id: str, *, store: Any = None) -> dict[str, Any]:
    """``{"project_id", "keep_latest", "pinned", "updated_at"}``; unset projects get the default."""
    from telemetry.snapshot import validate_project_id

    validate_project_id(project_id)
    with _lock:
        entry = _projects(_store(store)).get(project_id)
    entry = entry if isinstance(entry, dict) else {}
    keep = entry.get("keep_latest")
    pinned = entry.get("pinned")
    return {"project_id": project_id,
            "keep_latest": keep if type(keep) is int and 1 <= keep <= MAX_KEEP_LATEST else DEFAULT_KEEP_LATEST,
            "pinned": [p for p in pinned if isinstance(p, str)] if isinstance(pinned, list) else [],
            "updated_at": entry.get("updated_at")}


def set_policy(project_id: str, change: dict[str, Any], *, store: Any = None) -> dict[str, Any]:
    """Change ``keep_latest`` and/or ``pinned`` for one project."""
    from telemetry.snapshot import validate_project_id, validate_revision_id

    validate_project_id(project_id)
    if not isinstance(change, dict) or not ({"keep_latest", "pinned"} & change.keys()):
        raise InvalidRetention("Give keep_latest and/or pinned.")
    current = get_policy(project_id, store=store)
    if "keep_latest" in change:
        keep = change["keep_latest"]
        if type(keep) is not int or not 1 <= keep <= MAX_KEEP_LATEST:
            raise InvalidRetention(f"keep_latest must be a whole number from 1 to {MAX_KEEP_LATEST}.")
        current["keep_latest"] = keep
    if "pinned" in change:
        pinned = change["pinned"]
        if not isinstance(pinned, list) or len(pinned) > MAX_PINNED:
            raise InvalidRetention(f"pinned must be a list of at most {MAX_PINNED} revision IDs.")
        try:
            for revision_id in pinned:
                validate_revision_id(revision_id if isinstance(revision_id, str) else "")
        except ValueError as exc:
            raise InvalidRetention("pinned holds an invalid revision ID.") from exc
        current["pinned"] = sorted(set(pinned))
    target = _store(store)
    with _lock:
        projects = _projects(target)
        projects[project_id] = {"keep_latest": current["keep_latest"], "pinned": current["pinned"],
                                "updated_at": datetime.now(timezone.utc).isoformat()}
        target.set(STORE_KEY, {"projects": projects})
    return get_policy(project_id, store=target)


def revision_usage(rows: list[dict[str, Any]], *, registry: Any, project_id: str,
                   policy: dict[str, Any], reported: set[str], git_revisions: set[str]) -> dict[str, dict[str, Any]]:
    """Explain the same protections used by retention, without changing stored data."""
    from telemetry.snapshot import source_reference

    ready = [row for row in rows if row.get("state") == "ready"]  # newest first
    latest = ready[0]["revision_id"] if ready else None
    kept_trees = kept = 0
    usage = {}
    for row in rows:
        revision_id = row["revision_id"]
        if row.get("state") != "ready":
            usage[revision_id] = {"status": "not_ready", "reasons": [f"Revision is {row.get('state') or 'not ready'}."]}
            continue
        reasons = []
        if revision_id == latest:
            reasons.append("Latest ready revision.")
        if revision_id in policy["pinned"]:
            reasons.append("Pinned in Project access.")
        if revision_id in reported:
            reasons.append("Has a Code Quality assessment report.")
        if revision_id in git_revisions:
            reasons.append("Last synced revision of a Git source.")
        if registry.has_deployment(project_id, source_reference(row)):
            reasons.append("Used by a running deployment.")
        if row.get("source_kind") == "working_tree":
            kept_trees += 1
            if kept_trees == 1:
                reasons.append("Newest working-tree revision.")
        else:
            kept += 1
            if kept <= policy["keep_latest"]:
                reasons.append(f"Within the latest {policy['keep_latest']} revisions kept by retention.")
        usage[revision_id] = {"status": "kept" if reasons else "unused",
                              "reasons": reasons or ["No retention protection or running deployment; eligible for cleanup."]}
    return usage


def removable_revisions(project_id: str, *, registry: Any, policy: dict[str, Any],
                        reported: set[str], git_revisions: set[str]) -> list[dict[str, Any]]:
    """The project's ready revisions retention would remove now, oldest last kept first."""
    rows = registry.project_revisions(project_id)
    usage = revision_usage(rows, registry=registry, project_id=project_id, policy=policy,
                           reported=reported, git_revisions=git_revisions)
    return [row for row in rows if usage[row["revision_id"]]["status"] == "unused"]


def project_revision_usage(project_id: str, rows: list[dict[str, Any]], *, registry: Any) -> dict[str, dict[str, Any]]:
    return revision_usage(rows, registry=registry, project_id=project_id, policy=get_policy(project_id),
                          reported=_reported_revisions(project_id), git_revisions=_git_revisions(registry, project_id))


def _reported_revisions(project_id: str) -> set[str]:
    from code_quality.service import ReportStore

    with ReportStore()._connect() as db:
        rows = db.execute("SELECT DISTINCT revision_id FROM assessment_reports WHERE project_id=?",
                          (project_id,)).fetchall()
    return {row[0] for row in rows if row[0]}


def _git_revisions(registry: Any, project_id: str) -> set[str]:
    return {row["last_revision_id"] for row in registry.list_git_sources()
            if row.get("project_id") == project_id and row.get("last_revision_id")}


def plan_cleanup(*, registry: Any = None, store: Any = None) -> dict[str, list[dict[str, Any]]]:
    """What retention would remove now, per project (no changes)."""
    if registry is None:
        from telemetry.registry import default_registry
        registry = default_registry()
    with registry._connect() as db:
        project_ids = sorted({row[0] for row in db.execute("SELECT DISTINCT project_id FROM snapshots") if row[0]})
    plan = {}
    for project_id in project_ids:
        # A removal that failed part-way (e.g. ClickHouse down) leaves the row in 'deleting';
        # its index data may already be gone, so it is finished first, whatever the policy.
        interrupted = [row for row in registry.project_revisions(project_id) if row.get("state") in ("deleting", "superseded")]
        rows = interrupted + removable_revisions(project_id, registry=registry, policy=get_policy(project_id, store=store),
                                   reported=_reported_revisions(project_id),
                                   git_revisions=_git_revisions(registry, project_id))
        if rows:
            plan[project_id] = rows
    return plan


def run_cleanup(*, registry: Any = None, store: Any = None,
                remove: Callable[[str, str], bool] | None = None) -> list[tuple[str, str]]:
    """Remove what ``plan_cleanup`` lists; a busy or failing revision waits for the next pass."""
    if remove is None:
        from code_quality.revision_deletion import remove_revision as remove
    removed = []
    for project_id, rows in plan_cleanup(registry=registry, store=store).items():
        for row in rows:
            try:
                if remove(project_id, row["revision_id"]):
                    removed.append((project_id, row["revision_id"]))
                    log.info("Retention removed revision project=%s revision_id=%s", project_id, row["revision_id"])
            except Exception as exc:  # noqa: BLE001 — retried next pass
                log.warning("Retention could not remove revision project=%s revision_id=%s: %s",
                            project_id, row["revision_id"], str(exc)[:300])
    return removed
