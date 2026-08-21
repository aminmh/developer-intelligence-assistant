import pandas as pd
import pytest

import evaluation


def test_tier_boundaries():
    assert evaluation.tier_for(0.9) == "Excellent"
    assert evaluation.tier_for(0.85) == "Excellent"
    assert evaluation.tier_for(0.7) == "Good"
    assert evaluation.tier_for(0.65) == "Good"
    assert evaluation.tier_for(0.5) == "Needs review"
    assert evaluation.tier_for(0.4) == "Needs review"
    assert evaluation.tier_for(0.3) == "Poor"


def test_evaluate_response_returns_three_metrics(monkeypatch):
    class FakeResult:
        def to_pandas(self):
            return pd.DataFrame(
                [
                    {
                        "faithfulness": 0.9,
                        "answer_relevancy": 0.7,
                        "llm_context_precision_without_reference": 0.5,
                    }
                ]
            )

    monkeypatch.setattr(evaluation, "evaluate", lambda **kwargs: FakeResult())
    monkeypatch.setattr(evaluation, "_get_llm", lambda: None)
    monkeypatch.setattr(evaluation, "_get_embeddings", lambda: None)

    metrics = evaluation.evaluate_response("q", "a", ["c1", "c2"])

    assert set(metrics.keys()) == {"faithfulness", "answer_relevancy", "context_precision"}
    assert metrics["faithfulness"]["score"] == 0.9
    assert metrics["faithfulness"]["tier"] == "Excellent"
    assert metrics["context_precision"]["score"] == 0.5
    assert metrics["context_precision"]["tier"] == "Needs review"


def test_evaluate_response_handles_nan(monkeypatch):
    class FakeResult:
        def to_pandas(self):
            return pd.DataFrame(
                [
                    {
                        "faithfulness": float("nan"),
                        "answer_relevancy": 0.7,
                        "llm_context_precision_without_reference": 0.5,
                    }
                ]
            )

    monkeypatch.setattr(evaluation, "evaluate", lambda **kwargs: FakeResult())
    monkeypatch.setattr(evaluation, "_get_llm", lambda: None)
    monkeypatch.setattr(evaluation, "_get_embeddings", lambda: None)

    metrics = evaluation.evaluate_response("q", "a", ["c"])

    assert metrics["faithfulness"]["score"] is None
    assert metrics["faithfulness"]["tier"] == "Unavailable"


def test_evaluate_response_raises_on_failure(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("ollama timeout")

    monkeypatch.setattr(evaluation, "evaluate", boom)
    monkeypatch.setattr(evaluation, "_get_llm", lambda: None)
    monkeypatch.setattr(evaluation, "_get_embeddings", lambda: None)

    with pytest.raises(RuntimeError):
        evaluation.evaluate_response("q", "a", ["c"])
