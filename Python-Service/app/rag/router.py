"""Semantic Router — classifies user prompts using embedding similarity.

Adds a feedback loop (query corrections persisted to Qdrant) and a hybrid
LLM fallback for ambiguous/low-confidence cases. Embedding-based routing stays
the default fast path.
"""

import json
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from semantic_router.encoders import BaseEncoder
from semantic_router.route import Route
from semantic_router.layer import RouteLayer

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

from infra.settings import settings
from rag.query_normalizer import normalize_query

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


class OllamaEncoder(BaseEncoder):
    type: str = "ollama"
    score_threshold: float = 0.3

    def __init__(self, name: str = "ollama"):
        super().__init__(name=name)

    def __call__(self, docs: List[str]) -> List[List[float]]:
        from index.embeddings import embed_texts

        return embed_texts([normalize_query(doc) for doc in docs])


code_explain = Route(
    name="code_explain",
    utterances=[
        "Explain this code",
        "Summarize what this function does",
        "Describe the logic of this file",
        "What does this method do?",
        "Explain this PHP class",
        "Walk me through this function",
        "Break down this code step by step",
        "Tell me what this module does",
    ],
)

code_refactor = Route(
    name="code_refactor",
    utterances=[
        "Refactor this code",
        "Improve the structure of this class",
        "Rewrite this function to be cleaner",
        "Optimize this code and reduce duplication",
        "Suggest a safer refactor for this file",
        "Make this code more maintainable",
        "Split this method into smaller parts",
        "Improve naming and structure here",
        "Clean up this legacy code",
    ],
)

debug = Route(
    name="debug",
    utterances=[
        "I have an exception",
        "This code crashes at runtime",
        "Find the bug in this function",
        "Debug this error",
        "Why is this failing?",
        "Fix this runtime issue",
        "Why am I getting this stack trace?",
        "Track down this bug",
        "This throws an error",
    ],
)

search = Route(
    name="search",
    utterances=[
        "Find where this function is defined",
        "Search for callers of this method",
        "Locate imports of this class",
        "Show me files related to this symbol",
        "Search the codebase for this term",
        "Where is this variable used?",
        "Find references to this method",
        "Show all usages of this class",
        "Locate this symbol in the repo",
    ],
)

knowledge = Route(
    name="knowledge",
    utterances=[
        "Explain the project architecture",
        "How is this system configured?",
        "Where are the environment settings?",
        "Show me the deployment details",
        "What is the schema for this module?",
        "Explain the repository structure",
        "How does this service work?",
        "Where are the config files?",
        "What is the architecture of this app?",
        "Show me the system design",
    ],
)

chat = Route(
    name="chat",
    utterances=[
        "Hello",
        "Thanks",
        "Goodbye",
        "How are you?",
        "General conversation",
        "Hi",
        "Hey",
        "Thank you",
        "Bye",
    ],
)

encoder = OllamaEncoder()

routes = [
    code_explain,
    code_refactor,
    debug,
    search,
    knowledge,
    chat,
]

ROUTE_NAMES = [r.name for r in routes]

ROUTE_DESCRIPTIONS = {
    "code_explain": "Explain or summarize what code does",
    "code_refactor": "Refactor, clean up, or improve code structure",
    "debug": "Debug errors, crashes, or bugs",
    "search": "Find/search symbols, references, or usages",
    "knowledge": "Explain project architecture or configuration",
    "chat": "General conversation or small talk",
}

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


def _top_route_scores(query_vec: List[float], top_k: int = 20) -> List[Tuple[str, float]]:
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
    if top1 < AMBIGUOUS_SCORE_THRESHOLD:
        return True
    if len(ranked) >= 2 and (ranked[0][1] - ranked[1][1]) < AMBIGUOUS_MARGIN:
        return True
    return False


def _build_selector_prompt(query: str) -> str:
    lines = [
        "You are a routing classifier. Choose exactly ONE route from the list "
        "that best matches the user query.",
        "Return only valid JSON: {\"route\": \"<route_name>\"}",
        "",
        "Routes:",
    ]
    for name in ROUTE_NAMES:
        lines.append(f"- {name}: {ROUTE_DESCRIPTIONS.get(name, name)}")
    lines.append("")
    lines.append(f"Query: {query}")
    return "\n".join(lines)


def _llm_select_route(query: str) -> Optional[str]:
    """Ask the configured Ollama chat model to pick a route for an ambiguous query."""
    body = json.dumps(
        {
            "model": settings.ollama_llm_model,
            "prompt": _build_selector_prompt(query),
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
    except Exception:
        return None

    text = data.get("response") or ""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            route = parsed.get("route")
            if route in ROUTE_NAMES:
                return route
    except Exception:
        pass

    # Fallback: scan raw text for the first known route name.
    for name in ROUTE_NAMES:
        if name in text:
            return name
    return None


def _log_fallback(
    query: str,
    embedding_route: str,
    embedding_score: float,
    ranked: List[Tuple[str, float]],
    llm_route: Optional[str],
) -> None:
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
                "llm_route": llm_route,
                "final_choice": llm_route or embedding_route,
            },
            default=str,
        )
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(entry + "\n")
    except Exception:
        pass


def classify(prompt: str) -> dict:
    """Classify a user prompt and return {route, confidence, source?}."""
    if not _init_router():
        return {"route": "chat", "confidence": 0.0}

    try:
        query_vec = encoder([prompt])[0]
    except Exception:
        return {"route": "chat", "confidence": 0.0}

    # 1) Feedback override — a stored correction matches almost exactly.
    forced = _feedback_override(query_vec)
    if forced:
        return {"route": forced, "confidence": 1.0, "source": "feedback"}

    # 2) Normal embedding-based routing (default fast path).
    ranked = _top_route_scores(query_vec)
    if not ranked:
        return {"route": "chat", "confidence": 0.0}

    route = ranked[0][0]
    top1 = ranked[0][1]

    # 3) Ambiguity detection -> LLM fallback.
    if _is_ambiguous(top1, ranked):
        llm_route = _llm_select_route(prompt)
        _log_fallback(prompt, route, top1, ranked, llm_route)
        if llm_route:
            return {
                "route": llm_route,
                "confidence": round(top1, 4),
                "source": "llm_fallback",
            }

    return {
        "route": route,
        "confidence": round(top1, 4),
    }
