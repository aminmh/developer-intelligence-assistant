"""Tests for dual-provider LLM routing (local Ollama + optional remote vLLM)."""

import pytest

import llm


class _StubMessage:
    def __init__(self, content):
        self.content = content


class _StubChoice:
    def __init__(self, content):
        self.message = _StubMessage(content)


class _StubResponse:
    def __init__(self, content):
        self.choices = [_StubChoice(content)]


class _FakeClient:
    """Minimal OpenAI-client stand-in that records create() kwargs."""

    def __init__(self, content="hi", error=None):
        self.chat = self
        self.completions = self
        self.calls = []
        self._content = content
        self._error = error

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return _StubResponse(self._content)


@pytest.fixture(autouse=True)
def _reset_remote_client_state():
    """Undo the module-level remote client cache between tests."""
    yield
    llm._remote_client_state = None


def _configure_remote(monkeypatch, base_url="http://vllm:8000", model="qwen2.5-32b"):
    monkeypatch.setattr(llm.settings, "llm_remote_base_url", base_url)
    monkeypatch.setattr(llm.settings, "llm_remote_model", model)
    monkeypatch.setattr(llm.settings, "llm_remote_api_key", "sekret")


def test_complete_with_model_remote_unconfigured_falls_back_to_local(monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_remote_base_url", "")
    monkeypatch.setattr(llm.settings, "llm_remote_model", "")
    monkeypatch.setattr(llm.settings, "llm_remote_api_key", "")

    local_client = _FakeClient(content="local-answer")
    monkeypatch.setattr(llm, "_get_openai_client", lambda: local_client)
    remote_called = {"count": 0}
    monkeypatch.setattr(
        llm,
        "_get_remote_client",
        lambda: remote_called.__setitem__("count", remote_called["count"] + 1),
    )

    content, model_used, provider_used = llm.complete_with_model(
        "hello", provider="remote"
    )

    assert content == "local-answer"
    assert provider_used == "local"
    assert model_used == llm.settings.ollama_llm_model
    assert remote_called["count"] == 0
    assert local_client.calls[0]["model"] == llm.settings.ollama_llm_model


def test_complete_with_model_remote_configured_uses_remote(monkeypatch):
    _configure_remote(monkeypatch)

    remote_client = _FakeClient(content="hi")
    local_client = _FakeClient(content="should-not-run")
    monkeypatch.setattr(llm, "_get_remote_client", lambda: remote_client)
    monkeypatch.setattr(llm, "_get_openai_client", lambda: local_client)

    content, model_used, provider_used = llm.complete_with_model(
        "hello", provider="remote"
    )

    assert content == "hi"
    assert model_used == "qwen2.5-32b"
    assert provider_used == "remote"
    assert remote_client.calls[0]["model"] == "qwen2.5-32b"
    assert remote_client.calls[0]["messages"][-1]["content"] == "hello"
    assert local_client.calls == []


def test_complete_with_model_remote_error_falls_back_to_local(monkeypatch):
    _configure_remote(monkeypatch)

    remote_client = _FakeClient(error=RuntimeError("remote exploded"))
    local_client = _FakeClient(content="local-answer")
    monkeypatch.setattr(llm, "_get_remote_client", lambda: remote_client)
    monkeypatch.setattr(llm, "_get_openai_client", lambda: local_client)

    content, model_used, provider_used = llm.complete_with_model(
        "hello", provider="remote"
    )

    assert content == "local-answer"
    assert provider_used == "local"
    assert model_used == llm.settings.ollama_llm_model
    assert local_client.calls[0]["model"] == llm.settings.ollama_llm_model


def test_complete_with_model_remote_empty_content_falls_back_to_local(monkeypatch):
    _configure_remote(monkeypatch)

    remote_client = _FakeClient(content="")
    local_client = _FakeClient(content="local-answer")
    monkeypatch.setattr(llm, "_get_remote_client", lambda: remote_client)
    monkeypatch.setattr(llm, "_get_openai_client", lambda: local_client)

    content, model_used, provider_used = llm.complete_with_model(
        "hello", provider="remote"
    )

    assert content == "local-answer"
    assert provider_used == "local"
    assert model_used == llm.settings.ollama_llm_model
    assert local_client.calls[0]["model"] == llm.settings.ollama_llm_model


def test_remote_client_cached_and_recreated_on_base_url_change(monkeypatch):
    _configure_remote(monkeypatch, base_url="http://vllm:8000")

    first = llm._get_remote_client()
    second = llm._get_remote_client()
    assert first is second

    monkeypatch.setattr(llm.settings, "llm_remote_base_url", "http://vllm2:8001")
    third = llm._get_remote_client()
    assert third is not first

    fourth = llm._get_remote_client()
    assert third is fourth


def test_remote_client_recreated_on_api_key_change(monkeypatch):
    _configure_remote(monkeypatch)

    first = llm._get_remote_client()
    second = llm._get_remote_client()
    assert first is second

    monkeypatch.setattr(llm.settings, "llm_remote_api_key", "new-sekret")
    third = llm._get_remote_client()
    assert third is not first

    fourth = llm._get_remote_client()
    assert third is fourth


def test_complete_defaults_to_local_provider(monkeypatch):
    local_client = _FakeClient(content="local-answer")
    monkeypatch.setattr(llm, "_get_openai_client", lambda: local_client)

    content = llm.complete("hello")

    assert content == "local-answer"
    assert local_client.calls[0]["model"] == llm.settings.ollama_llm_model
