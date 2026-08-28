"""Tests for remote-answer routing and the /health/llm provider health endpoint."""

import json

import pytest

import answer


def test_generate_answer_routes_remote_when_opted_in(monkeypatch):
    captured = {}

    def fake_complete_with_model(
        prompt, system=None, temperature=None, top_p=None, max_tokens=2048, provider="local"
    ):
        captured["provider"] = provider
        captured["system"] = system
        return "remote answer", "qwen2.5-32b", provider

    monkeypatch.setattr(answer, "complete_with_model", fake_complete_with_model)

    result = answer.generate_answer("q", use_remote=True)

    assert captured["provider"] == "remote"
    assert captured["system"]  # system prompt passed
    assert result["answer"] == "remote answer"
    assert result["model"] == "qwen2.5-32b"
    assert result["provider"] == "remote"


def test_generate_answer_defaults_to_local(monkeypatch):
    captured = {}

    def fake_complete_with_model(
        prompt, system=None, temperature=None, top_p=None, max_tokens=2048, provider="local"
    ):
        captured["provider"] = provider
        return "local answer", answer.settings.ollama_llm_model, provider

    monkeypatch.setattr(answer, "complete_with_model", fake_complete_with_model)

    result = answer.generate_answer("q")

    assert captured["provider"] == "local"
    assert result["answer"] == "local answer"
    assert result["model"] == answer.settings.ollama_llm_model
    assert result["provider"] == "local"


# ─── /health/llm ────────────────────────────────────────────────────────────

# main.py imports llama_index (via indexer), which needs a working numpy/scipy
# stack. On the host where that stack is broken, skip rather than fail the run.


def _load_main():
    try:
        import main  # noqa: PLC0415
    except Exception as exc:
        pytest.skip(f"main import unavailable in this environment: {exc}")
    return main


class _FakeHttpResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_health_llm_never_raises_when_unconfigured(monkeypatch):
    main = _load_main()

    def refuse(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(main.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(main.settings, "ollama_base_url", "http://localhost:11434")
    monkeypatch.setattr(main.settings, "llm_remote_base_url", "")
    monkeypatch.setattr(main.settings, "llm_remote_api_key", "")

    result = main.health_llm()

    assert "local" in result
    assert "remote" in result
    assert result["local"]["configured"] is True
    assert result["local"]["reachable"] is False
    assert result["remote"]["configured"] is False
    assert result["remote"]["reachable"] is False


def test_health_llm_reports_reachable_providers(monkeypatch):
    main = _load_main()
    captured = {}

    def fake_urlopen(req, timeout=2.0):
        captured["timeout"] = timeout
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        if req.full_url.endswith("/api/tags"):
            return _FakeHttpResponse({"models": [{"name": "a"}, {"name": "b"}]})
        return _FakeHttpResponse({"data": [{"id": "qwen2.5-32b"}]})

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(main.settings, "ollama_base_url", "http://localhost:11434/")
    monkeypatch.setattr(main.settings, "llm_remote_base_url", "http://localhost:8000/")
    monkeypatch.setattr(main.settings, "llm_remote_api_key", "sekret")

    result = main.health_llm()

    assert result["local"]["configured"] is True
    assert result["local"]["reachable"] is True
    assert result["local"]["models"] == 2
    assert result["remote"]["configured"] is True
    assert result["remote"]["reachable"] is True
    assert result["remote"]["model"] == "qwen2.5-32b"
    assert captured["timeout"] == 2.0
    assert captured["url"].endswith("/v1/models")
    assert captured["auth"] == "Bearer sekret"


def test_health_llm_sends_no_auth_when_key_unset(monkeypatch):
    main = _load_main()
    captured = {}

    def fake_urlopen(req, timeout=2.0):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        return _FakeHttpResponse({"data": [{"id": "qwen2.5-32b"}]})

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(main.settings, "ollama_base_url", "http://localhost:11434/")
    monkeypatch.setattr(main.settings, "llm_remote_base_url", "http://localhost:8000/")
    monkeypatch.setattr(main.settings, "llm_remote_api_key", "")

    result = main.health_llm()

    assert result["remote"]["configured"] is True
    assert result["remote"]["reachable"] is True
    assert result["remote"]["model"] == "qwen2.5-32b"
    assert captured["url"].endswith("/v1/models")
    assert captured["auth"] is None
