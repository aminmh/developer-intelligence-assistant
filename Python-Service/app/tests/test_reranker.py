from __future__ import annotations

import rag.reranker as reranker


def _result(node_id: str, score: float = 0.5):
    return {
        "score": score,
        "payload": {"node_id": node_id, "type": "method", "name": node_id, "code": node_id},
    }


def test_candidate_text_is_deterministic_and_bounded():
    payload = {"type": "method", "class_fqn": "App\\Service", "name": "charge", "code": "x"}
    assert reranker.candidate_text(payload) == reranker.candidate_text(dict(payload))
    assert len(reranker.candidate_text({"code": "x" * 10000})) == 6000


def test_disabled_reranking_is_a_noop(monkeypatch):
    monkeypatch.setattr(reranker.settings, "reranker_enabled", False)
    rows = [_result("a"), _result("b")]
    summary = reranker.rerank("query", rows)
    assert summary["reranked_count"] == 0
    assert all("rerank_score" not in row for row in rows)


def test_reranking_orders_by_cross_encoder_score(monkeypatch):
    class FakeModel:
        def predict(self, pairs, show_progress_bar=False):
            assert pairs[0][0] == "query"
            return [0.1, 0.9]

    monkeypatch.setattr(reranker.settings, "reranker_enabled", True)
    monkeypatch.setattr(reranker.settings, "reranker_model", "fake")
    monkeypatch.setattr(reranker.settings, "reranker_timeout", 2)
    monkeypatch.setattr(reranker, "_model", lambda name: FakeModel())
    rows = [_result("a", 0.99), _result("b", 0.1)]
    summary = reranker.rerank("query", rows)
    rows.sort(key=lambda row: row["rerank_score"], reverse=True)
    assert [row["payload"]["node_id"] for row in rows] == ["b", "a"]
    assert summary["reranked_count"] == 2


def test_reranker_failure_is_non_fatal(monkeypatch):
    monkeypatch.setattr(reranker.settings, "reranker_enabled", True)
    monkeypatch.setattr(reranker.settings, "reranker_model", "fake")
    monkeypatch.setattr(reranker, "_model", lambda name: (_ for _ in ()).throw(RuntimeError("down")))
    rows = [_result("a")]
    summary = reranker.rerank("query", rows)
    assert summary["reranked_count"] == 0
    assert "down" in summary["fallback_reason"]
