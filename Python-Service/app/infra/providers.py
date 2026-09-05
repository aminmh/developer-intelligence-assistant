"""Resource-provider protocols + thin adapters over the real implementations.

Every swappable external capability — LLM chat, embeddings, library-doc
retrieval, sandbox backend — is behind a small Protocol here. Adapters are
**thin wrappers**: they carry no logic and read settings at call time, so the
existing env/dynamic settings keep working unchanged and existing tests
continue to cover the real behavior. Swapping a provider (e.g. Ollama →
RunPod for chat) means implementing the Protocol and registering it — call
sites stay on ``providers.llm()`` etc.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# --- protocols --------------------------------------------------------------


@runtime_checkable
class LlmProvider(Protocol):
    """Chat completion. ``complete`` never raises for remote failures (falls
    back to local exactly like the underlying implementation)."""

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int = 2048,
        provider: str = "local",
    ) -> tuple[str, str, str]: ...

    def stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        provider: str = "local",
    ) -> Iterator[tuple[str, str, str]]: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class DocRetriever(Protocol):
    def retrieve(
        self,
        packages: dict[str, Any],
        query: str,
        *,
        enabled: bool | None = None,
        strict: bool | None = None,
    ) -> dict[str, Any]: ...


@runtime_checkable
class SandboxBackend(Protocol):
    def status(self) -> dict[str, Any]: ...

    def analyze(self, workspace_path: str, project_id: str, *, framework: str = "php",
                probes: list[str] | None = None) -> dict[str, Any]: ...

    def run_command(self, project_id: str, command: str, *, framework: str = "php",
                    cwd: str | None = None) -> dict[str, Any]: ...


# --- adapters ---------------------------------------------------------------


class LlmAdapter:
    """Wraps runtime/llm.py — the real local/remote routing lives there."""

    name = "llm"

    def complete(self, prompt, *, system=None, temperature=None, top_p=None,
                 max_tokens=2048, provider="local"):
        from runtime import llm

        return llm.complete_with_model(
            prompt, system=system, temperature=temperature, top_p=top_p,
            max_tokens=max_tokens, provider=provider,
        )

    def stream(self, prompt, *, system=None, temperature=None, top_p=None,
               provider="local"):
        from runtime import llm

        return llm.stream_complete_with_model(
            prompt, system=system, temperature=temperature, top_p=top_p,
            provider=provider,
        )


class EmbeddingAdapter:
    """Wraps index/embeddings.py (OpenAI-compatible base URL else Ollama)."""

    name = "embeddings"

    def embed(self, texts: list[str]) -> list[list[float]]:
        from index import embeddings

        return embeddings.embed_texts(texts)


class DocRetrieverAdapter:
    """Wraps rag/doc_retrieval.py (Context7 primary, GitMCP fallback)."""

    name = "doc_retriever"

    def retrieve(self, packages, query, *, enabled=None, strict=None):
        from rag import doc_retrieval

        return doc_retrieval.retrieve_documentation(
            packages, query, enabled=enabled, strict=strict
        )


class SandboxBackendAdapter:
    """Wraps runtime/sandbox_runtime.py status + sandbox/executor.py."""

    name = "sandbox"

    def status(self) -> dict[str, Any]:
        from runtime import sandbox_runtime

        return sandbox_runtime.sandbox_available()

    def analyze(self, workspace_path, project_id, *, framework="php", probes=None):
        from sandbox import executor

        return executor.default_executor().analyze(
            workspace_path, project_id, framework=framework, probes=probes
        )

    def run_command(self, project_id, command, *, framework="php", cwd=None):
        from sandbox import executor

        return executor.default_executor().run_command(
            project_id, command, framework=framework, cwd=cwd
        )


# --- registry ---------------------------------------------------------------

_BuiltinProvider = Callable[[], Any]
_BUILTINS: dict[str, _BuiltinProvider] = {
    "llm": LlmAdapter,
    "embeddings": EmbeddingAdapter,
    "doc_retriever": DocRetrieverAdapter,
    "sandbox": SandboxBackendAdapter,
}

_instances: dict[str, Any] = {}


def register(name: str, provider: Any) -> None:
    """Register a provider instance (or factory) under ``name``."""
    _instances[name] = provider


def provider(name: str) -> Any:
    """Return the registered provider for ``name``, instantiating lazily."""
    if name in _instances:
        return _instances[name] if not isinstance(_instances[name], type) \
            else _instances[name]()
    factory = _BUILTINS.get(name)
    if factory is None:
        raise KeyError(f"unknown provider: {name!r}")
    instance = factory()
    _instances[name] = instance
    return instance


# Named accessors — the call sites that must never know implementation detail.
def llm() -> LlmProvider:
    return provider("llm")


def embeddings() -> EmbeddingProvider:
    return provider("embeddings")


def doc_retriever() -> DocRetriever:
    return provider("doc_retriever")


def sandbox() -> SandboxBackend:
    return provider("sandbox")
