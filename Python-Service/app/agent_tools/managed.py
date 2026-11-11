"""Conversion of user-managed tool records into model-callable definitions.

A managed record becomes callable only when it carries everything the spec
requires: a valid name, description, and explicit JSON argument schema, plus an
adapter that can run it. Records that miss something are reported as skipped
with the reason, and stay usable on the orchestrator's existing path.
"""

from __future__ import annotations

from typing import Any

from agent_tools.types import SOURCE_MANAGED, ToolCallRejected, ToolContext, ToolDefinition, ToolDefinitionError
from agent_tools.validation import (
    normalize_effect,
    validate_description,
    validate_input_schema,
    validate_name,
    validate_workflows,
)

# MCP and shell tools need the isolated tool runner (LangChain plan tasks 12,
# 12a, 13); until it exists they are not offered to the model.
_RUNNER_REQUIRED = {
    "mcp": "MCP tools need the isolated tool runner, which is not available yet.",
    "shell": "Shell tools need the isolated tool runner, which is not available yet.",
}


def _rest_handler(record: dict[str, Any]):
    def handler(arguments: dict[str, Any], context: ToolContext) -> Any:
        from infra.tool_executor import execute_tool

        result = execute_tool(record, filename=context.filename, arguments=arguments)
        if not result.get("success"):
            raise ToolCallRejected("tool_failed", str(result.get("error") or f"HTTP {result.get('status_code')}"))
        return {"status_code": result.get("status_code"), "body": result.get("result")}

    return handler


def to_definition(record: dict[str, Any]) -> ToolDefinition:
    """Build a definition or raise ``ToolDefinitionError`` explaining why not."""
    kind = record.get("type")
    if kind in _RUNNER_REQUIRED:
        raise ToolDefinitionError(_RUNNER_REQUIRED[kind])
    if kind != "rest":
        raise ToolDefinitionError(f"Unsupported tool type: {kind}")
    config = record.get("config") or {}
    if "inputSchema" not in config:
        raise ToolDefinitionError("REST tools need an explicit JSON argument schema (config.inputSchema).")
    timeout = config.get("timeoutSeconds")
    return ToolDefinition(
        name=validate_name(record.get("name")),
        description=validate_description(record.get("description")),
        input_schema=validate_input_schema(config["inputSchema"]),
        effect=normalize_effect(config.get("effect")),
        source=SOURCE_MANAGED,
        workflows=validate_workflows(config.get("workflows")),
        handler=_rest_handler(record),
        tool_id=record.get("id"),
        timeout_seconds=float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else None,
        version=f"managed {record['updated_at']}" if record.get("updated_at") else "managed",
    )
