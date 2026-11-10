"""Shared types for model-callable tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

EFFECT_READ_ONLY = "read_only"
EFFECT_SIDE_EFFECTING = "side_effecting"
EFFECTS = frozenset({EFFECT_READ_ONLY, EFFECT_SIDE_EFFECTING})

# User-facing workflows from the LangChain orchestration spec, named by the
# router intents that drive them.
USER_WORKFLOWS = frozenset(
    {"chat", "structure_analyze", "code_explain", "debug", "code_refactor", "knowledge", "search"}
)

SOURCE_BUILTIN = "builtin"
SOURCE_MANAGED = "managed"


class ToolCallRejected(Exception):
    """A tool call the application refuses to execute; ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ToolDefinitionError(ValueError):
    """A tool definition that cannot be exposed to the model."""


@dataclass(frozen=True)
class ToolContext:
    """What a tool may act on for one agent run."""

    workflow: str
    project_id: str | None = None
    revision_id: str | None = None
    workspace_path: str | None = None
    filename: str = ""
    commit_sha: str | None = None


Handler = Callable[[dict[str, Any], ToolContext], Any]


@dataclass(frozen=True)
class ToolDefinition:
    """One validated tool the agent runtime may offer to a model.

    ``effect`` decides approval: read-only calls run directly, side-effecting
    calls wait for the user. ``workflows`` is the allowlist of workflows that
    may see the tool.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    effect: str
    source: str
    workflows: frozenset[str]
    handler: Handler = field(compare=False, repr=False)
    tool_id: str | None = None
    timeout_seconds: float | None = None
    max_output_bytes: int | None = None

    def model_schema(self) -> dict[str, Any]:
        """OpenAI-style function schema; carries no credentials or config."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }
