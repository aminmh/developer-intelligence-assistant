"""Shared LLM client backed by Instructor + Ollama's OpenAI-compatible endpoint."""

from __future__ import annotations

from typing import Any, Optional

import instructor
from openai import OpenAI

from settings import settings

_client: Optional[Any] = None


def get_client():
    """Return a lazily-created Instructor client pointing at Ollama's /v1 endpoint."""
    global _client
    if _client is None:
        base_url = settings.ollama_base_url.rstrip("/")
        openai_client = OpenAI(base_url=f"{base_url}/v1", api_key="ollama")
        _client = instructor.from_openai(openai_client, mode=instructor.Mode.JSON)
    return _client


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
