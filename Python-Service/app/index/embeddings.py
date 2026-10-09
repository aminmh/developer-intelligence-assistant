"""Embedding client dispatch — Ollama, OpenAI-compatible, or Cohere.

The embedding server is configurable via the dynamic ``embedding_base_url``
(``embeddingBaseUrl``) setting:

* ``ollama`` -> Ollama ``/api/embeddings`` (default),
* ``openAICompatible`` -> ``{base}/v1/embeddings`` (LM Studio, Bionic, ...),
* ``cohere`` -> Cohere v2 ``/embed`` with role-specific models.

The model name comes from ``ollama_embedding_model`` (``ollamaEmbedModel``) for
both backends. The low-level ``embed_texts`` path is stdlib-only (``urllib``),
mirroring ``lmstudio.py``; the llama_index / langchain adapters are provided
for the ingestion and evaluation pipelines.
"""

from __future__ import annotations

import json
import time
from urllib.error import HTTPError
import urllib.request as request
import urllib.request
from typing import Any

from infra.settings import settings


def embedding_base_url() -> str:
    """Return the configured OpenAI-compatible embedding base URL, or ''."""
    return (settings.embedding_base_url or "").strip().rstrip("/")


def _openai_v1_base() -> str:
    """Normalise ``embedding_base_url`` to ``<base>/v1`` for the SDK paths."""
    base = embedding_base_url()
    base = base.removesuffix("/v1")
    return f"{base}/v1"


def provider_name() -> str:
    """Return the selected embedding provider, preserving legacy URL config."""
    return settings.embedding_provider


def embedding_profile(role: str = "search_document") -> dict[str, Any]:
    """Describe the vector space used for a document or query embedding."""
    provider = provider_name()
    if provider == "cohere":
        model = "embed-v5.0-fast" if role == "search_query" else "embed-v5.0-pro"
        return {"provider": provider, "model": model, "dimensions": 2048}
    if provider == "openAICompatible":
        return {"provider": provider, "model": settings.ollama_embedding_model, "base_url": _openai_v1_base()}
    return {"provider": "ollama", "model": settings.ollama_embedding_model, "base_url": settings.ollama_base_url.rstrip("/")}


def embed_texts(
    texts: list[str],
    *,
    input_type: str = "search_document",
    progress: Any = None,
) -> list[list[float]]:
    """Embed ``texts`` and return one vector per input.

    Raises on transport/parse failure. ``progress`` is an optional callback
    called with the number of inputs completed after each successful request.
    """
    if not texts:
        return []
    if input_type not in {"search_document", "search_query", "classification"}:
        raise ValueError("Unsupported embedding input_type")
    provider = provider_name()
    if provider == "cohere":
        return _embed_cohere(texts, input_type, progress)
    if provider == "openAICompatible":
        return _embed_openai(texts, progress)
    if provider == "ollama":
        return _embed_ollama(texts, progress)
    raise ValueError(f"Unsupported embedding provider: {provider}")


def _report_progress(progress: Any, count: int) -> None:
    if progress is not None:
        progress(count)


def _embed_cohere(texts: list[str], input_type: str, progress: Any = None) -> list[list[float]]:
    if not settings.cohere_api_key:
        raise RuntimeError("Cohere API key is not configured")
    vectors: list[list[float]] = []
    profile = embedding_profile(input_type)
    for offset in range(0, len(texts), 96):
        batch = texts[offset : offset + 96]
        body = json.dumps({
            "model": profile["model"],
            "texts": batch,
            "input_type": input_type,
            "embedding_types": ["float"],
            "truncate": "END",
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.cohere.com/v2/embed",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {settings.cohere_api_key}"},
            method="POST",
        )
        with _cohere_urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
        rows = (data.get("embeddings") or {}).get("float") if isinstance(data, dict) else None
        if not isinstance(rows, list) or len(rows) != len(batch) or not all(isinstance(row, list) and row for row in rows):
            raise TypeError("Invalid embedding response from Cohere")
        vectors.extend(rows)
        _report_progress(progress, len(batch))
    return vectors


def _cohere_urlopen(req: urllib.request.Request):
    """Retry Cohere rate limits using its Retry-After header when available."""
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            return urllib.request.urlopen(req, timeout=120)
        except HTTPError as exc:
            if exc.code != 429 or attempt == max_retries:
                raise
            retry_after = None
            if exc.headers is not None:
                retry_after = exc.headers.get("Retry-After")
            exc.close()
            try:
                delay = max(0.0, float(retry_after)) if retry_after is not None else 60.0
            except (TypeError, ValueError):
                delay = 60.0
            time.sleep(delay)


