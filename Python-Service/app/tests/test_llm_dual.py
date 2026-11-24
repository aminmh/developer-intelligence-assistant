"""Tests for dual-provider LLM routing (local Ollama + optional remote vLLM)."""

import json

import pytest

import runtime.llm as llm


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
def _reset_remote_client_state(monkeypatch):
    """Undo the module-level remote client cache between tests."""
    yield
    llm._remote_client_state = None
    llm._openai_client = None


@pytest.fixture(autouse=True)
def _isolate_local_chat_settings(monkeypatch):
    """Isolate from any user-saved localLlmUrl/localLlmModel in the live
    settings.db — the local chat provider defaults to Ollama in tests."""
    monkeypatch.setattr(llm.settings, "local_llm_url", "")
    monkeypatch.setattr(llm.settings, "local_llm_model", "")
    monkeypatch.setattr(llm.settings, "ollama_keep_alive", "-1")
    llm._cancel_local_model_unload()


def test_keep_alive_duration_parsing(monkeypatch):
    monkeypatch.setattr(llm.settings, "ollama_keep_alive", "30s")
    assert llm._keep_alive_seconds() == 30

    monkeypatch.setattr(llm.settings, "ollama_keep_alive", "2m")
    assert llm._keep_alive_seconds() == 120

    monkeypatch.setattr(llm.settings, "ollama_keep_alive", "0")
    assert llm._keep_alive_seconds() == 0

    monkeypatch.setattr(llm.settings, "ollama_keep_alive", "-1")
    assert llm._keep_alive_seconds() is None


def test_unload_local_model_uses_native_ollama_endpoint(monkeypatch):
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(llm.settings, "ollama_base_url", "http://ollama:11434")
    monkeypatch.setattr(llm.settings, "local_llm_url", "")
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    llm._unload_local_model("gemma3-4b")

    assert captured["url"] == "http://ollama:11434/api/generate"
    assert captured["timeout"] == 15
    assert json.loads(captured["body"]) == {
        "model": "gemma3-4b",
        "prompt": "",
        "stream": False,
        "keep_alive": 0,
    }


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


def test_complete_with_model_forwards_json_response_format(monkeypatch):
    _configure_remote(monkeypatch)

    remote_client = _FakeClient(content='{"ok":true}')
    monkeypatch.setattr(llm, "_get_remote_client", lambda: remote_client)
    monkeypatch.setattr(llm, "_get_openai_client", lambda: _FakeClient())

    llm.complete_with_model(
        "return json",
        provider="remote",
        response_format={"type": "json_object"},
    )

    assert remote_client.calls[0]["response_format"] == {"type": "json_object"}


def test_complete_with_model_forwards_per_request_timeout(monkeypatch):
    _configure_remote(monkeypatch)
    remote_client = _FakeClient(content="ok")
    monkeypatch.setattr(llm, "_get_remote_client", lambda: remote_client)

    llm.complete_with_model("hello", provider="remote", timeout=45)

    assert remote_client.calls[0]["timeout"] == 45


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


def test_remote_client_normalises_trailing_v1(monkeypatch):
    """A pasted full provider URL (with /v1) must not become /v1/v1."""
    _configure_remote(monkeypatch, base_url="http://vllm:8000/v1")

    captured = {}
    monkeypatch.setattr(
        llm, "OpenAI", lambda base_url, api_key: captured.update(base_url=base_url, api_key=api_key)
    )

    llm._get_remote_client()

    assert captured["base_url"] == "http://vllm:8000/v1"
    assert captured["api_key"] == "sekret"


def test_remote_client_cuts_a_pasted_chat_completions_endpoint_back_to_the_base(monkeypatch):
    for pasted in ("https://api.cloudflare.com/client/v4/accounts/abc/ai/v1/chat/completions",
                   "https://api.cloudflare.com/client/v4/accounts/abc/ai/v1/chat/completions/",
                   "https://api.cloudflare.com/client/v4/accounts/abc/ai/v1",
                   "https://api.cloudflare.com/client/v4/accounts/abc/ai"):
        _configure_remote(monkeypatch, base_url=pasted)
        assert llm.remote_endpoint()[0] == "https://api.cloudflare.com/client/v4/accounts/abc/ai/v1", pasted


def test_remote_client_normalises_plain_base(monkeypatch):
    _configure_remote(monkeypatch, base_url="http://vllm:8000")

    captured = {}
    monkeypatch.setattr(
        llm, "OpenAI", lambda base_url, api_key: captured.update(base_url=base_url, api_key=api_key)
    )

    llm._get_remote_client()

    assert captured["base_url"] == "http://vllm:8000/v1"


