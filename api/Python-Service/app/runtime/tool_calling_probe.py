"""Native tool-calling probe for the remote OpenAI-compatible model.

An endpoint that speaks the OpenAI chat API does not necessarily implement
tool calls (vLLM, for example, needs ``--enable-auto-tool-choice`` and a
``--tool-call-parser``). The agent runtime only exposes tools to a model that
completed one real round trip here: the model calls a stub tool on its own
(``tool_choice="auto"``), and the endpoint then accepts the tool result.

Results are stored per remote base URL and model, so changing either one
requires a new connection test before tools are offered again.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

PROBE_TOOL_NAME = "report_status"
PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": PROBE_TOOL_NAME,
        "description": "Report the connection status back to the application.",
        "parameters": {
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["ok"]}},
            "required": ["status"],
            "additionalProperties": False,
        },
    },
}
_PROBE_PROMPT = (
    f'Call the {PROBE_TOOL_NAME} tool with status "ok". '
    "Do not answer with text."
)
# Reasoning models spend tokens before the call; 256 leaves room without
# making the probe expensive.
_MAX_TOKENS = 256
_MAX_DETAIL_CHARS = 300
_STORE_KEY = "llmRemoteToolCallingProbes"
_MAX_STORED_PROBES = 20


def _detail(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:_MAX_DETAIL_CHARS]


def _result(supported: bool, detail: str) -> dict:
    return {"supported": supported, "detail": detail}


def probe_tool_calling(client: Any, model: str, timeout: float = 30.0) -> dict:
    """Run one stub-tool round trip and return ``{"supported", "detail"}``.

    Never raises: request errors, a text-only reply, a wrong tool name, or
    malformed arguments all report ``supported: False`` with the reason.
    """
    messages: list[dict] = [{"role": "user", "content": _PROBE_PROMPT}]
    try:
        first = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=[PROBE_TOOL],
            tool_choice="auto",
            max_tokens=_MAX_TOKENS,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — any provider error means unsupported
        return _result(False, f"The endpoint rejected a request with tools: {_detail(exc)}")

    message = first.choices[0].message if getattr(first, "choices", None) else None
    calls = list(getattr(message, "tool_calls", None) or [])
    if not calls:
        return _result(False, "The model answered with text instead of calling the tool.")

    call = calls[0]
    function = getattr(call, "function", None)
    name = getattr(function, "name", None)
    if name != PROBE_TOOL_NAME:
        return _result(False, f"The model called an unknown tool: {str(name)[:80]}")
    raw_arguments = getattr(function, "arguments", None) or "{}"
    try:
        arguments = json.loads(raw_arguments)
    except (TypeError, ValueError):
        return _result(False, "The tool arguments were not valid JSON.")
    if not isinstance(arguments, dict) or arguments.get("status") != "ok":
        return _result(False, "The tool arguments did not match the tool schema.")

    messages.append(
        {
            "role": "assistant",
            "content": getattr(message, "content", None),
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": PROBE_TOOL_NAME, "arguments": raw_arguments},
                }
            ],
        }
    )
    messages.append(
        {"role": "tool", "tool_call_id": call.id, "content": json.dumps({"received": True})}
    )
    try:
        client.chat.completions.create(
            model=model,
            messages=messages,
            tools=[PROBE_TOOL],
            max_tokens=_MAX_TOKENS,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return _result(False, f"The endpoint rejected the tool result: {_detail(exc)}")
    return _result(True, "Native tool calls supported.")


def probe_key(base_url: str, model: str) -> str:
    """Identify a probe by normalized base URL (without ``/v1``) and model."""
    base = (base_url or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return f"{base}|{model}"


def _store(store: Any = None) -> Any:
    if store is not None:
        return store
    from infra.settings_store import settings_store

    return settings_store


def record_probe(base_url: str, model: str, result: dict, store: Any = None) -> dict:
    """Persist a probe result; keeps the most recent ``_MAX_STORED_PROBES``."""
    target = _store(store)
    entry = {
        "supported": bool(result.get("supported")),
        "detail": str(result.get("detail") or "")[:_MAX_DETAIL_CHARS],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    probes = dict(target.get(_STORE_KEY) or {})
    key = probe_key(base_url, model)
    probes.pop(key, None)
    probes[key] = entry
    while len(probes) > _MAX_STORED_PROBES:
        probes.pop(next(iter(probes)))
    target.set(_STORE_KEY, probes)
    return entry


def lookup_probe(base_url: str, model: str, store: Any = None) -> Optional[dict]:
    """Return the stored probe for this base URL and model, or None if untested."""
    probes = _store(store).get(_STORE_KEY) or {}
    entry = probes.get(probe_key(base_url, model))
    return dict(entry) if isinstance(entry, dict) else None
