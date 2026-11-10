"""Built-in tools the application defines in code.

Only read operations of the built-in PHP tools are model-callable. The
``chunk_code`` orchestrator tool ingests into Qdrant and Neo4j, so it stays an
orchestrator-only step and has no definition here.
"""

from __future__ import annotations

from typing import Any

from agent_tools.types import EFFECT_READ_ONLY, SOURCE_BUILTIN, USER_WORKFLOWS, ToolContext, ToolDefinition

_STRUCTURE_MAX_BYTES = 8000


def _project_structure(arguments: dict[str, Any], context: ToolContext) -> str:
    from agent_tools.source import resolve_source
    from infra.php_tools import cap_structure, run_export_structure

    root = resolve_source(context)
    depth = int(arguments.get("depth", 4))
    return cap_structure(run_export_structure(str(root.path), depth), max_bytes=_STRUCTURE_MAX_BYTES)


PROJECT_STRUCTURE = ToolDefinition(
    name="project_structure",
    description=(
        "Return the project's directory and file structure as Markdown, excluding vendor, build, "
        "and cache directories. Your instructions already include a two-level layout; call this only "
        "for deeper levels, and pick the depth you need in one call."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 6,
                "default": 4,
                "description": "How many directory levels to include.",
            }
        },
        "additionalProperties": False,
    },
    effect=EFFECT_READ_ONLY,
    source=SOURCE_BUILTIN,
    workflows=USER_WORKFLOWS,
    handler=_project_structure,
    timeout_seconds=60,
)


def builtin_tools() -> list[ToolDefinition]:
    from agent_tools.navigation import NAVIGATION_TOOLS

    return [PROJECT_STRUCTURE, *NAVIGATION_TOOLS]
