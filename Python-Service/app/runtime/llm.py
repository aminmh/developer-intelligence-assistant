"""Shared LLM client backed by Ollama's OpenAI-compatible endpoint, with an
optional remote vLLM (OpenAI-compatible) provider for heavy answers.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, List, Optional, Tuple
from urllib.parse import urlsplit
import urllib.request

import instructor
from openai import OpenAI

from infra.settings import settings

logger = logging.getLogger(__name__)

_openai_client: Optional[Tuple[str, OpenAI]] = None
_extract_client_state: Optional[Tuple[str, OpenAI]] = None
_instructor_client: Optional[Any] = None

# (base_url, api_key, client) — re-created whenever either the dynamic base_url
# or api_key setting changes.
_remote_client_state: Optional[Tuple[str, str, OpenAI]] = None

# Ollama's OpenAI-compatible endpoint does not reliably forward the native
# ``keep_alive`` field. Keep the normal OpenAI path, then use Ollama's native
# unload request after an idle period so model memory is actually released.
_local_unload_timer: Optional[threading.Timer] = None
_local_unload_lock = threading.Lock()


def _keep_alive_seconds() -> float | None:
    """Parse ``OLLAMA_KEEP_ALIVE`` into seconds.

    ``None`` means unlimited (``-1``), while zero unloads immediately. Invalid
    values fall back to Ollama's documented five-minute default.
    """
    raw = str(getattr(settings, "ollama_keep_alive", "5m") or "5m").strip().lower()
    if raw in {"-1", "infinite", "forever"}:
        return None
    if raw == "0":
        return 0.0

    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)?", raw)
    if not match:
        return 300.0
    value = float(match.group(1))
    multiplier = {
        "ms": 0.001,
        "s": 1.0,
        "m": 60.0,
        "h": 3600.0,
        "d": 86400.0,
    }.get(match.group(2) or "s", 1.0)
    return max(0.0, value * multiplier)


def _cancel_local_model_unload() -> None:
    """Cancel an idle unload before starting another local Ollama request."""
    global _local_unload_timer
    with _local_unload_lock:
        if _local_unload_timer is not None:
            _local_unload_timer.cancel()
            _local_unload_timer = None


def _unload_local_model(model: str) -> None:
    """Ask Ollama's native API to unload one model from memory."""
    global _local_unload_timer
    with _local_unload_lock:
        _local_unload_timer = None

    body = json.dumps(
        {"model": model, "prompt": "", "stream": False, "keep_alive": 0}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{settings.ollama_base_url.rstrip('/')}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15):
            pass
    except Exception:
        # Unloading is best-effort; the next Ollama request remains healthy if
        # the server is unavailable or the model has already been evicted.
        pass


def _schedule_local_model_unload(model: str, enabled: bool = True) -> None:
    """Schedule an idle unload, replacing any older pending timer."""
    global _local_unload_timer
    if not enabled:
        _cancel_local_model_unload()
        return
    delay = _keep_alive_seconds()
    if delay is None:
        _cancel_local_model_unload()
        return
    if delay <= 0:
        _unload_local_model(model)
        return

    timer = threading.Timer(delay, _unload_local_model, args=(model,))
    timer.daemon = True
    with _local_unload_lock:
        if _local_unload_timer is not None:
            _local_unload_timer.cancel()
        _local_unload_timer = timer
    timer.start()


class CompletionResult(tuple):
    """Backward-compatible ``(content, model, provider)`` plus usage metadata."""

    def __new__(cls, content: str, model: str, provider: str, usage: dict | None = None):
        value = super().__new__(cls, (content, model, provider))
        value.usage = usage
        return value


class StreamChunk(tuple):
    """Backward-compatible stream tuple with optional final usage metadata."""

    def __new__(cls, delta: str, model: str, provider: str, usage: dict | None = None):
        value = super().__new__(cls, (delta, model, provider))
        value.usage = usage
        return value


def _usage_value(usage: Any, name: str) -> int | None:
    if usage is None:
        return None
    if isinstance(usage, dict):
        value = usage.get(name)
    else:
        value = getattr(usage, name, None)
    return int(value) if isinstance(value, (int, float)) else None


def normalize_usage(usage: Any) -> dict | None:
    """Normalize OpenAI-compatible usage objects into the API shape."""
    prompt = _usage_value(usage, "prompt_tokens")
    completion = _usage_value(usage, "completion_tokens")
    total = _usage_value(usage, "total_tokens")
    if prompt is None and completion is None and total is None:
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total if total is not None else (prompt or 0) + (completion or 0),
        "estimated": False,
        "source": "provider",
    }


def _get_openai_client() -> OpenAI:
    """Return a lazily-created raw OpenAI client for the LOCAL chat provider.

    Points at the local LLM server when configured (``localLlmUrl``, e.g. an
    LM Studio/Bionic OpenAI-compatible endpoint), otherwise falls back to the
    Ollama server's /v1 endpoint. The cache is keyed on the base URL so a
    changed setting re-creates the client on the next call.
    """
    global _openai_client
    base_url = (settings.local_llm_url or settings.ollama_base_url).rstrip("/")
    if isinstance(_openai_client, tuple) and _openai_client[0] == base_url:
        return _openai_client[1]
    _openai_client = (base_url, OpenAI(base_url=f"{base_url}/v1", api_key="ollama"))
    return _openai_client[1]


