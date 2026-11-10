"""Registry of model-callable tools and the gate every tool call passes.

Managed tools are re-read on every lookup, so disabling or deleting a tool takes
effect for the next call of a running agent as well (spec clause 7).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent_tools.builtins import builtin_tools
from agent_tools.managed import to_definition
from agent_tools.types import USER_WORKFLOWS, ToolCallRejected, ToolDefinition, ToolDefinitionError
from agent_tools.validation import validate_arguments

ManagedLoader = Callable[[], list[dict[str, Any]]]


@dataclass(frozen=True)
class SkippedTool:
    name: str
    tool_id: str | None
    reason: str


def _default_loader() -> list[dict[str, Any]]:
    from infra.tool_store import tool_store

    return tool_store.list()


class ToolRegistry:
    def __init__(
        self,
        builtins: list[ToolDefinition] | None = None,
        managed_loader: ManagedLoader | None = None,
    ) -> None:
        self._builtins = {t.name: t for t in (builtin_tools() if builtins is None else builtins)}
        self._load_managed = managed_loader or _default_loader

    def _snapshot(self) -> tuple[dict[str, ToolDefinition], set[str], list[SkippedTool]]:
        """Return (callable tools by name, names of disabled managed tools, skipped)."""
        tools = dict(self._builtins)
        disabled: set[str] = set()
        skipped: list[SkippedTool] = []
        for record in self._load_managed():
            name = str(record.get("name") or "")
            if record.get("deleted_at"):
                continue
            if not record.get("enabled"):
                disabled.add(name)
                continue
            if name in tools:
                skipped.append(SkippedTool(name, record.get("id"), "The name is already used by another tool."))
                continue
            try:
                tools[name] = to_definition(record)
            except ToolDefinitionError as exc:
                skipped.append(SkippedTool(name, record.get("id"), str(exc)))
        return tools, disabled, skipped

    def tools_for(self, workflow: str) -> list[ToolDefinition]:
        """Enabled tools the workflow may use, sorted by name."""
        if workflow not in USER_WORKFLOWS:
            return []
        tools, _, _ = self._snapshot()
        return sorted((t for t in tools.values() if workflow in t.workflows), key=lambda t: t.name)

    def skipped_tools(self) -> list[SkippedTool]:
        """Managed tools that are enabled but cannot be offered to the model."""
        return self._snapshot()[2]

    def resolve_call(self, workflow: str, name: str, arguments: Any) -> tuple[ToolDefinition, dict[str, Any]]:
        """Return the definition and validated arguments, or raise ``ToolCallRejected``."""
        tools, disabled, skipped = self._snapshot()
        definition = tools.get(name)
        if definition is None:
            if name in disabled:
                raise ToolCallRejected("tool_disabled", f"The tool {name} is disabled.")
            if any(s.name == name for s in skipped):
                raise ToolCallRejected("tool_unavailable", f"The tool {name} cannot be called by the model.")
            raise ToolCallRejected("unknown_tool", f"There is no tool named {name[:64]}.")
        if workflow not in definition.workflows:
            raise ToolCallRejected("not_allowed", f"The tool {name} is not allowed in the {workflow} workflow.")
        return definition, validate_arguments(definition.input_schema, arguments)
