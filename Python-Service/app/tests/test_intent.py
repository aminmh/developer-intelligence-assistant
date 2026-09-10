import pytest

import rag.intent as it


def test_extract_intent_valid(monkeypatch):
    monkeypatch.setattr(
        it,
        "extract",
        lambda model, prompt: it.PromptIntent(
            intent="explain",
            target_file="UserController.php",
            class_name="UserController",
            method_name="index",
        ),
    )

    r = it.extract_intent("explain the index method")

    assert r.intent == "explain"
    assert r.target_file == "UserController.php"
    assert r.class_name == "UserController"
    assert r.method_name == "index"


def test_extract_intent_normalizes_synonym(monkeypatch):
    monkeypatch.setattr(it, "extract", lambda model, prompt: it.PromptIntent(intent="fix"))

    r = it.extract_intent("fix the bug")

    assert r.intent == "debug"


def test_extract_intent_invalid_intent_defaults_chat(monkeypatch):
    monkeypatch.setattr(it, "extract", lambda model, prompt: it.PromptIntent(intent="bogus"))

    r = it.extract_intent("whatever")

    assert r.intent == "chat"


def test_extract_intent_fallback_on_failure(monkeypatch):
    def boom(model, prompt):
        raise RuntimeError("no llm")

    monkeypatch.setattr(it, "extract", boom)

    r = it.extract_intent("anything at all")

    assert r.intent == "chat"
    assert r.class_name is None
    assert r.constraints == []


def test_extract_intent_fallback_recovers_named_service(monkeypatch):
    def offline(*_args, **_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(it, "extract", offline)

    r = it.extract_intent("how CommentService works?")

    assert r.intent == "explain"
    assert r.class_name == "CommentService"


def test_extract_intent_overrides_generic_model_route_for_named_service(monkeypatch):
    monkeypatch.setattr(
        it,
        "extract",
        lambda model, prompt: it.PromptIntent(intent="knowledge"),
    )

    r = it.extract_intent("how CommentService works?")

    assert r.intent == "explain"
    assert r.class_name == "CommentService"


def test_extract_intent_empty_prompt():
    assert it.extract_intent("").intent == "chat"


def test_build_query_filter_none():
    from index.llamaindex_layer import _build_query_filter

    assert _build_query_filter() is None


def test_build_query_filter_class_only():
    from index.llamaindex_layer import _build_query_filter

    f = _build_query_filter(class_name="Invoice")
    assert f is not None
    assert len(f.must) == 1
    assert f.must[0].key == "class"


def test_build_query_filter_both():
    from index.llamaindex_layer import _build_query_filter

    f = _build_query_filter(class_name="Invoice", file="Invoice.php")
    assert f is not None
    assert len(f.must) == 2
    keys = {c.key for c in f.must}
    assert keys == {"class", "file"}
