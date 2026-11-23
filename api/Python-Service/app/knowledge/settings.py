"""Per-project switch for remote documentation (spec Surface ``remote_docs_allowed``, clause 9).

On by default (user, 2026-11-23): only public library identifiers, versions, file paths and topic
words leave the machine, never project code. A project administrator can turn it off, for example
on an air-gapped install. Stored next to the external-model consent in ``settings_store``.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

STORE_KEY = "remoteDocs"
DEFAULT_ALLOWED = True
OFF_REASON = "Remote documentation is turned off for this project, so only the installed copy was read."
_lock = threading.Lock()


def _store(store: Any = None) -> Any:
    if store is not None:
        return store
    from infra.settings_store import settings_store

    return settings_store


def _projects(store: Any) -> dict[str, Any]:
    data = store.get(STORE_KEY)
    return dict(data.get("projects") or {}) if isinstance(data, dict) else {}


def get_remote_docs(project_id: str, *, store: Any = None) -> dict[str, Any]:
    from infra.remote_consent import validate_project_id

    validate_project_id(project_id)
    entry = _projects(_store(store)).get(project_id)
    if not isinstance(entry, dict):
        return {"project_id": project_id, "allowed": DEFAULT_ALLOWED, "updated_at": None, "source": "default"}
    return {"project_id": project_id, "allowed": bool(entry.get("allowed")), "updated_at": entry.get("updated_at"),
            "source": "admin"}


def set_remote_docs(project_id: str, allowed: Any, *, store: Any = None) -> dict[str, Any]:
    from infra.remote_consent import InvalidProject, validate_project_id

    validate_project_id(project_id)
    if type(allowed) is not bool:
        raise InvalidProject("allowed must be true or false.")
    target = _store(store)
    with _lock:
        projects = _projects(target)
        projects[project_id] = {"allowed": allowed, "updated_at": datetime.now(timezone.utc).isoformat()}
        target.set(STORE_KEY, {"projects": projects})
    return get_remote_docs(project_id, store=target)


def remote_docs_allowed(project_id: str | None, *, store: Any = None) -> bool:
    """Whether remote providers may run; an unreadable store fails closed."""
    if not project_id:
        return False
    try:
        return bool(get_remote_docs(project_id, store=store)["allowed"])
    except Exception:  # noqa: BLE001 — fail closed: no remote calls when the setting cannot be read
        return False
