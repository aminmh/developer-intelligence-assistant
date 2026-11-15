"""Bounded execution of one validated model tool call.

``execute_call`` never raises: rejections, failures, and timeouts all become a
bounded error result that goes back to the model, as the spec's failure rules
require. It does not decide approval; the agent runtime pauses side-effecting
calls before it reaches this function.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from agent_tools.limits import RunBudget, ToolLimits, effective_output_bytes, effective_timeout
from agent_tools.types import ToolCallRejected, ToolContext, ToolDefinition

logger = logging.getLogger(__name__)

_MAX_ERROR_CHARS = 500
# Daemon-free pool: a timed-out handler keeps its worker until it returns, so
# handlers that call subprocesses must enforce their own timeouts as well.
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="agent-tool")


def _serialize(output: Any) -> str:
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(output)


def bound_output(text: str, max_bytes: int) -> tuple[str, bool]:
    """Cut text to ``max_bytes`` UTF-8 bytes on a character boundary."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _error(tool: str, code: str, message: str, started: float) -> dict[str, Any]:
    return {
        "ok": False,
        "tool": tool,
        "error": {"code": code, "message": message[:_MAX_ERROR_CHARS]},
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
    }


def execute_call(
    definition: ToolDefinition,
    arguments: dict[str, Any],
    context: ToolContext,
    limits: ToolLimits,
    budget: RunBudget | None = None,
) -> dict[str, Any]:
    """Run one call within the tool's timeout and output limits.

    Arguments must already be validated by the registry.
    Returns ``{"ok", "tool", "output" | "error", "truncated", "duration_ms"}``.
    """
    started = time.monotonic()
    try:
        if budget is not None:
            budget.consume()
    except ToolCallRejected as exc:
        return _error(definition.name, exc.code, exc.message, started)

    timeout = effective_timeout(limits, definition.timeout_seconds)
    future = _pool.submit(definition.handler, arguments, context)
    try:
        output = future.result(timeout=timeout)
    except FutureTimeout:
        future.cancel()
        return _error(definition.name, "timeout", f"The tool did not finish within {timeout:g} seconds.", started)
    except ToolCallRejected as exc:
        return _error(definition.name, exc.code, exc.message, started)
    except Exception as exc:  # noqa: BLE001 — every failure becomes a bounded result
        logger.warning("Tool %s failed: %s", definition.name, exc)
        return _error(definition.name, "tool_failed", str(exc) or exc.__class__.__name__, started)

    text, truncated = bound_output(_serialize(output), effective_output_bytes(limits, definition.max_output_bytes))
    return {
        "ok": True,
        "tool": definition.name,
        "output": text,
        "truncated": truncated,
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
    }
