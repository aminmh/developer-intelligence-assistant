"""Tests for LLM streaming and answer streaming events."""

import answer
import llm


class _Chunk:
    """Minimal OpenAI streaming chunk: choices[0].delta.content."""

    def __init__(self, text: str | None) -> None:
        class _Delta:
            content = text

        class _Choice:
            delta = _Delta()

        self.choices = [_Choice()]


class _FakeStream:
    def __init__(self, chunks: list) -> None:
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)


class _FakeClient:
    """Chainable fake exposing chat.completions.create(..., stream=True)."""

    def __init__(self, chunks: list) -> None:
        self._chunks = chunks
        self.captured = None

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.captured = kwargs
        assert kwargs.get("stream") is True
        return _FakeStream(self._chunks)


# ─── llm.stream_chat ────────────────────────────────────────────────────────


def test_stream_chat_yields_text_deltas(monkeypatch):
    client = _FakeClient([_Chunk("Hel"), _Chunk("lo "), _Chunk("world")])
    out = list(llm.stream_chat(client, "m", "prompt"))
    assert out == ["Hel", "lo ", "world"]
    assert client.captured["stream"] is True
    assert client.captured["messages"][-1]["content"] == "prompt"


def test_stream_chat_skips_empty_deltas(monkeypatch):
    client = _FakeClient([_Chunk("a"), _Chunk(None), _Chunk("b"), _Chunk(None)])
    out = list(llm.stream_chat(client, "m", "prompt"))
    assert out == ["a", "b"]


def test_stream_complete_with_model_local_default(monkeypatch):
    client = _FakeClient([_Chunk("one"), _Chunk("two")])
    monkeypatch.setattr(llm, "_get_openai_client", lambda: client)
    # No remote configured — provider stays local.
    events = list(llm.stream_complete_with_model("p", provider="remote"))
    assert events[0] == ("", llm.settings.ollama_llm_model, "local")
    assert events[1:] == [("one", llm.settings.ollama_llm_model, "local"),
                          ("two", llm.settings.ollama_llm_model, "local")]


# ─── answer.generate_answer_stream ──────────────────────────────────────────


def _fake_stream_complete():
    def _gen(prompt, system=None, temperature=None, top_p=None,
             max_tokens=2048, provider="local"):
        yield "", "gemma-4b", "local"
        yield "Hello ", "gemma-4b", "local"
        yield "world", "gemma-4b", "local"
    return _gen


def test_generate_answer_stream_events(monkeypatch):
    monkeypatch.setattr(answer, "stream_complete_with_model", _fake_stream_complete())
    events = list(answer.generate_answer_stream("q", route="chat"))
    types = [e["type"] for e in events]
    assert types == ["start", "delta", "delta", "validation", "done"]
    assert events[0]["model"] == "gemma-4b"
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "Hello world"
    done = events[-1]
    assert done["answer"] == "Hello world"
    assert done["provider"] == "local"
    assert done["code_validation"] == []


def test_generate_answer_stream_skips_validation_for_chat(monkeypatch):
    monkeypatch.setattr(answer, "stream_complete_with_model", _fake_stream_complete())

    def boom(*args, **kwargs):
        raise AssertionError("LSP validation must not run for chat route")

    monkeypatch.setattr("lsp.validate_markdown", boom)
    monkeypatch.setattr(answer.settings, "lsp_validation_enabled", True)

    events = list(answer.generate_answer_stream("q", route="chat"))
    assert events[-1]["code_validation"] == []


def test_generate_answer_stream_skips_validation_for_knowledge(monkeypatch):
    monkeypatch.setattr(answer, "stream_complete_with_model", _fake_stream_complete())

    def boom(*args, **kwargs):
        raise AssertionError("LSP validation must not run for knowledge route")

    monkeypatch.setattr("lsp.validate_markdown", boom)
    monkeypatch.setattr(answer.settings, "lsp_validation_enabled", True)

    events = list(answer.generate_answer_stream("q", route="knowledge"))
    assert events[-1]["code_validation"] == []


def test_generate_answer_stream_runs_validation_for_code_routes(monkeypatch):
    monkeypatch.setattr(answer, "stream_complete_with_model", _fake_stream_complete())

    def fake_validate(text, question, max_retries=2):
        return text, [{"language": "php", "index": 0, "status": "valid",
                       "errors": [], "warnings": []}]

    monkeypatch.setattr("lsp.validate_markdown", fake_validate)
    monkeypatch.setattr("lsp.build_note", lambda s: "VALIDATION-NOTE")
    monkeypatch.setattr(answer.settings, "lsp_validation_enabled", True)

    events = list(answer.generate_answer_stream("q", route="explain"))
    done = events[-1]
    assert done["code_validation"][0]["language"] == "php"
    assert "VALIDATION-NOTE" in done["answer"]


# ─── answer.generate_answer route gating ────────────────────────────────────


def test_generate_answer_nonstream_skips_validation_for_chat(monkeypatch):
    def fake_complete_with_model(prompt, system=None, temperature=None,
                                 top_p=None, max_tokens=2048, provider="local"):
        return "plain answer", "m", provider

    monkeypatch.setattr(answer, "complete_with_model", fake_complete_with_model)

    def boom(*args, **kwargs):
        raise AssertionError("LSP validation must not run for chat route")

    monkeypatch.setattr("lsp.validate_markdown", boom)
    monkeypatch.setattr(answer.settings, "lsp_validation_enabled", True)

    result = answer.generate_answer("q", route="chat")
    assert result["answer"] == "plain answer"
    assert result["code_validation"] == []


# ─── /answer route registration regression ──────────────────────────────────


def test_answer_route_registered_to_real_handler():
    """Regression: a stray @app.post('/answer') on _answer_context shadowed
    the real handler, so every POST /answer 422'd with missing query
    params. The registered endpoint must be answer_prompt, and there must
    be exactly one /answer route."""
    from main import app
    import main as main_module

    answer_routes = [
        r for r in app.routes
        if getattr(r, "path", None) == "/answer"
    ]
    assert len(answer_routes) == 1
    endpoint = answer_routes[0].endpoint
    assert endpoint is main_module.answer_prompt
