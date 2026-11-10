"""Per-project consent for sending project source to the external model.

Decision: @.archcore/agent-tools/remote-model-source-access-requires-project-consent.adr.md.
One setting per project covers retrieved code chunks and agent tool results
alike. When it is off, the project gets no external-model answers.

The first time the store loads, every project that already has an indexed
snapshot is marked allowed, so chat keeps working for existing projects. A
project first seen after that starts with consent off until a project
administrator turns it on.

Requests without a project identity (legacy filename-only uploads and chats
with no attachment) carry no project to ask, so they are not gated here.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

STORE_KEY = "remoteSourceConsent"
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_lock = threading.Lock()

NOT_ALLOWED_REASON = (
    "This project does not allow sending its source code to the external model. "
    "A project administrator can turn on “Allow source to go to the external model” in Project access."
)


class InvalidProject(ValueError):
    pass


class RemoteSourceNotAllowed(PermissionError):
    """The project has not allowed its source to go to the external model."""

    def __init__(self, project_id: str | None = None) -> None:
        super().__init__(NOT_ALLOWED_REASON)
        self.project_id = project_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def existing_project_ids() -> list[str]:
    """Projects that already have an indexed snapshot (the seed set)."""
    from telemetry.registry import default_registry

    db = sqlite3.connect(Path(default_registry().root) / "snapshots.sqlite", timeout=30)
    try:
        return sorted({row[0] for row in db.execute("SELECT DISTINCT project_id FROM snapshots") if row[0]})
    finally:
        db.close()


def _store(store: Any = None) -> Any:
    if store is not None:
        return store
    from infra.settings_store import settings_store

    return settings_store


def _load(store: Any, seed: Callable[[], list[str]] | None) -> dict[str, Any]:
    data = store.get(STORE_KEY)
    if isinstance(data, dict) and isinstance(data.get("projects"), dict):
        return data
    try:
        seeded = (seed or existing_project_ids)()
    except Exception:  # noqa: BLE001 — an unreadable registry seeds nothing; projects then start off
        seeded = []
    data = {"seeded_at": _now(),
            "projects": {pid: {"allowed": True, "updated_at": _now(), "source": "existing-project"} for pid in seeded}}
    store.set(STORE_KEY, data)
    return data


def validate_project_id(project_id: Any) -> str:
    if not isinstance(project_id, str) or not _PROJECT_ID.match(project_id):
        raise InvalidProject("A valid project ID is required.")
    return project_id


def get_consent(project_id: str, *, store: Any = None, seed: Callable[[], list[str]] | None = None) -> dict[str, Any]:
    """Return ``{"project_id", "allowed", "updated_at", "source"}`` for one project."""
    validate_project_id(project_id)
    with _lock:
        entry = _load(_store(store), seed)["projects"].get(project_id)
    if not isinstance(entry, dict):
        return {"project_id": project_id, "allowed": False, "updated_at": None, "source": "default"}
    return {"project_id": project_id, "allowed": bool(entry.get("allowed")),
            "updated_at": entry.get("updated_at"), "source": entry.get("source", "admin")}


def set_consent(project_id: str, allowed: Any, *, store: Any = None,
                seed: Callable[[], list[str]] | None = None) -> dict[str, Any]:
    validate_project_id(project_id)
    if type(allowed) is not bool:
        raise InvalidProject("allowed must be true or false.")
    target = _store(store)
    with _lock:
        data = _load(target, seed)
        projects = dict(data["projects"])
        projects[project_id] = {"allowed": allowed, "updated_at": _now(), "source": "admin"}
        target.set(STORE_KEY, {**data, "projects": projects})
    return get_consent(project_id, store=target, seed=seed)


def require_remote_source_allowed(project_id: Any) -> None:
    """Raise ``RemoteSourceNotAllowed`` unless the project allows external-model use.

    Fails closed: an unreadable consent store also raises. Call it right before
    a feature that sends project source to the external model.
    """
    try:
        allowed = remote_source_allowed(project_id)
    except Exception as exc:  # noqa: BLE001 — unreadable consent means not allowed
        raise RemoteSourceNotAllowed(project_id) from exc
    if not allowed:
        raise RemoteSourceNotAllowed(project_id)


def remote_source_allowed(project_id: Any, *, store: Any = None,
                          seed: Callable[[], list[str]] | None = None) -> bool:
    """True when source may go to the external model for this request's project."""
    if not project_id:
        return True
    try:
        return get_consent(str(project_id), store=store, seed=seed)["allowed"]
    except InvalidProject:
        return False
