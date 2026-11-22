"""Extensible AI Coding Agent Platform — plugin architecture (scope A).

Roadmap (spec phases, marked as TODO in code):

- [x] scope-A: plugin interfaces, registry + entry-point discovery, project
      detection → ProjectProfile, capability model, DI container, PHP +
      Laravel plugins, TestFrameworkPlugin (extensibility proof).
- [ ] TODO(scope-B): agent loop — LLM planning → patch → capability-driven
      validation → iterate (builds on llm.py;
      live LSP/artisan validators; new /api/agent/* endpoints).
- [ ] TODO(scope-C): code-graph integration — framework-specific edges into
      graph_store.py (MODEL_RELATIONSHIP, CONTAINER_BINDING, MIDDLEWARE_CHAIN)
      and graph-aware retrieval (spec §16, §28).
- [ ] Phase 2/3: Python+Django, TypeScript+NestJS plugins (no core changes).
- [ ] Packaging: pyproject.toml with [project.entry-points] so plugins become
      pip-installable external packages (spec §6, §23).
"""

from agent.core.di import Container, default_container
from agent.core.registry import PluginRegistry
from agent.core.types import Project
from agent.detection import detect_project
from agent.plugins import register_builtins

__all__ = [
    "Container",
    "PluginRegistry",
    "Project",
    "detect_project",
    "register_builtins",
]


def default_registry(container: Container | None = None) -> PluginRegistry:
    """Registry with builtin plugins registered and wired to live services
    (convenience entry point; pass a ``Container`` to substitute them)."""
    registry = PluginRegistry()
    register_builtins(registry, container if container is not None else default_container())
    return registry
