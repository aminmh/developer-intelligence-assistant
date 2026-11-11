"""Record an agent run's tool calls in the answer's retrieval trace.

The retrieval trace (``record_retrieval_trace``) already keeps what retrieval
found for one answer, keyed by the ``X-RAG-Trace-Id`` header. This adds an
``agent`` section with every tool call, the version of the tool that ran, and
the source revision the tools read, so an answer can be traced back to exactly
what the model saw.
"""

from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any

from agent_tools.types import SOURCE_BUILTIN, ToolCallRejected, ToolContext, ToolDefinition

# Built-in tools backed by an external program or package, and how to ask its version.
_BINARY_BACKENDS = {
    "code_text_search": ("ripgrep", ["rg", "--version"]),
    "code_structural_search": ("ast-grep", ["ast-grep", "--version"]),
    "code_stats": ("scc", ["scc", "--version"]),
}
_PACKAGE_BACKENDS = {"code_outline": "tree-sitter-language-pack"}
_MAX_ARGUMENT_CHARS = 500


@lru_cache(maxsize=None)
def _backend_version(tool: str) -> str | None:
    """``"ripgrep 13.0.0"`` and the like; None when the tool has no backend or it is missing."""
    if tool in _PACKAGE_BACKENDS:
        package = _PACKAGE_BACKENDS[tool]
        try:
            return f"{package} {metadata.version(package)}"
        except metadata.PackageNotFoundError:
            return None
    if tool not in _BINARY_BACKENDS:
        return None
    label, argv = _BINARY_BACKENDS[tool]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    words = out.split()
    # "ripgrep 13.0.0 (rev …)", "ast-grep 0.51.1", "scc version 4.1.0"
    version = next((w for w in words if w[:1].isdigit()), None)
    return f"{label} {version}" if version else None


def tool_version(definition: ToolDefinition | None, name: str) -> str:
    if definition is None:
        return "unknown"
    if definition.version:
        return definition.version
    if definition.source == SOURCE_BUILTIN:
        backend = _backend_version(name)
        return f"builtin; {backend}" if backend else "builtin"
    return definition.source


def revision_identity(context: ToolContext, *, resolve: bool) -> dict[str, Any]:
    """The project revision the tools read; ``resolve`` also names the source root they used."""
    identity: dict[str, Any] = {"project_id": context.project_id, "revision_id": context.revision_id,
                                "commit_sha": context.commit_sha, "workflow": context.workflow}
    if not resolve:
        return identity
    from agent_tools.source import resolve_source

    try:
        root = resolve_source(context)
    except ToolCallRejected as exc:
        return {**identity, "source": None, "source_error": exc.code}
    except Exception:  # the trace never fails an answer
        return {**identity, "source": None, "source_error": "unavailable"}
    identity.update(source=root.origin, revision_id=root.revision_id or context.revision_id,
                    archive_sha256=root.archive_sha256, project_root=root.wrapper_prefix or ".")
    if root.origin == "workspace":
        identity["workspace"] = Path(root.path).name
    return identity


def _arguments(value: Any) -> Any:
    text = json.dumps(value or {}, ensure_ascii=False, default=str)
    if len(text) <= _MAX_ARGUMENT_CHARS:
        return value or {}
    return text[:_MAX_ARGUMENT_CHARS] + "…"


def trace_calls(tool_calls: list[dict[str, Any]], definitions: dict[str, ToolDefinition]) -> list[dict[str, Any]]:
    rows = []
    for call in tool_calls:
        name = str(call.get("name") or "")
        definition = definitions.get(name)
        rows.append({
            "id": call.get("id"), "name": name, "arguments": _arguments(call.get("arguments")),
            "status": call.get("status"), "error": call.get("error"),
            "duration_ms": call.get("duration_ms"), "truncated": bool(call.get("truncated")),
            "source": definition.source if definition else None,
            "effect": definition.effect if definition else None,
            "version": tool_version(definition, name),
        })
    return rows


def record_tool_trace(
    trace_id: str | None,
    context: ToolContext,
    tool_calls: list[dict[str, Any]],
    definitions: dict[str, ToolDefinition],
    *,
    status: str,
    model: str | None,
    approval_id: str | None = None,
) -> None:
    """Merge the run's tool calls into the answer's retrieval trace. Best-effort."""
    if not trace_id:
        return
    try:
        from index.llamaindex_layer import get_retrieval_trace, record_retrieval_trace

        trace = get_retrieval_trace(trace_id) or {}
        agent = {"status": status, "model": model,
                 "revision": revision_identity(context, resolve=bool(tool_calls)),
                 "tool_calls": trace_calls(tool_calls, definitions)}
        if approval_id:
            agent["approval_id"] = approval_id
        record_retrieval_trace(trace_id, {**trace, "agent": agent})
    except Exception as exc:  # noqa: BLE001 — tracing must not break the answer
        from infra.exception_logger import get_exception_logger

        get_exception_logger().warning("Tool trace write failed: %s", exc)
