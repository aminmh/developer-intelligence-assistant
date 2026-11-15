"""Configured limits for model tool calls (``agent_tools`` in orchestrator.yml)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from agent_tools.types import ToolCallRejected

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "orchestrator.yml"

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_OUTPUT_BYTES = 65_536
DEFAULT_MAX_CALLS_PER_RUN = 12
# Hard ceilings: configuration cannot raise a limit past these.
MAX_TIMEOUT_SECONDS = 300.0
MAX_OUTPUT_BYTES = 1_048_576
MAX_CALLS_PER_RUN = 50


MAX_REQUEST_TOKENS = 1_000_000


@dataclass(frozen=True)
class ToolLimits:
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_calls_per_run: int = DEFAULT_MAX_CALLS_PER_RUN
    # Provider limit on one model request (for example Groq's free tier allows
    # 8,000 tokens per minute); 0 means no limit. See request_budget().
    max_request_tokens: int = 0

    def request_budget(self) -> "ToolLimits":
        """Limits adjusted so one agent request stays under ``max_request_tokens``.

        Tool output is capped to about 30% of the budget (roughly 4 bytes per
        token), so the newest tool result always fits next to the prompt.
        """
        if self.max_request_tokens <= 0:
            return self
        cap = max(4_096, int(self.max_request_tokens * 4 * 0.3))
        return replace(self, max_output_bytes=min(self.max_output_bytes, cap))


def _bounded(value: Any, default: float, ceiling: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return min(number, ceiling)


def load_limits(path: Path | None = None) -> ToolLimits:
    """Read limits from the orchestrator config; fall back to defaults."""
    try:
        data = yaml.safe_load((path or _CONFIG_PATH).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    section = data.get("agent_tools") if isinstance(data, dict) else None
    section = section if isinstance(section, dict) else {}
    return ToolLimits(
        timeout_seconds=_bounded(section.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS),
        max_output_bytes=int(_bounded(section.get("max_output_bytes"), DEFAULT_MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES)),
        max_calls_per_run=int(_bounded(section.get("max_calls_per_run"), DEFAULT_MAX_CALLS_PER_RUN, MAX_CALLS_PER_RUN)),
        max_request_tokens=int(_bounded(section.get("max_request_tokens"), 0, MAX_REQUEST_TOKENS)),
    )


def effective_timeout(limits: ToolLimits, tool_timeout: float | None) -> float:
    """A tool may ask for less time than the global limit, never more."""
    if tool_timeout and tool_timeout > 0:
        return min(float(tool_timeout), limits.timeout_seconds)
    return limits.timeout_seconds


def effective_output_bytes(limits: ToolLimits, tool_bytes: int | None) -> int:
    if tool_bytes and tool_bytes > 0:
        return min(int(tool_bytes), limits.max_output_bytes)
    return limits.max_output_bytes


class RunBudget:
    """Counts tool calls in one agent run and stops at the configured limit."""

    def __init__(self, max_calls: int) -> None:
        self.max_calls = max_calls
        self.used = 0

    @property
    def exhausted(self) -> bool:
        return self.used >= self.max_calls

    def consume(self) -> None:
        if self.exhausted:
            raise ToolCallRejected(
                "tool_call_limit", f"The run reached its limit of {self.max_calls} tool calls."
            )
        self.used += 1
