"""LM Studio client tests — model listing + load against a mocked server."""

import json
import urllib.request
import unittest
import urllib.request

import runtime.lmstudio as lmstudio


class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _fake_urlopen(payload):
    import urllib.request as request

    def opener(request, timeout=None):
        return _FakeResponse(payload)

    import unittest.mock as mock

    return unittest.mock.patch.object(urllib.request, "urlopen", opener)


def test_models_parse_mlx_entry(monkeypatch):
    payload = {
        "object": "list",
        "data": [
            {
                "id": "mlx-community/Qwen3-4B-4bit",
                "type": "llm",
                "publisher": "mlx-community",
                "compatibility_type": "mlx",
            },
            {
                "id": "qwen2.5-coder-7b-instruct",
                "type": "llm",
                "publisher": "lmstudio-community",
                "compatibility_type": "llama.cpp",
            },
        ],
    }
    with _fake_urlopen(payload):
        result = lmstudio.lmstudio_models("http://localhost:1234")

    assert result["ok"] is True
    assert result["models"][0] == {
        "id": "mlx-community/Qwen3-4B-4bit",
        "engine": "mlx",
        "type": "llm",
        "quantization": None,
    }
    assert result["models"][1]["engine"] == "llama.cpp"
    assert result["models"][1]["type"] == "llm"
    # MLX models sort first.
    assert result["models"][0]["engine"] == "mlx"


def test_models_parse_embedding_entry(monkeypatch):
    payload = {
        "object": "list",
        "data": [
            {
                "id": "text-embedding-nomic-embed-text-v1.5",
                "type": "embeddings",
                "publisher": "nomic-ai",
                "compatibility_type": "gguf",
                "quantization": "Q4_K_M",
            }
        ],
    }
    with _fake_urlopen(payload):
        result = lmstudio.lmstudio_models("http://localhost:1234")

    assert result["ok"] is True
    assert result["models"][0]["type"] == "embeddings"
    assert result["models"][0]["engine"] == "gguf"


def test_models_falls_back_to_v1(monkeypatch):
    import urllib.error as error
    import urllib.request as request

    calls = []

    def fake(request, timeout=None):
        calls.append(request.full_url)
        if request.full_url.endswith("/api/v0/models"):
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", None, None)
        return _FakeResponse(
            {
                "object": "list",
                "data": [
                    {
                        "id": "qwen2.5-coder-7b",
                        "meta": {"engine": "mlx", "quantization": "4bit"},
                    }
                ],
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake)

    result = lmstudio.lmstudio_models("http://localhost:1234")

    assert result["ok"] is True
    assert calls[0].endswith("/api/v0/models")
    assert calls[1].endswith("/v1/models")
    assert result["models"][0]["engine"] == "mlx"


def test_models_empty_list(monkeypatch):
    with _fake_urlopen({"object": "list", "data": []}):
        result = lmstudio.lmstudio_models("http://localhost:1234")

    assert result["ok"] is True
    assert result["models"] == []


def test_models_no_base_url():
    result = lmstudio.lmstudio_models("")
    assert result["ok"] is False
    assert "No base URL" in result["detail"]


def test_models_unreachable(monkeypatch):
    import urllib.error as error
    import urllib.request as request

    def fail(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fail)

    result = lmstudio.lmstudio_models("http://127.0.0.1:1")

    assert result["ok"] is False
    assert "connection refused" in result["detail"]


def test_models_strips_v1_and_trailing_slash(monkeypatch):
    captured = {}

    def capture(request, timeout=None):
        captured["url"] = request.full_url
        return _FakeResponse({"object": "list", "data": []})

    import urllib.request as request

    monkeypatch.setattr(urllib.request, "urlopen", capture)

    lmstudio.lmstudio_models("http://localhost:1234/v1/")

    assert captured["url"] == "http://localhost:1234/api/v0/models"


def test_load_success(monkeypatch):
    captured = {}

    def capture(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode("utf-8")
        return _FakeResponse(
            {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Ok"},
                    }
                ],
            }
        )

    import urllib.request as request

    monkeypatch.setattr(urllib.request, "urlopen", capture)

    result = lmstudio.lmstudio_load(
        "http://localhost:1234", "mlx-community/Qwen3-4B-4bit"
    )

    assert result["ok"] is True
    assert captured["url"] == "http://localhost:1234/v1/chat/completions"
    body = json.loads(captured["body"])
    assert body["model"] == "mlx-community/Qwen3-4B-4bit"
    assert body["max_tokens"] == 1
    assert result["status"] == "loaded"


def test_load_missing_model():
    result = lmstudio.lmstudio_load("http://localhost:1234", "  ")
    assert result["ok"] is False
    assert "No model selected" in result["detail"]


def test_load_server_error(monkeypatch):
    import urllib.error as error
    import urllib.request as request

    def fail(request, timeout=None):
        raise urllib.error.URLError("model not found")

    monkeypatch.setattr(urllib.request, "urlopen", fail)

    result = lmstudio.lmstudio_load("http://localhost:1234", "nope")

    assert result["ok"] is False
    assert "model not found" in result["detail"]