def _embed_openai(texts: list[str], progress: Any = None) -> list[list[float]]:
    body = json.dumps(
        {
            "model": settings.ollama_embedding_model,
            "input": texts,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{_openai_v1_base()}/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))

    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise TypeError("Invalid embedding response from OpenAI-compatible server")
    rows = sorted(rows, key=lambda r: r.get("index", 0))
    vectors = [r.get("embedding") for r in rows]
    if len(vectors) != len(texts) or not all(
        isinstance(v, list) and v for v in vectors
    ):
        raise TypeError("Invalid embedding response from OpenAI-compatible server")
    _report_progress(progress, len(texts))
    return vectors


def _embed_ollama(texts: list[str], progress: Any = None) -> list[list[float]]:
    embeddings: list[list[float]] = []
    base = settings.ollama_base_url.rstrip("/")
    for doc in texts:
        body = json.dumps(
            {
                "model": settings.ollama_embedding_model,
                "prompt": doc,
                "keep_alive": settings.ollama_keep_alive,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/api/embeddings",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
        embedding = data.get("embedding")
        if not embedding or not isinstance(embedding, list):
            raise RuntimeError("Invalid embedding returned from Ollama")
        embeddings.append(embedding)
        _report_progress(progress, 1)
    return embeddings


def get_llamaindex_embed_model() -> Any:
    """Return the llama_index embedding model for ingestion/retrieval."""
    if provider_name() != "ollama":
        # llama_index's OpenAIEmbedding validates its model enum, rejecting
        # arbitrary local model ids — so wrap the raw /v1/embeddings call
        # instead. Keeps the dispatch in one place.
        from llama_index.core.embeddings import BaseEmbedding
        from llama_index.core.schema import BaseNode

        class _OpenAICompatibleEmbedding(BaseEmbedding):
            def __init__(self) -> None:
                super().__init__(model_name=settings.ollama_embedding_model)

            def _get_query_embedding(self, query: str) -> list[float]:
                return embed_texts([query], input_type="search_query")[0]

            def _get_text_embedding(self, text: str) -> list[float]:
                return embed_texts([text], input_type="search_document")[0]

            def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
                return embed_texts(texts, input_type="search_document")

            def _aget_query_embedding(self, query: str):
                return self._get_query_embedding(query)

            def _aget_text_embedding(self, text: str):
                return self._get_text_embedding(text)

            def _aget_text_embeddings(self, texts: list[str]):
                return self._get_text_embeddings(texts)

            def _embed(self, nodes: list[BaseNode]) -> list[BaseNode]:
                embeddings = embed_texts(
                    [n.get_content(metadata_mode="llm") for n in nodes],
                    input_type="search_document",
                )
                for node, vec in zip(nodes, embeddings):
                    node.embedding = vec
                return nodes

        return _OpenAICompatibleEmbedding()
    from llama_index.embeddings.ollama import OllamaEmbedding

    class _KeepAliveOllamaEmbedding(OllamaEmbedding):
        """Keep LlamaIndex's Ollama adapter behavior with a configurable TTL."""

        def get_general_text_embedding(self, texts: str) -> list[float]:
            result = self._client.embeddings(
                model=self.model_name,
                prompt=texts,
                options=self.ollama_additional_kwargs,
                keep_alive=settings.ollama_keep_alive,
            )
            return result["embedding"]

        async def aget_general_text_embedding(self, prompt: str) -> list[float]:
            result = await self._async_client.embeddings(
                model=self.model_name,
                prompt=prompt,
                options=self.ollama_additional_kwargs,
                keep_alive=settings.ollama_keep_alive,
            )
            return result["embedding"]

    return _KeepAliveOllamaEmbedding(
        model_name=settings.ollama_embedding_model,
        base_url=settings.ollama_base_url,
    )


def get_langchain_embeddings() -> Any:
    """Return the langchain embeddings wrapper for RAGAS evaluation."""
    if provider_name() == "openAICompatible":
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(
            model=settings.ollama_embedding_model,
            base_url=_openai_v1_base(),
            api_key="ollama",
        )
    if provider_name() != "ollama":
        from langchain_core.embeddings import Embeddings

        class _ConfiguredEmbeddings(Embeddings):
            def embed_documents(self, texts: list[str]) -> list[list[float]]:
                return embed_texts(texts, input_type="search_document")

            def embed_query(self, text: str) -> list[float]:
                return embed_texts([text], input_type="search_query")[0]

        return _ConfiguredEmbeddings()
    from langchain_core.embeddings import Embeddings

    class _KeepAliveOllamaEmbeddings(Embeddings):
        """Use the shared Ollama request path and preserve LangChain prefixes."""

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return _embed_ollama([f"passage: {text}" for text in texts])

        def embed_query(self, text: str) -> list[float]:
            return _embed_ollama([f"query: {text}"])[0]

    return _KeepAliveOllamaEmbeddings()