def test_test_remote_uses_unsaved_base_url_and_model_with_the_saved_key(monkeypatch):
    _configure_remote(monkeypatch, base_url="http://saved:8000")
    built = []
    probes = []

    def fake_client(base_url, api_key):
        built.append((base_url, api_key))
        return _FakeClient(content="ok")

    monkeypatch.setattr(llm, "_remote_client_for", fake_client)
    monkeypatch.setattr(llm, "_get_remote_client", lambda: (_ for _ in ()).throw(AssertionError("saved client used")))
    import runtime.tool_calling_probe as probe_module
    monkeypatch.setattr(probe_module, "probe_tool_calling", lambda *_a, **_k: {"supported": True, "detail": "ok"})
    monkeypatch.setattr(probe_module, "record_probe", lambda url, model, probe, store=None: probes.append((url, model)) or probe)

    result = llm.test_remote(overrides={"base_url": "https://draft.example/v1", "model": " draft-model "})

    assert result["ok"] is True and result["model"] == "draft-model"
    assert built == [("https://draft.example/v1", "sekret")]  # the saved key, never one from the form
    assert probes == [("https://draft.example/v1", "draft-model")]


def test_test_remote_without_overrides_still_uses_the_saved_endpoint(monkeypatch):
    _configure_remote(monkeypatch)
    monkeypatch.setattr(llm, "_get_remote_client", lambda: _FakeClient(content="ok"))
    assert llm.test_remote(overrides={})["model"] == "qwen2.5-32b"
    monkeypatch.setattr(llm.settings, "llm_remote_base_url", "")
    assert llm.test_remote(overrides={"base_url": ""})["detail"] == "No base URL configured"


def test_remote_test_route_validates_and_forwards_the_unsaved_values(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import system

    seen = []
    monkeypatch.setattr(system.llm_module, "test_remote", lambda overrides=None: seen.append(overrides) or {"ok": True})
    app = FastAPI()
    app.include_router(system.router)
    client = TestClient(app)

    assert client.post("/health/test/remote", json={"base_url": " https://x.example ", "model": "m", "api_key": "leak"}).json() == {"ok": True}
    assert seen[-1] == {"base_url": "https://x.example", "model": "m"}  # no api_key is ever forwarded
    assert client.post("/health/test/remote").json() == {"ok": True}
    assert seen[-1] == {}
    bad = client.post("/health/test/remote", json={"base_url": "file:///etc/passwd"}).json()
    assert bad["ok"] is False and len(seen) == 2


def test_test_remote_ok(monkeypatch):
    _configure_remote(monkeypatch)
    monkeypatch.setattr(llm, "_get_remote_client", lambda: _FakeClient(content="ok"))

    result = llm.test_remote()

    assert result["ok"] is True
    assert result["model"] == "qwen2.5-32b"
    assert result["detail"] == "connected"
    assert result["latency_ms"] >= 0


def test_test_remote_error_reports_detail(monkeypatch):
    _configure_remote(monkeypatch)
    monkeypatch.setattr(
        llm, "_get_remote_client", lambda: _FakeClient(error=RuntimeError("401 invalid key"))
    )

    result = llm.test_remote()

    assert result["ok"] is False
    assert "401 invalid key" in result["detail"]


def test_test_remote_unconfigured(monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_remote_base_url", "")
    monkeypatch.setattr(llm.settings, "llm_remote_model", "")

    result = llm.test_remote()

    assert result["ok"] is False
    assert "No base URL configured" in result["detail"]


@pytest.mark.parametrize('provider', ['local', 'remote'])
def test_completion_retry_override_is_per_call(monkeypatch, provider):
    _configure_remote(monkeypatch)
    base = _FakeClient()
    bounded = _FakeClient()
    options = []
    def with_options(**kwargs):
        options.append(kwargs)
        return bounded
    base.with_options = with_options
    monkeypatch.setattr(llm, '_get_remote_client', lambda: base)
    monkeypatch.setattr(llm, '_get_openai_client', lambda: base)
    llm.complete_with_model('hello', provider=provider, max_retries=0, timeout=60)
    assert options == [{'max_retries': 0}]
    assert len(bounded.calls) == 1 and bounded.calls[0]['timeout'] == 60
    llm.complete_with_model('hello', provider=provider)
    assert len(base.calls) == 1 and len(options) == 1
