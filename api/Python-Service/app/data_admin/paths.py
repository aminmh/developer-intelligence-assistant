"""Where each local store lives. Every path honours the same env override its owning module reads."""
from __future__ import annotations

import os
from pathlib import Path


def snapshot_root() -> Path:
    from infra.settings import settings

    return Path(os.environ.get("TELEMETRY_SNAPSHOT_ROOT") or Path(settings.workspace_root).parent / "snapshots")


def _agent_runs_dir() -> Path:
    return Path(os.environ.get("AGENT_RUNS_DIR", "/app/data"))


def sqlite_files() -> dict[str, Path]:
    """Logical name -> SQLite file. Missing files are reported, never created."""
    root = snapshot_root()
    return {
        "snapshots": root / "snapshots.sqlite",
        "code_quality": root / "code_quality.sqlite",
        "settings": Path(os.environ.get("SETTINGS_DB_PATH", "/app/data/settings.db")),
        "domain_concepts": Path(os.environ.get("DOMAIN_DB_PATH", "/app/data/domain_concepts.db")),
        "agent_runs": _agent_runs_dir() / "agent_runs.sqlite",
        "agent_checkpoints": _agent_runs_dir() / "agent_checkpoints.sqlite",
    }


def directories() -> dict[str, Path]:
    """Logical name -> directory whose direct children are listed as items."""
    from infra.log_paths import configured_log_dir
    from infra.settings import settings

    try:
        from analysis.analysis_service import bundle_dir
        analyses = bundle_dir()
    except Exception:  # noqa: BLE001 — analysis extras are optional in some images
        analyses = Path("/app/data/analyses/jobs")
    return {
        "workspaces": Path(settings.workspace_root),
        "analyses": analyses,
        "repo_maps": Path(os.environ.get("REPO_MAP_DIR", "/app/data/repo_maps")),
        "code_tools_cache": Path(os.environ.get("CODE_TOOLS_CACHE_DIR", "/tmp/devassistant-code-tools")),
        "logs": Path(configured_log_dir()),
    }
