"""Semantic Router — classifies user prompts using embedding similarity."""

import json
import urllib.request
from typing import List

from semantic_router.encoders import BaseEncoder
from semantic_router.route import Route
from semantic_router.layer import RouteLayer

from settings import settings


class OllamaEncoder(BaseEncoder):
    """Semantic Router encoder backed by Ollama embeddings."""

    type: str = "ollama"
    score_threshold: float = 0.3

    def __init__(self, name: str = "ollama"):
        super().__init__(name=name)

    def __call__(self, docs: List[str]) -> List[List[float]]:
        embeddings = []
        for text in docs:
            body = json.dumps({
                "model": settings.ollama_embedding_model,
                "prompt": text,
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{settings.ollama_base_url}/api/embeddings",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.load(resp)
            emb = result.get("embedding")
            if not emb or not isinstance(emb, list):
                raise RuntimeError("Ollama did not return a valid embedding")
            embeddings.append(emb)
        return embeddings


# ── Route definitions ──────────────────────────────────────────────────────

code_explain = Route(
    name="code_explain",
    utterances=[
        "Explain this function",
        "What does this method do?",
        "How does this class work?",
        "Walk me through this code",
        "What is the purpose of this file?",
        "Describe the logic here",
        "Explain the architecture",
        "How are these classes connected?",
        "What does this variable represent?",
        "Explain the control flow",
        "Can you summarize this module?",
        "What design pattern is used here?",
    ],
)

code_refactor = Route(
    name="code_refactor",
    utterances=[
        "Refactor this code",
        "Rewrite this method",
        "Improve this function",
        "Clean up this class",
        "Optimize this loop",
        "Make this code more readable",
        "Split this into smaller functions",
        "Extract this logic",
        "Apply SOLID principles",
        "Reduce duplication here",
        "Convert to async",
        "Add type hints",
    ],
)

debug = Route(
    name="debug",
    utterances=[
        "Why does this error happen?",
        "Fix this bug",
        "Debug the crash",
        "What is causing the null pointer?",
        "Why is this returning undefined?",
        "Trace the source of this exception",
        "How do I fix this TypeError?",
        "The app crashes when I call this",
        "Find the memory leak",
        "Why is the output wrong?",
        "Investigate the timeout",
        "Resolve this stack trace",
    ],
)

search = Route(
    name="search",
    utterances=[
        "Find where this is defined",
        "Search for the implementation of",
        "Where is this function called?",
        "Show me all references to",
        "List the files that use",
        "Find the callers of this method",
        "Where is the class instantiated?",
        "Locate the config file",
        "Search the codebase for",
        "Which file contains the route definition?",
        "Find imports of",
        "Grep the project for",
    ],
)

knowledge = Route(
    name="knowledge",
    utterances=[
        "What is the project architecture?",
        "How do I set up the environment?",
        "Tell me about the database schema",
        "What technologies are used?",
        "Explain the deployment process",
        "How does authentication work?",
        "What are the API endpoints?",
        "Describe the data model",
        "How is logging configured?",
        "What is the testing strategy?",
        "Documentation for the pipeline",
        "What external services do we use?",
    ],
)

chat = Route(
    name="chat",
    utterances=[
        "Hello",
        "Hi there",
        "Thanks",
        "Goodbye",
        "What can you do?",
        "How are you?",
        "Tell me a joke",
        "What's new?",
        "Good morning",
        "Nice work",
        "Help",
        "I need assistance",
    ],
)

# ── Router instance ──────────────────────────────────────────────────────────

encoder = OllamaEncoder()

routes = [code_explain, code_refactor, debug, search, knowledge, chat]

router = RouteLayer(encoder=encoder, routes=routes)


def classify(prompt: str) -> dict:
    """Classify a user prompt and return {route, confidence}."""
    try:
        result = router(prompt)
        score = result.similarity_score if result.similarity_score is not None else 1.0
        return {
            "route": result.name,
            "confidence": round(score, 4) if isinstance(score, (int, float)) else 1.0,
        }
    except Exception:
        return {"route": "chat", "confidence": 0.0}
