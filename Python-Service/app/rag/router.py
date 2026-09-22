"""Semantic Router — classifies user prompts using embedding similarity.

Adds a feedback loop (query corrections persisted to Qdrant) and a hybrid
LLM fallback for ambiguous/heavy cases. Embedding-based routing stays the
default fast path; route definitions and boundary criteria come from
``rag.route_registry`` and the adjudicator prompt from ``rag.router_prompt``.
"""

import json
import logging
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from semantic_router.encoders import BaseEncoder
from semantic_router.layer import RouteLayer
from semantic_router.route import Route

from infra.settings import settings
from rag.query_normalizer import normalize_query
from rag.code_query_signals import specific_symbol_route
from rag.route_registry import (
    ROUTE_DESCRIPTIONS,  # noqa: F401 — re-exported for module API compatibility
    ROUTE_ENTRIES,
    ROUTE_HEAVY,
    ROUTE_NAMES,
    assert_route_enum,
)
from rag.router_prompt import build_adjudicator_prompt

logger = logging.getLogger(__name__)

# Feedback collection holds user-submitted corrections: query vector -> route.
FEEDBACK_COLLECTION = "route_feedback"

# Similarity override threshold: a feedback point matching this closely wins.
# 0.80 (not 0.95) so rephrased variants of a corrected query still match —
# e.g. "which PHP version?" vs "which PHP version this project implemented?"
# (cosine ~0.82). Exact re-asks are ~1.0; unrelated queries fall well below.
FEEDBACK_OVERRIDE_THRESHOLD = 0.80

# Ambiguity detection: top similarity below this, or top-2 within this margin.
AMBIGUOUS_SCORE_THRESHOLD = 0.75
AMBIGUOUS_MARGIN = 0.05

# Below this embedding score a route is not worth trusting on its own; mirrors
# orchestrator.yml defaults.min_confidence.
MIN_CONFIDENCE = 0.35

class OllamaEncoder(BaseEncoder):
    type: str = "ollama"
    score_threshold: float = 0.3

    def __init__(self, name: str = "ollama"):
        super().__init__(name=name)

    def __call__(self, docs: List[str]) -> List[List[float]]:
        from index.embeddings import embed_texts

        return embed_texts([normalize_query(doc) for doc in docs])


encoder = OllamaEncoder()

routes = [
    Route(name=entry["name"], utterances=list(entry["utterances"]))
    for entry in ROUTE_ENTRIES
]

try:
    router = RouteLayer(encoder=encoder, routes=routes)
except Exception:
    router = None


def _init_router():
    global router
    if router is None:
        try:
            router = RouteLayer(encoder=encoder, routes=routes)
        except Exception:
            pass
    return router is not None


def _get_qdrant() -> QdrantClient:
    kwargs = {"url": settings.qdrant_url}
    if settings.qdrant_api_key:
        kwargs["api_key"] = settings.qdrant_api_key
    return QdrantClient(**kwargs)


def _ensure_feedback_collection(qdrant: QdrantClient, dim: int) -> None:
    try:
        existing = [c.name for c in qdrant.get_collections().collections]
        if FEEDBACK_COLLECTION in existing:
            # Recreate when the embedding model changed the vector dimension
            # (Qdrant cannot alter an existing collection's vector size).
            info = qdrant.get_collection(FEEDBACK_COLLECTION)
            current = getattr(info.config.params.vectors, "size", None)
            if current != dim:
                qdrant.delete_collection(FEEDBACK_COLLECTION)
            else:
                return
        qdrant.create_collection(
            collection_name=FEEDBACK_COLLECTION,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
    except Exception:
        pass


def submit_feedback(query: str, correct_route: str) -> dict:
    """Persist a user correction so future identical queries route correctly."""
    if not query or not isinstance(query, str):
        return {"success": False, "error": "Missing query"}
    if correct_route not in ROUTE_NAMES:
        return {
            "success": False,
            "error": f"Unknown route '{correct_route}'. Valid: {ROUTE_NAMES}",
        }

    try:
        embedding = encoder([query])[0]
        qdrant = _get_qdrant()
        _ensure_feedback_collection(qdrant, len(embedding))

        point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, query))
        qdrant.upsert(
            collection_name=FEEDBACK_COLLECTION,
            wait=True,
            points=[
                PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload={
                        "route": correct_route,
                        "source": "feedback",
                        "original_query": query,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )
            ],
        )
        return {"success": True, "route": correct_route, "query": query}
    except Exception as exc:
        return {"success": False, "error": str(exc)[:200]}


