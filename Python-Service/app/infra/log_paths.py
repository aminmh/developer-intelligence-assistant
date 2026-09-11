"""Canonical filesystem locations for Python-service diagnostics."""

from __future__ import annotations

import os
from pathlib import Path


def default_log_dir() -> Path:
    """Return the one supported log directory for the current runtime.

    Docker runs the service from ``/app`` and mounts the repository's
    ``Python-Service/logs`` directory there. Local source execution uses the
    same repository directory directly. No component should derive an
    ``app/logs`` path independently.
    """
    module_root = Path(__file__).resolve().parents[1]
    if module_root == Path("/app"):
        return module_root / "logs"
    return module_root.parent / "logs"


def configured_log_dir() -> str:
    """Return ``LOG_DIR`` when explicitly configured, otherwise canonical path."""
    return os.getenv("LOG_DIR", str(default_log_dir()))
