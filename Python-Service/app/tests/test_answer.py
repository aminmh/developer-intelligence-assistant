import answer


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
