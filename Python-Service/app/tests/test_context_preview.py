"""Hermetic tests for the context-window preview feature.

Covers the canonical markdown renderer, the /answer + /answer/stream generator
paths (preview-only vs inline confirm), stream-mode gating (the leading
``context`` event only appears when the caller enables it), per-request
settings resolution, and the /settings PUT enum guard.
"""

import json

import pytest

import main as main_module
from rag import answer

# ─── render_context_window ─────────────────────────────────────────────────


def test_render_context_window_shape():
    system = "You are an expert developer assistant RAG system."
    prompt = "User question: how does save work"
    window = answer.render_context_window(system, prompt)
    assert "# System prompt" in window
    assert system in window
    assert "# Context window sent to model" in window
    assert prompt in window


# ─── generate_answer (non-stream) ──────────────────────────────────────────


def test_generate_answer_preview_only_skips_model(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("complete_with_model must not run for preview_only")

    monkeypatch.setattr("rag.answer.complete_with_model", boom)

    result = answer.generate_answer(
        "q",
        contexts=[{"class": "A", "name": "b", "file": "f.php", "code": "return 1;"}],
        preview_only=True,
    )

    assert result["preview_only"] is True
    assert result["answer"] is None
    assert result["context_preview"]
    assert "# System prompt" in result["context_preview"]
    assert "User question: q" in result["context_preview"]


def test_generate_answer_inline_attaches_context_preview(monkeypatch):
    def fake_complete_with_model(prompt, system=None, temperature=None, top_p=None, provider="local"):
        return "hi", "m", provider

    monkeypatch.setattr("rag.answer.complete_with_model", fake_complete_with_model)

    result = answer.generate_answer("q", route="chat")

    assert result["answer"] == "hi"
    assert result["model"] == "m"
    assert result["provider"] == "local"
    assert "context_preview" in result
    assert "User question: q" in result["context_preview"]
    assert result.get("preview_only") is None


def test_generate_answer_gate_confirm_runs_model_with_preview(monkeypatch):
    """Gate confirm path: mode gate + preview_only False still answers."""
    def fake_complete_with_model(prompt, system=None, temperature=None, top_p=None, provider="local"):
        return "ok", "m", "local"

    monkeypatch.setattr("rag.answer.complete_with_model", fake_complete_with_model)

    result = answer.generate_answer("q", route="chat", preview_mode="gate")

    assert result["answer"] == "ok"
    assert result["model"] == "m"
    assert result.get("preview_only") is None
    assert "context_preview" in result
    assert "# System prompt" in result["context_preview"]
    assert "User question: q" in result["context_preview"]


# ─── generate_answer_stream ────────────────────────────────────────────────


def _fake_stream(deltas=("a",)):
    def _gen(prompt, system=None, temperature=None, top_p=None, provider="local"):
        yield "", "m", provider  # model/provider handshake → start event
        for text in deltas:
            yield text, "m", provider

    return _gen


def test_generate_answer_stream_preview_only_skips_model(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("stream_complete_with_model must not run for preview_only")

    monkeypatch.setattr("rag.answer.stream_complete_with_model", boom)

    events = list(
        answer.generate_answer_stream("q", preview_only=True, preview_mode="off")
    )

    assert events[0]["type"] == "context"
    assert events[0]["preview"] == answer.render_context_window(
        answer.SYSTEM_PROMPT, "User question: q"
    )
    assert len(events) == 2
    done = events[-1]
    assert done["type"] == "done"
    assert done["preview_only"] is True
    assert done["answer"] is None
    assert done["model"] is None


def test_generate_answer_stream_off_omits_context_event(monkeypatch):
    monkeypatch.setattr("rag.answer.stream_complete_with_model", _fake_stream(deltas=("hi",)))

    events = list(answer.generate_answer_stream("q", route="chat"))
    types = [e["type"] for e in events]

    assert types[0] != "context"
    assert types[0] in ("start", "delta")
    assert "context" not in types
    assert "delta" in types
    assert "done" in types
    assert events[-1]["answer"] == "hi"


def test_generate_answer_stream_inline_starts_with_context(monkeypatch):
    monkeypatch.setattr("rag.answer.stream_complete_with_model", _fake_stream(deltas=("a",)))

    events = list(
        answer.generate_answer_stream("q", route="chat", preview_mode="inline")
    )
    types = [e["type"] for e in events]

    assert types[0] == "context"
    assert "start" in types
    assert "delta" in types
    assert "done" in types
    assert events[-1]["answer"] == "a"


# ─── settings resolution (main._resolve_preview_mode) ──────────────────────


def test_resolve_preview_mode_uses_body_when_valid(monkeypatch):
    monkeypatch.setattr(main_module.settings, "context_preview_mode", "off")
    assert main_module._resolve_preview_mode({"preview_mode": "gate"}) == "gate"
    assert main_module._resolve_preview_mode({"preview_mode": "inline"}) == "inline"


def test_resolve_preview_mode_falls_back_to_settings(monkeypatch):
    monkeypatch.setattr(main_module.settings, "context_preview_mode", "inline")
    assert main_module._resolve_preview_mode({}) == "inline"
    assert main_module._resolve_preview_mode({"preview_mode": "bogus"}) == "inline"


def test_resolve_preview_mode_unknown_falls_to_off(monkeypatch):
    monkeypatch.setattr(main_module.settings, "context_preview_mode", "gate")
    assert main_module._resolve_preview_mode({"preview_mode": 42}) == "gate"
    # Settings value itself invalid -> hard "off".
    monkeypatch.setattr(main_module.settings, "context_preview_mode", "wat")
    assert main_module._resolve_preview_mode({}) == "off"


# ─── /settings PUT validation ──────────────────────────────────────────────


def test_settings_put_validates_context_preview_mode():
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)

    bad = client.put("/settings", json={"contextPreviewMode": "bogus"})
    assert bad.status_code == 400
    assert bad.json()["detail"] == "contextPreviewMode must be one of off, inline, gate"

    try:
        ok = client.put("/settings", json={"contextPreviewMode": "gate"})
        assert ok.status_code == 200
        got = client.get("/settings").json()
        assert got["config"]["contextPreviewMode"] == "gate"
    finally:
        restored = client.put("/settings", json={"contextPreviewMode": "off"})
        assert restored.status_code == 200
        final = client.get("/settings").json()
        assert final["config"]["contextPreviewMode"] == "off"


# ─── HTTP contract: gate + preview returns only the window ─────────────────


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    with TestClient(main_module.app) as c:
        yield c


@pytest.fixture()
def _noop_retrieval_trace(monkeypatch):
    """_answer_context records an explicit-context trace per request; keep the
    JSONL log inert in tests. The explicit-context branch never touches Qdrant,
    so no retrieval backend is needed."""
    import index.llamaindex_layer as layer

    monkeypatch.setattr(layer, "record_retrieval_trace", lambda *a, **k: None)


_PREVIEW_BODY = {
    "query": "hi",
    "extra_context": "PROJECT x",
    "preview_mode": "gate",
    "preview": True,
}


def test_answer_gate_preview_http_contract(client, _noop_retrieval_trace):
    resp = client.post("/answer", json=_PREVIEW_BODY)
    assert resp.status_code == 200
    data = resp.json()
    assert data["context_preview"].startswith("# System prompt")
    assert "PROJECT x" in data["context_preview"]
    assert data["preview_only"] is True
    assert data["answer"] is None
    assert data["llmUsed"] is None


def test_answer_stream_gate_preview_http_contract(client, _noop_retrieval_trace):
    resp = client.post("/answer/stream", json=_PREVIEW_BODY)
    assert resp.status_code == 200
    events = [
        json.loads(line[len("data: "):])
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    assert len(events) == 2
    assert events[0]["type"] == "context"
    assert events[0]["preview"]
    assert events[1]["type"] == "done"
    assert events[1]["preview_only"] is True
