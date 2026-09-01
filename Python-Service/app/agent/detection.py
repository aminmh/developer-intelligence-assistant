"""Project detection pipeline (spec §7).

Turns a path into a structured ProjectProfile by asking every registered
language/framework plugin for its opinion. Pure registry delegation — no
framework names appear here.
"""

from __future__ import annotations

from pathlib import Path

from agent.core.profile import ProjectProfile
from agent.core.registry import PluginRegistry
from agent.core.types import Project


def detect_project(
    path: str | Path,
    registry: PluginRegistry | None = None,
) -> ProjectProfile:
    if registry is None:
        from agent.plugins import register_builtins

        registry = PluginRegistry()
        register_builtins(registry)
    project = Project.from_path(path)
    return registry.detect(project)
