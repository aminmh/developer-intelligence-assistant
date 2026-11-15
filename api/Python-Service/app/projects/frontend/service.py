"""Serve the UI map of one project revision.

Plan: @.archcore/frontend-assist/frontend-developer-assistance.plan.md (Phase 2).

A revision is immutable, so its UI map is computed once from the revision's
source archive and kept as a JSON file keyed by the archive checksum, the
application folder, and the extractor version. Nothing is written to the graph
database: the map already holds the route -> component -> store -> request
edges, and later contract checks read them from here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from projects.frontend.ui_map import EXTRACTOR_VERSION, build_ui_map, find_apps

_APP_ROOT = re.compile(r"^[A-Za-z0-9_.@-]+(/[A-Za-z0-9_.@-]+){0,3}$")


class UiMapUnavailable(LookupError):
    """There is no revision source to build the map from; the message is safe to show."""


def _cache_dir() -> Path:
    from telemetry.registry import default_registry

    path = Path(default_registry().root) / "ui_maps"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ui_map_for_revision(project_id: str, revision_id: str | None = None, app_root: str | None = None, *,
                        registry: Any = None, source_resolver: Any = None, cache_dir: Path | None = None) -> dict[str, Any]:
    """The UI map of ``revision_id`` (default: the project's newest ready revision)."""
    from agent_tools.types import ToolCallRejected, ToolContext

    if registry is None:
        from telemetry.registry import default_registry
        registry = default_registry()
    if source_resolver is None:
        from agent_tools.source import resolve_source as source_resolver
    if app_root is not None and (not _APP_ROOT.match(app_root) or ".." in app_root.split("/")):
        raise ValueError("Invalid application folder.")

    revision = (registry.get_by_revision_id(project_id, revision_id) if revision_id
                else registry.latest_ready(project_id))
    if not revision or revision.get("state") != "ready":
        raise UiMapUnavailable("The project has no indexed revision yet. Index the project first.")
    try:
        source = source_resolver(ToolContext(workflow="chat", project_id=project_id, revision_id=revision["revision_id"]))
    except ToolCallRejected as exc:
        raise UiMapUnavailable(exc.message) from exc

    apps = find_apps(source.path)
    app = next((item for item in apps if item["root"] == app_root), None) if app_root is not None else (apps[0] if apps else None)
    if app_root is not None and app is None:
        raise ValueError("No front-end application in that folder.")
    checksum = source.archive_sha256 or revision.get("source_archive_sha256") or ""
    key = hashlib.sha256(f"{checksum}:{app['root'] if app else ''}".encode()).hexdigest()[:32]
    cache = (cache_dir or _cache_dir()) / f"{key}-v{EXTRACTOR_VERSION}.json"
    ui_map = None
    if checksum and cache.is_file():
        try:
            ui_map = json.loads(cache.read_text())
        except (OSError, ValueError):
            ui_map = None
    if ui_map is None:
        ui_map = build_ui_map(source.path, app)
        if checksum:
            temporary = cache.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(ui_map, separators=(",", ":")))
            os.replace(temporary, cache)
    return {"project_id": project_id, "revision_id": revision["revision_id"],
            "revision": {key: revision.get(key) for key in ("revision_id", "commit_sha", "source_kind", "created_at")},
            **ui_map}


def checks_for_revision(project_id: str, revision_id: str | None = None, *, registry: Any = None,
                        source_resolver: Any = None, cache_dir: Path | None = None) -> dict[str, Any]:
    """Front-end static checks (accessibility, security, reliability, performance) for one revision, cached like the UI map."""
    from agent_tools.types import ToolCallRejected, ToolContext
    from code_quality.frontend_checks import CHECKS_VERSION, run_on_directory

    if registry is None:
        from telemetry.registry import default_registry
        registry = default_registry()
    if source_resolver is None:
        from agent_tools.source import resolve_source as source_resolver
    revision = (registry.get_by_revision_id(project_id, revision_id) if revision_id
                else registry.latest_ready(project_id))
    if not revision or revision.get("state") != "ready":
        raise UiMapUnavailable("The project has no indexed revision yet. Index the project first.")
    try:
        source = source_resolver(ToolContext(workflow="chat", project_id=project_id, revision_id=revision["revision_id"]))
    except ToolCallRejected as exc:
        raise UiMapUnavailable(exc.message) from exc
    checksum = source.archive_sha256 or revision.get("source_archive_sha256") or ""
    cache = (cache_dir or _cache_dir()) / f"checks-{hashlib.sha256(checksum.encode()).hexdigest()[:32]}-{CHECKS_VERSION}.json"
    result = None
    if checksum and cache.is_file():
        try:
            result = json.loads(cache.read_text())
        except (OSError, ValueError):
            result = None
    if result is None:
        result = run_on_directory(source.path)
        if checksum:
            temporary = cache.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(result, separators=(",", ":")))
            os.replace(temporary, cache)
    return {"project_id": project_id, "revision_id": revision["revision_id"], **result}
