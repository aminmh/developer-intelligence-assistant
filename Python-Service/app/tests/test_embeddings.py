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
    assert captured["body"] == {
        "model": "BGE-M3",
        "prompt": "hello",
        "keep_alive": "5m",
    }
    assert vectors == [[0.7, 0.8]]


def test_empty_input_short_circuits(monkeypatch):
    _isolate_embedding_settings(monkeypatch)

    def fail(request, timeout=None):
        raise AssertionError("should not be called")

    _patch_urlopen(monkeypatch, fail)

    assert embeddings.embed_texts([]) == []


def test_cohere_dispatch_uses_role_model_and_reports_batch_progress(monkeypatch):
    monkeypatch.setattr(embeddings, "provider_name", lambda: "cohere")
    monkeypatch.setattr(embeddings.settings, "cohere_api_key", "test-key")
    captured = []

    def fake(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        captured.append((req.full_url, req.get_header("Authorization"), body))
        return _FakeResponse({"embeddings": {"float": [[0.1, 0.2] for _ in body["texts"]]}})

    _patch_urlopen(monkeypatch, fake)
    progress = []
    vectors = embeddings.embed_texts(
        [f"text-{n}" for n in range(97)],
        input_type="search_document",
        progress=progress.append,
    )

    assert len(vectors) == 97
    assert [row[2]["model"] for row in captured] == ["embed-v5.0-pro", "embed-v5.0-pro"]
    assert [len(row[2]["texts"]) for row in captured] == [96, 1]
    assert all(row[0] == "https://api.cohere.com/v2/embed" for row in captured)
    assert all(row[1] == "Bearer test-key" for row in captured)
    assert all(row[2]["input_type"] == "search_document" for row in captured)
    assert progress == [96, 1]


def test_cohere_query_uses_fast_model_and_rejects_missing_key(monkeypatch):
    monkeypatch.setattr(embeddings, "provider_name", lambda: "cohere")
    monkeypatch.setattr(embeddings.settings, "cohere_api_key", "test-key")
    captured = {}

    def fake(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"embeddings": {"float": [[0.1, 0.2]]}})

    _patch_urlopen(monkeypatch, fake)
    embeddings.embed_texts(["query"], input_type="search_query")
    assert captured["body"]["model"] == "embed-v5.0-fast"
    assert captured["body"]["input_type"] == "search_query"

    monkeypatch.setattr(embeddings.settings, "cohere_api_key", "")
    try:
        embeddings.embed_texts(["query"], input_type="search_query")
        raised = False
    except RuntimeError as exc:
        raised = "API key" in str(exc)
    assert raised


def test_cohere_retries_rate_limit_after_cooldown(monkeypatch):
    monkeypatch.setattr(embeddings, "provider_name", lambda: "cohere")
    monkeypatch.setattr(embeddings.settings, "cohere_api_key", "test-key")
    attempts = []
    sleeps = []

    def fake(req, timeout=None):
        attempts.append(req.full_url)
        if len(attempts) == 1:
            raise error.HTTPError(req.full_url, 429, "rate limited", {}, None)
        return _FakeResponse({"embeddings": {"float": [[0.1, 0.2]]}})

    _patch_urlopen(monkeypatch, fake)
    monkeypatch.setattr(embeddings.time, "sleep", sleeps.append)

    assert embeddings.embed_texts(["query"], input_type="search_document") == [[0.1, 0.2]]
    assert len(attempts) == 2
    assert sleeps == [60.0]


def test_ollama_embedding_request_uses_configured_keep_alive(monkeypatch):
    monkeypatch.setattr(embeddings, "provider_name", lambda: "ollama")
    monkeypatch.setattr(embeddings.settings, "ollama_base_url", "http://ollama:11434")
    monkeypatch.setattr(embeddings.settings, "ollama_embedding_model", "BGE-M3")
    monkeypatch.setattr(embeddings.settings, "ollama_keep_alive", "30s")
    captured = {}

    def fake(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"embedding": [0.1, 0.2]})

    _patch_urlopen(monkeypatch, fake)

    assert embeddings.embed_texts(["document"]) == [[0.1, 0.2]]
    assert captured["url"] == "http://ollama:11434/api/embeddings"
    assert captured["body"] == {
        "model": "BGE-M3",
        "prompt": "document",
        "keep_alive": "30s",
    }


def test_llamaindex_embed_model_dispatch(monkeypatch):
    _isolate_embedding_settings(monkeypatch)
    # Ollama path keeps the LlamaIndex adapter and forwards the configured TTL.
    model = embeddings.get_llamaindex_embed_model()
    assert type(model).__module__.startswith("llama_index")
    assert model.model_name == "BGE-M3"
    monkeypatch.setattr(embeddings.settings, "ollama_keep_alive", "30s")
    captured = {}

    def fake_embeddings(**kwargs):
        captured.update(kwargs)
        return {"embedding": [0.1, 0.2]}

    monkeypatch.setattr(model._client, "embeddings", fake_embeddings)
    assert model.get_general_text_embedding("document") == [0.1, 0.2]
    assert captured["keep_alive"] == "30s"

    monkeypatch.setattr(embeddings.settings, "embedding_base_url", "http://localhost:1234")
    monkeypatch.setattr(embeddings, "provider_name", lambda: "openAICompatible")
    model = embeddings.get_llamaindex_embed_model()
    # OpenAI-compatible path returns the BaseEmbedding wrapper.
    from llama_index.core.embeddings import BaseEmbedding

    assert isinstance(model, BaseEmbedding)
    assert model.model_name == "BGE-M3"


def test_langchain_embeddings_dispatch(monkeypatch):
    _isolate_embedding_settings(monkeypatch)
    emb = embeddings.get_langchain_embeddings()
    assert type(emb).__name__ == "_KeepAliveOllamaEmbeddings"
    forwarded = []
    monkeypatch.setattr(
        embeddings,
        "_embed_ollama",
        lambda texts, progress=None: forwarded.append(texts) or [[0.1, 0.2]],
    )
    assert emb.embed_documents(["document"]) == [[0.1, 0.2]]
    assert emb.embed_query("question") == [0.1, 0.2]
    assert forwarded == [["passage: document"], ["query: question"]]

    monkeypatch.setattr(embeddings.settings, "embedding_base_url", "http://localhost:1234")
    monkeypatch.setattr(embeddings, "provider_name", lambda: "openAICompatible")
    emb = embeddings.get_langchain_embeddings()
    assert type(emb).__name__ == "OpenAIEmbeddings"
    assert emb.model == "BGE-M3"