def _feedback_override(query_vec: List[float]) -> Optional[str]:
    """Return a forced route if a stored correction matches closely enough."""
    try:
        qdrant = _get_qdrant()
        collections = [c.name for c in qdrant.get_collections().collections]
        if FEEDBACK_COLLECTION not in collections:
            return None

        hits = qdrant.search(
            collection_name=FEEDBACK_COLLECTION,
            query_vector=query_vec,
            limit=1,
        )
        if not hits:
            return None
        if hits[0].score <= FEEDBACK_OVERRIDE_THRESHOLD:
            return None
        route = (hits[0].payload or {}).get("route")
        if route in ROUTE_NAMES:
            return route
    except Exception:
        pass
    return None


def _top_route_scores(query_vec: List[float], top_k: int = 8) -> List[Tuple[str, float]]:
    """Rank routes by their best (max) utterance cosine similarity."""
    if not _init_router():
        return []
    try:
        rows = router._retrieve(np.asarray(query_vec, dtype=np.float32), top_k=top_k)
    except Exception:
        return []

    best: dict = {}
    for row in rows:
        route = row.get("route")
        score = float(row.get("score", 0.0))
        if route not in best or score > best[route]:
            best[route] = score

    return sorted(best.items(), key=lambda kv: kv[1], reverse=True)


def _is_ambiguous(top1: float, ranked: List[Tuple[str, float]]) -> bool:
    return top1 < AMBIGUOUS_SCORE_THRESHOLD or (
        len(ranked) >= 2 and (ranked[0][1] - ranked[1][1]) < AMBIGUOUS_MARGIN
    )


def _first_known_route(text: str) -> str | None:
    """Return the first registered route name mentioned in raw LLM text."""
    for name in ROUTE_NAMES:
        if name in text:
            return name
    return None


def _llm_select_route(
    query: str,
    candidates: list[str],
    context_note: str = "",
) -> dict | None:
    """Ask the configured Ollama chat model to pick a route for a hard query.

    Returns a parsed, validated dict
    ``{route, confidence, reason, needs_clarification, candidate_routes}`` or
    None on any transport/parse/validation failure. ``confidence`` is None when
    the model omits it (or when the route came from a prose scan) so ``classify``
    substitutes the embedding top score; a present numeric value is clamped to
    [0, 1].
    """
    body = json.dumps(
        {
            "model": settings.ollama_llm_model,
            "prompt": build_adjudicator_prompt(query, candidates, context_note),
            "stream": False,
            "format": "json",
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        f"{settings.ollama_base_url}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — best-effort classifier; any transport failure is a None.
        return None

    text = data.get("response") or "" if isinstance(data, dict) else ""
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001 — the model may return prose; fall back to a text scan.
        parsed = None

    # Prose fallback: text-scan only when the model emitted no usable JSON.
    # Valid JSON naming an unknown route is a validation failure (None) — a text
    # scan of the raw JSON could let a route name inside `reason` win by accident.
    if not isinstance(parsed, dict):
        route = _first_known_route(text)
        if route is None:
            return None
        return {
            "route": route,
            "confidence": None,
            "reason": "",
            "needs_clarification": False,
            "candidate_routes": [],
        }

    route = parsed.get("route")
    if route not in ROUTE_NAMES:
        return None

    try:
        confidence = float(parsed.get("confidence"))
    except (TypeError, ValueError):
        confidence = None
    if confidence is not None:
        confidence = min(1.0, max(0.0, confidence))

    raw_reason = parsed.get("reason")
    reason = str(raw_reason)[:300] if raw_reason is not None else ""

    raw_clarify = parsed.get("needs_clarification")
    needs_clarification = raw_clarify if isinstance(raw_clarify, bool) else False

    candidate_routes = []
    if isinstance(parsed.get("candidate_routes"), list):
        valid = [c for c in parsed["candidate_routes"] if c in ROUTE_NAMES]
        candidate_routes = list(dict.fromkeys(valid))[:3]

    return {
        "route": route,
        "confidence": confidence,
        "reason": reason,
        "needs_clarification": needs_clarification,
        "candidate_routes": candidate_routes,
    }


def _log_fallback(
    query: str,
    embedding_route: str,
    embedding_score: float,
    ranked: List[Tuple[str, float]],
    llm_result: dict | None,
    final_confidence: float,
) -> None:
    """Append one JSON line for every adjudicator fallback invocation."""
    try:
        log_path = Path(settings.log_dir) / "router-fallback.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "query": query,
                "embedding_route": embedding_route,
                "embedding_score": embedding_score,
                "top_routes": ranked,
                "llm_route": (llm_result or {}).get("route"),
                "llm_confidence": (llm_result or {}).get("confidence"),
                "reason": (llm_result or {}).get("reason"),
                "needs_clarification": (llm_result or {}).get(
                    "needs_clarification"
                ),
                "candidate_routes": (llm_result or {}).get("candidate_routes"),
                "final_confidence": final_confidence,
                "final_choice": (llm_result or {}).get("route")
                or embedding_route,
            },
            default=str,
        )
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(entry + "\n")
    except Exception:
        pass


