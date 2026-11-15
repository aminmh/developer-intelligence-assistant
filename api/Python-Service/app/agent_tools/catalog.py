"""How each tool looks to the model, for the LLM Tools settings card.

The card shows, for every built-in and managed tool, whether the model can call
it, whether calls run directly or wait for approval, which workflows may use
it, and, when the model cannot use it, the reason in plain words.
"""

from __future__ import annotations

from typing import Any

from agent_tools.builtins import builtin_tools
from agent_tools.managed import to_definition
from agent_tools.types import EFFECT_READ_ONLY, USER_WORKFLOWS, ToolDefinition, ToolDefinitionError
from agent_tools.validation import normalize_effect


def _workflows(values: Any) -> list[str]:
    return sorted(values) if values is not None else sorted(USER_WORKFLOWS)


def _builtin_entry(definition: ToolDefinition) -> dict[str, Any]:
    return {
        "id": f"builtin:{definition.name}", "type": "builtin", "name": definition.name,
        "description": definition.description, "enabled": True, "builtin": True, "config": {},
        "agent": {"callable": True, "reason": None, "effect": definition.effect,
                  "workflows": _workflows(definition.workflows), "input_schema": definition.input_schema},
    }


def agent_status(record: dict[str, Any], builtin_names: set[str] | None = None) -> dict[str, Any]:
    """Model access for one managed tool record."""
    config = record.get("config") or {}
    names = builtin_names if builtin_names is not None else {d.name for d in builtin_tools()}
    status: dict[str, Any] = {"callable": False, "reason": None, "effect": normalize_effect(config.get("effect")),
                              "workflows": _workflows(config.get("workflows")),
                              "input_schema": config.get("inputSchema")}
    if record.get("deleted_at"):
        status["reason"] = "Deleted."
    elif not record.get("enabled"):
        status["reason"] = "Disabled."
    elif record.get("name") in names:
        status["reason"] = "A built-in tool already uses this name."
    else:
        try:
            to_definition(record)
            status["callable"] = True
        except ToolDefinitionError as exc:
            status["reason"] = str(exc)
    return status


def pipeline_entries(orchestrator_tools: dict[str, Any]) -> list[dict[str, Any]]:
    """Indexing-pipeline tools: run by the orchestrator, never offered to the model."""
    return [{
        "id": f"pipeline:{name}", "type": "pipeline", "name": name,
        "description": (cfg or {}).get("description") or name, "enabled": True, "builtin": True, "config": {},
        "agent": {"callable": False, "reason": "Runs inside the indexing pipeline; not offered to the model.",
                  "effect": EFFECT_READ_ONLY, "workflows": [], "input_schema": None},
    } for name, cfg in sorted(orchestrator_tools.items())]


def builtin_entries() -> list[dict[str, Any]]:
    return [_builtin_entry(d) for d in sorted(builtin_tools(), key=lambda d: d.name)]
