"""Embedding client dispatch — Ollama native or OpenAI-compatible endpoint.

The embedding server is configurable via the dynamic ``embedding_base_url``
(``embeddingBaseUrl``) setting:

* unset  -> Ollama ``/api/embeddings`` (default, backward compatible),
* set    -> OpenAI-compatible ``{base}/v1/embeddings`` (LM Studio, Bionic, ...).

The model name comes from ``ollama_embedding_model`` (``ollamaEmbedModel``) for
both backends. The low-level ``embed_texts`` path is stdlib-only (``urllib``),
mirroring ``lmstudio.py``; the llama_index / langchain adapters are provided
for the ingestion and evaluation pipelines.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from settings import settings


def embedding_base_url() -> str:
    """Return the configured OpenAI-compatible embedding base URL, or ''."""
    return (settings.embedding_base_url or "").strip().rstrip("/")


def _openai_v1_base() -> str:
    """Normalise ``embedding_base_url`` to ``<base>/v1`` for the SDK paths."""
    base = embedding_base_url()
    base = base.removesuffix("/v1")
    return f"{base}/v1"


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed ``texts`` and return one vector per input.

    Routes to the OpenAI-compatible ``/v1/embeddings`` endpoint when
    ``embedding_base_url`` is configured, otherwise to Ollama's native
    ``/api/embeddings``. Raises on transport/parse failure — callers degrade
    gracefully.
    """
    if not texts:
        return []
    if embedding_base_url():
        return _embed_openai(texts)
    return _embed_ollama(texts)


def _embed_openai(texts: list[str]) -> list[list[float]]:
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
    return vectors


def _embed_ollama(texts: list[str]) -> list[list[float]]:
    embeddings: list[list[float]] = []
    base = settings.ollama_base_url.rstrip("/")
    for doc in texts:
        body = json.dumps(
            {"model": settings.ollama_embedding_model, "prompt": doc}
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
    return embeddings


def get_llamaindex_embed_model() -> Any:
    """Return the llama_index embedding model for ingestion/retrieval."""
    if embedding_base_url():
        # llama_index's OpenAIEmbedding validates its model enum, rejecting
        # arbitrary local model ids — so wrap the raw /v1/embeddings call
        # instead. Keeps the dispatch in one place.
        from llama_index.core.embeddings import BaseEmbedding
        from llama_index.core.schema import BaseNode

        class _OpenAICompatibleEmbedding(BaseEmbedding):
            def __init__(self) -> None:
                super().__init__(model_name=settings.ollama_embedding_model)

            def _get_query_embedding(self, query: str) -> list[float]:
                return embed_texts([query])[0]

            def _get_text_embedding(self, text: str) -> list[float]:
                return embed_texts([text])[0]

            def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
                return embed_texts(texts)

            def _aget_query_embedding(self, query: str):
                return self._get_query_embedding(query)

            def _aget_text_embedding(self, text: str):
                return self._get_text_embedding(text)

            def _aget_text_embeddings(self, texts: list[str]):
                return self._get_text_embeddings(texts)

            def _embed(self, nodes: list[BaseNode]) -> list[BaseNode]:
                embeddings = embed_texts(
                    [n.get_content(metadata_mode="llm") for n in nodes]
                )
                for node, vec in zip(nodes, embeddings):
                    node.embedding = vec
                return nodes

        return _OpenAICompatibleEmbedding()
    from llama_index.embeddings.ollama import OllamaEmbedding

    return OllamaEmbedding(
        model_name=settings.ollama_embedding_model,
        base_url=settings.ollama_base_url,
    )


def get_langchain_embeddings() -> Any:
    """Return the langchain embeddings wrapper for RAGAS evaluation."""
    if embedding_base_url():
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(
            model=settings.ollama_embedding_model,
            base_url=_openai_v1_base(),
            api_key="ollama",
        )
    from langchain_community.embeddings import OllamaEmbeddings

    return OllamaEmbeddings(
        model=settings.ollama_embedding_model,
        base_url=settings.ollama_base_url,
    )
