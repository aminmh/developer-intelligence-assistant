"""Shared LLM client backed by Ollama's OpenAI-compatible endpoint, with an
optional remote vLLM (OpenAI-compatible) provider for heavy answers.
"""

from __future__ import annotations

import time
from typing import Any, List, Optional, Tuple

import instructor
from openai import OpenAI

from settings import settings

_openai_client: Optional[Tuple[str, OpenAI]] = None
_extract_client_state: Optional[Tuple[str, OpenAI]] = None
_instructor_client: Optional[Any] = None

# (base_url, api_key, client) — re-created whenever either the dynamic base_url
# or api_key setting changes.
_remote_client_state: Optional[Tuple[str, str, OpenAI]] = None


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
    return client.chat.completions.create(
        model=settings.ollama_llm_model,
        response_model=response_model,
        messages=[{"role": "user", "content": prompt}],
        max_retries=1,
    )


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
) -> str:
    """Run a chat completion on `client` and return the plain text content."""
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

    response = client.chat.completions.create(**kwargs)
    content = response.choices[0].message.content if response.choices else ""
    return content or ""


def complete_with_model(
    prompt: str,
    system: Optional[str] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    max_tokens: int = 2048,
    provider: str = "local",
) -> Tuple[str, str, str]:
    """Return (content, model_used, provider_used).

    provider_used is "remote" only when the remote path actually succeeded,
    otherwise "local". An unconfigured or failing remote silently falls back to
    the local Ollama chat model — no error is raised for remote failures.
    """
    if provider == "remote" and remote_configured():
        model = settings.llm_remote_model or settings.ollama_llm_model
        try:
            content = _run_chat(
                _get_remote_client(),
                model,
                prompt,
                system=system,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
            )
            if content:
                return content, model, "remote"
            # Empty/falsy content is a remote failure — fall through to local.
        except Exception:
            pass  # silently fall back to the local model

    model = settings.local_llm_model or settings.ollama_llm_model
    content = _run_chat(
        _get_openai_client(),
        model,
        prompt,
        system=system,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
    )
    return content, model, "local"


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
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if top_p is not None:
        kwargs["top_p"] = top_p

    response = client.chat.completions.create(**kwargs)
    for chunk in response:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


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
        yield "", model, "local"
        yield from (
            (delta, model, "local")
            for delta in stream_chat(
                _get_openai_client(),
                model,
                prompt,
                system=system,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
            )
        )

    if provider == "remote" and remote_configured():
        model = settings.llm_remote_model or settings.ollama_llm_model
        try:
            first = stream_chat(
                _get_remote_client(),
                model,
                prompt,
                system=system,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
            )
            for delta in first:
                if not emitted:
                    yield "", model, "remote"
                    emitted = True
                yield delta, model, "remote"
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
