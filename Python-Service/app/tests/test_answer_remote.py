"""Tests for remote-answer routing and the /health/llm provider health endpoint."""

import json

import pytest

import rag.answer as answer
import runtime.llm as llm


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


def test_remote_completion_timeout_does_not_start_a_second_unbounded_local_request(monkeypatch):
    captured = {}

    def timeout(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        raise TimeoutError("provider timed out")

    monkeypatch.setattr(llm, "remote_configured", lambda: True)
    monkeypatch.setattr(llm, "_get_remote_client", lambda: object())
    monkeypatch.setattr(llm, "_run_chat", timeout)
    monkeypatch.setattr(llm, "_get_openai_client", lambda: pytest.fail("must not start local fallback"))

    with pytest.raises(TimeoutError, match="provider timed out"):
        llm.complete_with_model(
            "score", provider="remote", timeout=120, fallback_to_local=False,
        )

    assert captured["timeout"] == 120


@pytest.mark.parametrize(
    ("base_url", "model", "expected_extra_body"),
    [
        ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b", {"reasoning_format": "hidden"}),
        ("https://api.example.test/v1", "openai/gpt-oss-120b", None),
        ("https://api.groq.com/openai/v1", "qwen/qwen3-32b", None),
    ],
)
def test_remote_json_completion_adds_groq_gpt_oss_reasoning_format_only_when_required(
    monkeypatch, base_url, model, expected_extra_body,
):
    captured = {}

    monkeypatch.setattr(llm, "remote_configured", lambda: True)
    monkeypatch.setattr(llm, "_get_remote_client", lambda: object())
    monkeypatch.setattr(llm.settings, "llm_remote_base_url", base_url)
    monkeypatch.setattr(llm.settings, "llm_remote_model", model)
    monkeypatch.setattr(
        llm,
        "_run_chat",
        lambda *_args, **kwargs: (captured.update(kwargs) or "{}", None),
    )

    result = llm.complete_with_model(
        "Return a JSON object.",
        provider="remote",
        response_format={"type": "json_object"},
        fallback_to_local=False,
    )

    assert result[2] == "remote"
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["extra_body"] == expected_extra_body


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
    _load_main()
    from routers import system

    def refuse(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(system.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(system.settings, "ollama_base_url", "http://localhost:11434")
    monkeypatch.setattr(system.settings, "llm_remote_base_url", "")
    monkeypatch.setattr(system.settings, "llm_remote_api_key", "")

    result = system.health_llm()

    assert "local" in result
    assert "remote" in result
    assert result["local"]["configured"] is True
    assert result["local"]["reachable"] is False
    assert result["remote"]["configured"] is False
    assert result["remote"]["reachable"] is False


def test_health_llm_reports_reachable_providers(monkeypatch):
    _load_main()
    from routers import system
    captured = {}

    def fake_urlopen(req, timeout=2.0):
        captured["timeout"] = timeout
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        if req.full_url.endswith("/api/tags"):
            return _FakeHttpResponse({"models": [{"name": "a"}, {"name": "b"}]})
        return _FakeHttpResponse({"data": [{"id": "qwen2.5-32b"}]})

    monkeypatch.setattr(system.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(system.settings, "ollama_base_url", "http://localhost:11434/")
    monkeypatch.setattr(system.settings, "llm_remote_base_url", "http://localhost:8000/")
    monkeypatch.setattr(system.settings, "llm_remote_api_key", "sekret")
    monkeypatch.setattr(system.settings, "runpod_api_key", "")

    result = system.health_llm()

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
    _load_main()
    from routers import system
    captured = {}

    def fake_urlopen(req, timeout=5.0):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        return _FakeHttpResponse({"data": [{"id": "qwen2.5-32b"}]})

    monkeypatch.setattr(system.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(system.settings, "ollama_base_url", "http://localhost:11434/")
    monkeypatch.setattr(system.settings, "llm_remote_base_url", "http://localhost:8000/")
    monkeypatch.setattr(system.settings, "llm_remote_api_key", "")
    monkeypatch.setattr(system.settings, "runpod_api_key", "")

    result = system.health_llm()

    assert result["remote"]["configured"] is True
    assert result["remote"]["reachable"] is True
    assert result["remote"]["model"] == "qwen2.5-32b"
    assert captured["url"].endswith("/v1/models")
    assert captured["auth"] is None
