"""Semantic Router — classifies user prompts using embedding similarity."""

import json
import urllib.request
from typing import List

from semantic_router.encoders import BaseEncoder
from semantic_router.route import Route
from semantic_router.layer import RouteLayer

from settings import settings


class OllamaEncoder(BaseEncoder):
    type: str = "ollama"
    score_threshold: float = 0.3

    def __init__(self, name: str = "ollama"):
        super().__init__(name=name)

    def __call__(self, docs: List[str]) -> List[List[float]]:
        embeddings = []

        for doc in docs:
            body = json.dumps(
                {
                    "model": settings.ollama_embedding_model,
                    "prompt": doc,
                }
            ).encode("utf-8")

            req = urllib.request.Request(
                f"{settings.ollama_base_url}/api/embeddings",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=120) as response:
                data = json.loads(response.read().decode("utf-8"))

            embedding = data.get("embedding")
            if not embedding or not isinstance(embedding, list):
                raise RuntimeError("Invalid embedding returned from Ollama")

            embeddings.append(embedding)

        return embeddings


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


def classify(prompt: str) -> dict:
    """Classify a user prompt and return {route, confidence}."""
    if not _init_router():
        return {"route": "chat", "confidence": 0.0}
    try:
        result = router(prompt)
        score = result.similarity_score if result.similarity_score is not None else 1.0

        return {
            "route": result.name,
            "confidence": round(score, 4) if isinstance(score, (int, float)) else 1.0,
        }

    except Exception:
        return {"route": "chat", "confidence": 0.0}
