"""Embedding client dispatch tests — Ollama native vs OpenAI-compatible."""

import json
import urllib.error as error
import urllib.request as request
import urllib.request

import index.embeddings as embeddings


class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _patch_urlopen(monkeypatch, opener):
    monkeypatch.setattr(urllib.request, "urlopen", opener)


def _isolate_embedding_settings(monkeypatch):
    """Force deterministic embedding settings for tests."""
    monkeypatch.setattr(embeddings.settings, "embedding_base_url", "")
    monkeypatch.setattr(embeddings.settings, "ollama_base_url", "http://ollama:11434")
    monkeypatch.setattr(embeddings.settings, "ollama_embedding_model", "BGE-M3")


def test_openai_compatible_dispatch(monkeypatch):
    _isolate_embedding_settings(monkeypatch)
    monkeypatch.setattr(embeddings.settings, "embedding_base_url", "http://localhost:1234")
    captured = {}

    def fake(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {
                "object": "list",
                "data": [
                    {"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]},
                    {"object": "embedding", "index": 1, "embedding": [0.4, 0.5, 0.6]},
                ],
            }
        )

    _patch_urlopen(monkeypatch, fake)

    vectors = embeddings.embed_texts(["hello", "world"])

    assert captured["url"] == "http://localhost:1234/v1/embeddings"
    assert captured["body"] == {
        "model": "BGE-M3",
        "input": ["hello", "world"],
    }
    assert len(vectors) == 2
    assert vectors[0] == [0.1, 0.2, 0.3]


def test_openai_v1_suffix_stripped(monkeypatch):
    _isolate_embedding_settings(monkeypatch)
    monkeypatch.setattr(
        embeddings.settings, "embedding_base_url", "http://localhost:1234/v1/"
    )
    captured = {}

    def fake(request, timeout=None):
        captured["url"] = request.full_url
        return _FakeResponse(
            {"object": "list", "data": [{"index": 0, "embedding": [0.1]}]}
        )

    _patch_urlopen(monkeypatch, fake)

    embeddings.embed_texts(["x"])
    assert captured["url"] == "http://localhost:1234/v1/embeddings"


def test_openai_invalid_response_raises(monkeypatch):
    _isolate_embedding_settings(monkeypatch)
    monkeypatch.setattr(embeddings.settings, "embedding_base_url", "http://localhost:1234")

    def fake(request, timeout=None):
        return _FakeResponse({"object": "list", "data": []})

    _patch_urlopen(monkeypatch, fake)

    try:
        embeddings.embed_texts(["hello"])
        raised = False
    except TypeError:
        raised = True
    assert raised


def test_ollama_fallback_dispatch(monkeypatch):
    _isolate_embedding_settings(monkeypatch)
    captured = {}

    def fake(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse({"embedding": [0.7, 0.8]})

    _patch_urlopen(monkeypatch, fake)

    vectors = embeddings.embed_texts(["hello"])

    assert captured["url"] == "http://ollama:11434/api/embeddings"
    assert captured["body"] == {"model": "BGE-M3", "prompt": "hello"}
    assert vectors == [[0.7, 0.8]]


def test_empty_input_short_circuits(monkeypatch):
    _isolate_embedding_settings(monkeypatch)

    def fail(request, timeout=None):
        raise AssertionError("should not be called")

    _patch_urlopen(monkeypatch, fail)

    assert embeddings.embed_texts([]) == []


def test_llamaindex_embed_model_dispatch(monkeypatch):
    _isolate_embedding_settings(monkeypatch)
    # Ollama path returns a llama_index OllamaEmbedding.
    model = embeddings.get_llamaindex_embed_model()
    assert type(model).__module__.startswith("llama_index")
    assert model.model_name == "BGE-M3"

    monkeypatch.setattr(embeddings.settings, "embedding_base_url", "http://localhost:1234")
    model = embeddings.get_llamaindex_embed_model()
    # OpenAI-compatible path returns the BaseEmbedding wrapper.
    from llama_index.core.embeddings import BaseEmbedding

    assert isinstance(model, BaseEmbedding)
    assert model.model_name == "BGE-M3"


def test_langchain_embeddings_dispatch(monkeypatch):
    _isolate_embedding_settings(monkeypatch)
    emb = embeddings.get_langchain_embeddings()
    assert "Ollama" in type(emb).__name__

    monkeypatch.setattr(embeddings.settings, "embedding_base_url", "http://localhost:1234")
    emb = embeddings.get_langchain_embeddings()
    assert type(emb).__name__ == "OpenAIEmbeddings"
    assert emb.model == "BGE-M3"
