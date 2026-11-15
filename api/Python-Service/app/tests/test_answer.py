import rag.answer as answer


def test_build_user_prompt_includes_contexts_and_entities():
    contexts = [
        {
            "class": "Invoice",
            "name": "save",
            "file": "app/Models/Invoice.php",
            "code": "return true;",
        },
    ]
    entities = {"intent": "explain", "class_name": "Invoice", "method_name": "save"}

    prompt = answer.build_user_prompt("how does save work", contexts, entities)

    assert "app/Models/Invoice.php" in prompt
    assert "return true;" in prompt
    assert "Detected context" in prompt
    assert "Class: Invoice" in prompt
    assert "User question: how does save work" in prompt


def test_build_user_prompt_without_context_or_entities():
    prompt = answer.build_user_prompt("hello", [], None)

    assert "User question: hello" in prompt
    assert "Retrieved code context" not in prompt
    assert "Detected context" not in prompt


def test_system_prompt_forbids_echoing_context():
    assert "Never restate, quote, or reproduce the retrieved context" in answer.SYSTEM_PROMPT
    assert "source material only" in answer.SYSTEM_PROMPT


def test_build_user_prompt_truncates_large_chunks():
    contexts = [
        {"class": "A", "name": "b", "file": "f.php", "code": "x" * (answer.MAX_CONTEXT_CHARS + 500)},
    ]
    prompt = answer.build_user_prompt("q", contexts, None)

    assert "… (truncated)" in prompt
    assert "x" * answer.MAX_CONTEXT_CHARS in prompt
    assert "x" * (answer.MAX_CONTEXT_CHARS + 500) not in prompt


def test_build_user_prompt_caps_context_block_count():
    contexts = [
        {"class": f"C{i}", "name": f"n{i}", "file": f"f{i}.php", "code": "c"}
        for i in range(answer.MAX_CONTEXT_BLOCKS + 5)
    ]
    prompt = answer.build_user_prompt("q", contexts, None)

    # Only the first MAX_CONTEXT_BLOCKS blocks are embedded.
    assert f"[{answer.MAX_CONTEXT_BLOCKS + 1}]" not in prompt
    assert f"f{answer.MAX_CONTEXT_BLOCKS - 1}.php" in prompt


def test_generate_answer_returns_expected_shape(monkeypatch):
    captured = {}

    def fake_complete_with_model(
        prompt, system=None, temperature=None, top_p=None, max_tokens=2048, provider="local"
    ):
        captured["system"] = system
        captured["prompt"] = prompt
        captured["temperature"] = temperature
        captured["top_p"] = top_p
        captured["provider"] = provider
        return "the answer", answer.settings.ollama_llm_model, provider

    monkeypatch.setattr(answer, "complete_with_model", fake_complete_with_model)

    result = answer.generate_answer(
        "q",
        contexts=[{"class": "A", "name": "b", "file": "f", "code": "c"}],
        entities={"intent": "explain"},
    )

    assert result["answer"] == "the answer"
    assert result["model"] == answer.settings.ollama_llm_model
    assert result["provider"] == "local"
    assert result["duration_ms"] >= 0
    assert captured["system"]  # system prompt passed
    assert "c" in captured["prompt"]  # code context in prompt


def test_generate_answer_empty_contexts(monkeypatch):
    monkeypatch.setattr(
        answer,
        "complete_with_model",
        lambda prompt, system=None, temperature=None, top_p=None, max_tokens=2048, provider="local": (
            "ok",
            answer.settings.ollama_llm_model,
            provider,
        ),
    )

    result = answer.generate_answer("q")

    assert result["answer"] == "ok"
    assert result["model"] == answer.settings.ollama_llm_model
