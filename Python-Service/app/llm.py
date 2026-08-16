"""Shared LLM client backed by Ollama's OpenAI-compatible endpoint."""

from __future__ import annotations

from typing import Any, List, Optional

import instructor
from openai import OpenAI

from settings import settings

_openai_client: Optional[OpenAI] = None
_instructor_client: Optional[Any] = None


def _get_openai_client() -> OpenAI:
    """Return a lazily-created raw OpenAI client pointing at Ollama's /v1 endpoint."""
    global _openai_client
    if _openai_client is None:
        base_url = settings.ollama_base_url.rstrip("/")
        _openai_client = OpenAI(base_url=f"{base_url}/v1", api_key="ollama")
    return _openai_client


def get_client():
    """Return a lazily-created Instructor client (JSON mode) for structured output."""
    global _instructor_client
    if _instructor_client is None:
        _instructor_client = instructor.from_openai(
            _get_openai_client(), mode=instructor.Mode.JSON
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


def complete(
    prompt: str,
    system: Optional[str] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    max_tokens: int = 2048,
) -> str:
    """Return a plain text completion from the configured Ollama chat model.

    Raises on LLM failure — callers are expected to degrade gracefully.
    """
    client = _get_openai_client()
    messages: List[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs: dict = {
        "model": settings.ollama_llm_model,
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
