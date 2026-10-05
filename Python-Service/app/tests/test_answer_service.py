from __future__ import annotations

import json
import sys
import types

import pytest

from rag.answer_service import AnswerService, InvalidAnswerRequest


def _service():
    return AnswerService(
        answer_context=lambda body, query, trace: (
            [{"payload": {"node_id": "n1", "class": "App\\User", "name": "find", "file": "User.php", "code": "return;"}, "score": 0.9}],
            [{"code": "return;"}],
            "base context",
        ),
        resolve_profile=lambda body, query: {"framework": "laravel"},
        inspect_context=lambda results, body: None,
        domain_context=lambda results, body: None,
        resolve_preview_mode=lambda body: "off",
        report_stream_error=lambda exc: None,
    )


def test_answer_service_rejects_missing_query():
    with pytest.raises(InvalidAnswerRequest):
        _service().answer({})


def test_answer_service_normalizes_answer_response(monkeypatch):
    module = types.ModuleType("rag.answer")
    module.generate_answer = lambda *args, **kwargs: {
        "answer": "done",
        "provider": "local",
        "model": "test",
        "duration_ms": 12,
        "code_validation": [],
    }
    monkeypatch.setitem(sys.modules, "rag.answer", module)

    result = _service().answer({"query": "find user"})
    assert result["answer"] == "done"
    assert result["matches"][0]["chunk"]["fileName"] == "App\\User::find"


def test_answer_service_frames_stream_events(monkeypatch):
    module = types.ModuleType("rag.answer")
    module.generate_answer_stream = lambda *args, **kwargs: iter([{"type": "done", "answer": "ok"}])
    monkeypatch.setitem(sys.modules, "rag.answer", module)

    events = list(_service().stream({"query": "stream"}))
    assert len(events) == 1
    assert events[0].startswith("data: ")
    assert events[0].endswith("\n\n")
    payload = json.loads(events[0][len("data: "):])
    assert payload["type"] == "done"
    assert payload["answer"] == "ok"
    assert payload["matches"][0]["chunk"]["fileName"] == "App\\User::find"