def _get_extract_client() -> OpenAI:
    """Return a lazily-created raw OpenAI client pinned to the Ollama server.

    Structured extraction (JSON mode via instructor) and route classification
    stay on Ollama even when the local chat provider points elsewhere.
    """
    global _extract_client_state
    base_url = settings.ollama_base_url.rstrip("/")
    if _extract_client_state is None or _extract_client_state[0] != base_url:
        _extract_client_state = (base_url, OpenAI(base_url=f"{base_url}/v1", api_key="ollama"))
    return _extract_client_state[1]


def get_client():
    """Return a lazily-created Instructor client (JSON mode) for structured output."""
    global _instructor_client
    if _instructor_client is None:
        _instructor_client = instructor.from_openai(
            _get_extract_client(), mode=instructor.Mode.JSON
        )
    return _instructor_client


def extract(response_model, prompt: str):
    """Run structured extraction, returning a validated `response_model` instance.

    Raises on LLM/parse failure — callers are expected to degrade gracefully.
    """
    client = get_client()
    model = settings.ollama_llm_model
    _cancel_local_model_unload()
    try:
        return client.chat.completions.create(
            model=model,
            response_model=response_model,
            messages=[{"role": "user", "content": prompt}],
            max_retries=1,
        )
    finally:
        _schedule_local_model_unload(model)


def _get_remote_client() -> OpenAI:
    """Return a lazily-created OpenAI client for the remote OpenAI-compatible server.

    The cache is keyed on (base_url, api_key) because settings are dynamic
    (SQLite-backed); a changed URL or key re-creates the client on the next call.

    A trailing ``/v1`` is tolerated so users may paste full provider URLs; the
    endpoint path is always normalised to ``<base>/v1`` for the OpenAI SDK.
    """
    global _remote_client_state
    base_url = settings.llm_remote_base_url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[: -len("/v1")]
    base_url = f"{base_url}/v1"
    api_key = settings.llm_remote_api_key or "EMPTY"
    if _remote_client_state is None or (
        _remote_client_state[0] != base_url or _remote_client_state[1] != api_key
    ):
        _remote_client_state = (base_url, api_key, OpenAI(base_url=base_url, api_key=api_key))
    return _remote_client_state[2]


def remote_configured() -> bool:
    """Return True when a remote OpenAI-compatible base URL is configured."""
    return bool((settings.llm_remote_base_url or "").strip())


def test_remote(timeout: float = 30.0) -> dict:
    """Probe the configured remote OpenAI-compatible endpoint.

    Returns ``{"ok": bool, "latency_ms": float, "detail": str, "model": str}``.
    Raises nothing — connection/auth/model errors are folded into ``ok``.
    """
    if not remote_configured():
        return {"ok": False, "latency_ms": 0.0, "detail": "No base URL configured", "model": ""}
    model = settings.llm_remote_model or settings.ollama_llm_model
    started = time.monotonic()
    try:
        _run_chat(
            _get_remote_client(),
            model,
            "Reply with the single word: ok",
            max_tokens=1,
        )
        latency_ms = (time.monotonic() - started) * 1000.0
        return {"ok": True, "latency_ms": round(latency_ms, 1), "detail": "connected", "model": model}
    except Exception as exc:  # noqa: BLE001 — report any failure shape to the UI
        latency_ms = (time.monotonic() - started) * 1000.0
        detail = str(exc).strip() or exc.__class__.__name__
        return {"ok": False, "latency_ms": round(latency_ms, 1), "detail": detail, "model": model}


