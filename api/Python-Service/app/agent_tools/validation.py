"""Validation of tool definitions and model-supplied tool arguments."""

from __future__ import annotations

import json
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from agent_tools.types import (
    EFFECT_SIDE_EFFECTING,
    EFFECTS,
    USER_WORKFLOWS,
    ToolCallRejected,
    ToolDefinitionError,
)

# OpenAI function names: letters, digits, underscore, hyphen; 1–64 characters.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_DESCRIPTION_CHARS = 1024
_MAX_SCHEMA_BYTES = 16_384
_MAX_ARGUMENT_BYTES = 32_768
_MAX_ERROR_CHARS = 300


def validate_name(name: Any) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ToolDefinitionError(
            "Tool name must be 1–64 letters, digits, underscores, or hyphens."
        )
    return name


def normalize_effect(effect: Any) -> str:
    """Unknown or missing classifications are side-effecting (spec)."""
    return effect if effect in EFFECTS else EFFECT_SIDE_EFFECTING


def validate_description(description: Any) -> str:
    text = str(description or "").strip()
    if not text:
        raise ToolDefinitionError("Tool description is required.")
    return text[:_MAX_DESCRIPTION_CHARS]


def validate_input_schema(schema: Any) -> dict[str, Any]:
    """Accept only a bounded JSON Schema describing an object of arguments."""
    if not isinstance(schema, dict):
        raise ToolDefinitionError("Tool argument schema must be a JSON object.")
    if len(json.dumps(schema)) > _MAX_SCHEMA_BYTES:
        raise ToolDefinitionError("Tool argument schema is too large.")
    if schema.get("type") != "object":
        raise ToolDefinitionError('Tool argument schema must have "type": "object".')
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ToolDefinitionError(f"Invalid tool argument schema: {exc.message[:_MAX_ERROR_CHARS]}") from exc
    return schema


def validate_workflows(workflows: Any) -> frozenset[str]:
    """``None`` means every user-facing workflow; otherwise known names only."""
    if workflows is None:
        return USER_WORKFLOWS
    if not isinstance(workflows, (list, tuple, set, frozenset)):
        raise ToolDefinitionError("Tool workflows must be a list of workflow names.")
    unknown = sorted(str(w) for w in workflows if w not in USER_WORKFLOWS)
    if unknown:
        raise ToolDefinitionError(f"Unknown workflows: {', '.join(unknown)}")
    return frozenset(workflows)


def validate_arguments(schema: dict[str, Any], arguments: Any) -> dict[str, Any]:
    """Parse and validate model-supplied arguments; raise ``ToolCallRejected``."""
    if isinstance(arguments, str):
        if len(arguments.encode("utf-8")) > _MAX_ARGUMENT_BYTES:
            raise ToolCallRejected("arguments_too_large", "Tool arguments are too large.")
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError as exc:
            raise ToolCallRejected("invalid_arguments", "Tool arguments are not valid JSON.") from exc
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolCallRejected("invalid_arguments", "Tool arguments must be a JSON object.")
    if len(json.dumps(arguments, default=str).encode("utf-8")) > _MAX_ARGUMENT_BYTES:
        raise ToolCallRejected("arguments_too_large", "Tool arguments are too large.")
    errors = sorted(Draft202012Validator(schema).iter_errors(arguments), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.path) or "(root)"
        raise ToolCallRejected(
            "invalid_arguments", f"Tool arguments failed validation at {where}: {first.message[:_MAX_ERROR_CHARS]}"
        )
    return arguments