def classify(prompt: str) -> dict:
    """Classify a user prompt and return a superset dict of route metadata.

    Keys: ``route``, ``confidence``, ``reason``, ``needs_clarification``,
    ``source`` (``"embedding"`` | ``"llm_fallback"`` | ``"feedback"``) and,
    after an adjudicator call, ``candidate_routes``. Consumers must treat the
    response as additive — new keys may be added without notice.
    """
    # 1) Deterministic degradation: no router, no embeddings => safe chat route.
    if not _init_router():
        return {
            "route": "chat",
            "confidence": 0.0,
            "reason": "router unavailable",
            "needs_clarification": True,
        }

    try:
        query_vec = encoder([prompt])[0]
    except Exception:  # noqa: BLE001 — embed failures degrade to the safe chat route.
        return {
            "route": "chat",
            "confidence": 0.0,
            "reason": "router unavailable",
            "needs_clarification": True,
        }

    # 2) Feedback override — a stored correction matches almost exactly.
    forced = _feedback_override(query_vec)
    if forced:
        return {
            "route": forced,
            "confidence": 1.0,
            "source": "feedback",
            "reason": "user correction match",
            "needs_clarification": False,
        }

    # A concrete symbol name is stronger evidence than the generic phrase
    # "how ... works" that otherwise tends to embed near knowledge examples.
    # Keep this after feedback so an explicit user correction remains the
    # highest-priority owner of routing.
    specific_route = specific_symbol_route(prompt)
    if specific_route:
        return {
            "route": specific_route,
            "confidence": 1.0,
            "source": "deterministic_symbol",
            "reason": "named code symbol boundary",
            "needs_clarification": False,
        }

    # 3) Embedding ranking (default fast path).
    ranked = _top_route_scores(query_vec)
    if not ranked:
        return {
            "route": "chat",
            "confidence": 0.0,
            "reason": "no route candidates",
            "needs_clarification": True,
        }

    top1 = ranked[0][0]
    top_score = ranked[0][1]
    candidates = [name for name, _score in ranked[:3]]

    # 4) Escalate heavy routes and ambiguous rankings to the LLM adjudicator.
    needs_adjudication = _is_ambiguous(top_score, ranked) or any(
        name in ROUTE_HEAVY for name in candidates
    )

    if not needs_adjudication:
        return {
            "route": top1,
            "confidence": round(top_score, 4),
            "source": "embedding",
            "reason": None,
            "needs_clarification": False,
        }

    # 5) LLM adjudication.
    llm = _llm_select_route(prompt, candidates)
    if llm:
        final_confidence = (
            round(llm["confidence"], 4)
            if llm.get("confidence") is not None
            else round(top_score, 4)
        )
        _log_fallback(prompt, top1, top_score, ranked, llm, final_confidence)
        return {
            **llm,
            "source": "llm_fallback",
            "confidence": final_confidence,
            # Model echo of real candidates wins; an empty echo falls back to
            # the embedding pre-filter so the field always reflects reality.
            "candidate_routes": llm.get("candidate_routes") or candidates,
        }

    # 6) Adjudicator unavailable => trust the embedding only when it is strong.
    fallback_confidence = round(top_score, 4)
    _log_fallback(prompt, top1, top_score, ranked, llm, fallback_confidence)
    return {
        "route": top1 if top_score >= MIN_CONFIDENCE else "chat",
        "confidence": fallback_confidence,
        "source": "embedding",
        "reason": "llm adjudication unavailable",
        "needs_clarification": top_score < 0.5,
    }


def _check_route_enum_drift() -> None:
    """Log a warning when classifier routes drift from orchestrator.yml keys.

    Best-effort import-time parity tripwire: absent PyYAML or a missing/malformed
    config file must never crash boot, but a genuine enum mismatch deserves a
    loud warning even though import itself does not raise.
    """
    try:
        import yaml
    except ImportError:
        return
    config_path = Path(__file__).resolve().parent.parent / "config" / "orchestrator.yml"
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        keys = list((data.get("routes") or {}).keys())
        # Orchestrator-only routes launched by dedicated endpoints (never via the
        # chat classifier) are exempt from the ROUTE_NAMES enum.
        assert_route_enum(
            keys,
            allowed_extra=frozenset(
                {"structure_analyze", "index_code"}
            ),
        )
    except Exception as exc:  # noqa: BLE001 — best-effort tripwire; never crash boot.
        logger.warning("route enum drift: %s", exc)


# Import-time tripwire: surface (never crash on) route-enum drift vs the
# executable orchestrator config.
_check_route_enum_drift()
