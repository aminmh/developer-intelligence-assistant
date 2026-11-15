"""Token and context usage metadata tests."""

import rag.answer as answer
import runtime.llm as llm
from runtime.llm import CompletionResult, StreamChunk


def test_generate_answer_preserves_provider_usage_and_context_limit(monkeypatch):
    monkeypatch.setattr(answer.settings, "llm_context_window_tokens", 100)
    monkeypatch.setattr(
        answer,
        "complete_with_model",
        lambda *args, **kwargs: CompletionResult(
            "answer", "model-x", "remote",
            {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27,
             "estimated": False, "source": "provider"},
        ),
    )

    result = answer.generate_answer("question", use_remote=True)

    assert result["usage"]["total_tokens"] == 27
    assert result["usage"]["estimated"] is False
    assert result["context_usage"] == {
        "input_tokens": 20,
        "limit_tokens": 100,
        "percent": 20.0,
        "estimated": False,
    }


def test_generate_answer_estimates_usage_when_provider_omits_it(monkeypatch):
    monkeypatch.setattr(answer.settings, "llm_context_window_tokens", 0)
    monkeypatch.setattr(
        answer,
        "complete_with_model",
        lambda *args, **kwargs: ("answer", "model-x", "local"),
    )

    result = answer.generate_answer("question")

    assert result["usage"]["estimated"] is True
    assert result["usage"]["source"] == "estimate"
    assert result["usage"]["total_tokens"] == (
        result["usage"]["prompt_tokens"] + result["usage"]["completion_tokens"]
    )
    assert result["context_usage"] is None


def test_stream_answer_captures_final_provider_usage(monkeypatch):
    monkeypatch.setattr(answer.settings, "llm_context_window_tokens", 200)

    def fake_stream(*args, **kwargs):
        yield "", "model-x", "remote"
        yield StreamChunk("answer", "model-x", "remote")
        yield StreamChunk(
            "", "model-x", "remote",
            {"prompt_tokens": 40, "completion_tokens": 3, "total_tokens": 43,
             "estimated": False, "source": "provider"},
        )

    monkeypatch.setattr(answer, "stream_complete_with_model", fake_stream)
    done = list(answer.generate_answer_stream("question", use_remote=True))[-1]

    assert done["type"] == "done"
    assert done["usage"]["total_tokens"] == 43
    assert done["context_usage"]["percent"] == 20.0


def test_stream_chat_usage_chunk_is_normalized_without_changing_text_api():
    class Chunk:
        def __init__(self, text=None, usage=None):
            self.choices = [] if text is None else [type("Choice", (), {
                "delta": type("Delta", (), {"content": text})(),
            })()]
            self.usage = usage

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    return iter([
                        Chunk("hi"),
                        Chunk(usage={"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}),
                    ])

    assert list(llm.stream_chat(Client(), "model", "prompt")) == ["hi"]
    pairs = list(llm._stream_chat_with_usage(Client(), "model", "prompt"))
    assert pairs[-1][0] == ""
    assert pairs[-1][1]["total_tokens"] == 5
