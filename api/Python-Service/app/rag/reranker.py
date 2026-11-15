"""Optional local cross-encoder reranking for retrieved candidates."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from infra.settings import settings

_LOGGER = logging.getLogger(__name__)
_LOCK = threading.RLock()
_MODELS: dict[str, Any] = {}


def candidate_text(payload: dict[str, Any]) -> str:
    """Build a bounded, deterministic text representation for reranking."""
    parts = [
        f"Type: {payload.get('type') or payload.get('kind') or ''}",
        f"Class: {payload.get('class_fqn') or payload.get('class') or ''}",
        f"Name: {payload.get('method_name') or payload.get('name') or payload.get('fqn') or ''}",
        f"File: {payload.get('file') or ''}",
        f"Signature: {payload.get('signature') or ''}",
        f"Code: {payload.get('code') or payload.get('description') or ''}",
    ]
    return "\n".join(parts)[:6000]


def _model(model_name: str):
    with _LOCK:
        if model_name in _MODELS:
            return _MODELS[model_name]
        from sentence_transformers import CrossEncoder

        loaded = CrossEncoder(model_name)
        _MODELS[model_name] = loaded
        return loaded


def rerank(query: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    """Rerank results, returning a non-fatal operational summary.

    The original ``score`` is intentionally preserved. ``rerank_score`` is
    added to each result only after a successful model call.
    """
    summary = {
        "enabled": bool(settings.reranker_enabled),
        "model": settings.reranker_model,
        "candidate_count": len(results),
        "reranked_count": 0,
        "latency_ms": 0,
        "fallback_reason": None,
    }
    if not settings.reranker_enabled or not results:
        return summary

    started = time.perf_counter()
    try:
        model = _model(settings.reranker_model)
        pairs = [(query, candidate_text(r.get("payload") or {})) for r in results]
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(model.predict, pairs, show_progress_bar=False)
        try:
            scores = future.result(timeout=max(0.1, float(settings.reranker_timeout)))
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        if len(scores) != len(results):
            raise RuntimeError("reranker returned an unexpected score count")
        for result, score in zip(results, scores):
            result["rerank_score"] = float(score)
        summary["reranked_count"] = len(results)
    except Exception as exc:  # best-effort retrieval enhancement
        summary["fallback_reason"] = str(exc)[:300]
        _LOGGER.warning("Reranking unavailable; retaining vector/graph order: %s", exc)
    finally:
        summary["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return summary


def clear_cache() -> None:
    with _LOCK:
        _MODELS.clear()