def _run_chat(
    client: OpenAI,
    model: str,
    prompt: str,
    system: Optional[str] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    max_tokens: int = 2048,
    response_format: Optional[dict] = None,
    extra_body: Optional[dict] = None,
    timeout: float | None = None,
) -> tuple[str, dict | None]:
    """Run a chat completion and return text plus normalized provider usage."""
    messages: List[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if top_p is not None:
        kwargs["top_p"] = top_p
    if response_format is not None:
        kwargs["response_format"] = response_format
    if extra_body is not None:
        kwargs["extra_body"] = extra_body
    if timeout is not None:
        kwargs["timeout"] = timeout

    response = client.chat.completions.create(**kwargs)
    content = response.choices[0].message.content if response.choices else ""
    return content or "", normalize_usage(getattr(response, "usage", None))


def complete_with_model(
    prompt: str,
    system: Optional[str] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    max_tokens: int = 2048,
    provider: str = "local",
    response_format: Optional[dict] = None,
    timeout: float | None = None,
    fallback_to_local: bool = True,
    max_retries: int | None = None,
) -> CompletionResult:
    """Return (content, model_used, provider_used).

    provider_used is "remote" only when the remote path actually succeeded,
    otherwise "local". Remote failures fall back to local by default; callers
    can disable fallback and set a per-request timeout for bounded operations.
    """
    if provider == "remote" and remote_configured():
        model = settings.llm_remote_model or settings.ollama_llm_model
        remote_host = urlsplit(settings.llm_remote_base_url).hostname
        extra_body = (
            {"reasoning_format": "hidden"}
            if response_format is not None
            and remote_host == "api.groq.com"
            and model.startswith("openai/gpt-oss-")
            else None
        )
        try:
            content, usage = _run_chat(
                (_get_remote_client().with_options(max_retries=max_retries)
                 if max_retries is not None else _get_remote_client()),
                model,
                prompt,
                system=system,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                response_format=response_format,
                extra_body=extra_body,
                timeout=timeout,
            )
            if content:
                return CompletionResult(content, model, "remote", usage)
            # Empty/falsy content is a remote failure — fall through to local.
        except Exception as exc:
            if not fallback_to_local:
                raise
            # Keep the reason visible: the caller only sees the local fallback's result or error.
            logger.warning("Remote model call failed, falling back to local (host=%s): %s: %s",
                           remote_host, type(exc).__name__, str(exc)[:300])
        if not fallback_to_local:
            raise RuntimeError("The configured remote model returned an empty completion.")
    elif provider == "remote" and not fallback_to_local:
        raise RuntimeError("The configured remote model is unavailable.")

    model = settings.local_llm_model or settings.ollama_llm_model
    _cancel_local_model_unload()
    try:
        content, usage = _run_chat(
            (_get_openai_client().with_options(max_retries=max_retries)
             if max_retries is not None else _get_openai_client()),
            model,
            prompt,
            system=system,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            response_format=response_format,
            timeout=timeout,
        )
    finally:
        _schedule_local_model_unload(model, enabled=not bool(settings.local_llm_url))
    return CompletionResult(content, model, "local", usage)


def stream_chat(
    client: OpenAI,
    model: str,
    prompt: str,
    system: Optional[str] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    max_tokens: int = 2048,
) -> Any:
    """OpenAI-compatible streaming chat; yields text deltas.

    Raises on transport/API failure — callers degrade gracefully.
    """
    for delta, _usage in _stream_chat_with_usage(
        client, model, prompt, system=system, temperature=temperature,
        top_p=top_p, max_tokens=max_tokens,
    ):
        if delta:
            yield delta


def _stream_chat_with_usage(
    client: OpenAI,
    model: str,
    prompt: str,
    system: Optional[str] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    max_tokens: int = 2048,
) -> Any:
    """Yield ``(delta, usage)`` pairs, including usage-only final chunks."""
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    kwargs: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if top_p is not None:
        kwargs["top_p"] = top_p
    response = client.chat.completions.create(**kwargs)
    for chunk in response:
        usage = normalize_usage(getattr(chunk, "usage", None))
        if not chunk.choices:
            if usage is not None:
                yield "", usage
            continue
        yield chunk.choices[0].delta.content or "", usage


def stream_complete_with_model(
    prompt: str,
    system: Optional[str] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    max_tokens: int = 2048,
    provider: str = "local",
) -> Any:
    """Yield (delta, model, provider) text deltas, mirroring complete_with_model.

    Remote-first when ``provider == "remote"`` and configured; an empty first
    delta marks the start (``("", model, provider)``). A remote failure before
    the first text chunk silently falls back to the local Ollama model (re-emits
    the start marker with the local model). Failures after the stream began
    propagate to the caller.
    """
    emitted = False

    def _local_stream():
        model = settings.local_llm_model or settings.ollama_llm_model
        _cancel_local_model_unload()
        try:
            yield "", model, "local"
            for delta, usage in _stream_chat_with_usage(
                _get_openai_client(), model, prompt, system=system,
                temperature=temperature, top_p=top_p, max_tokens=max_tokens,
            ):
                yield StreamChunk(delta, model, "local", usage)
        finally:
            _schedule_local_model_unload(model, enabled=not bool(settings.local_llm_url))

    if provider == "remote" and remote_configured():
        model = settings.llm_remote_model or settings.ollama_llm_model
        try:
            first = _stream_chat_with_usage(
                _get_remote_client(),
                model,
                prompt,
                system=system,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
            )
            for delta, usage in first:
                if not emitted:
                    yield StreamChunk("", model, "remote")
                    emitted = True
                yield StreamChunk(delta, model, "remote", usage)
            return
        except Exception:
            # Nothing delivered yet — fall back to the local model.  # noqa: S110, BLE001 — intentional degradation to the local path
            pass

    yield from _local_stream()


def complete(
    prompt: str,
    system: Optional[str] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    max_tokens: int = 2048,
    provider: str = "local",
) -> str:
    """Return a plain text completion, optionally routed to the remote vLLM.

    Raises on local LLM failure — callers are expected to degrade gracefully.
    """
    content, _, _ = complete_with_model(
        prompt,
        system=system,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        provider=provider,
    )
    return content
