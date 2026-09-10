"""Intent & entity extraction (roadmap step 3).

Turns a raw user prompt into a machine-readable `PromptIntent` using a
structured-output LLM (Instructor + Ollama). Extraction failures degrade
gracefully to a neutral intent with empty fields.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from infra.exception_logger import get_exception_logger
from runtime.llm import extract
from rag.code_query_signals import infer_code_query

ROUTE_INTENTS = {
    "explain",
    "refactor",
    "debug",
    "search",
    "chat",
    "knowledge",
}

_INTENT_SYNONYMS = {
    "fix": "debug",
    "bug": "debug",
    "error": "debug",
    "exception": "debug",
    "crash": "debug",
    "find": "search",
    "locate": "search",
    "where": "search",
    "lookup": "search",
    "summarize": "explain",
    "describe": "explain",
    "understand": "explain",
    "improve": "refactor",
    "cleanup": "refactor",
    "clean": "refactor",
    "optimize": "refactor",
    "architecture": "knowledge",
    "config": "knowledge",
    "setup": "knowledge",
    "conversation": "chat",
    "greet": "chat",
}

def _infer_from_prompt(prompt: str) -> PromptIntent:
    """Recover high-value entities when the structured extractor is offline."""
    inferred = infer_code_query(prompt)
    return PromptIntent(
        intent=str(inferred["intent"]),
        target_file=inferred["target_file"],
        class_name=inferred["class_name"],
    )


class PromptIntent(BaseModel):
    intent: str = Field(
        default="chat",
        description="One of: explain, refactor, debug, search, chat, knowledge",
    )
    target_file: Optional[str] = Field(
        default=None, description="Target file the user is asking about"
    )
    class_name: Optional[str] = Field(
        default=None, description="Class/service name the user is asking about"
    )
    method_name: Optional[str] = Field(
        default=None, description="Method/function name the user is asking about"
    )
    action_type: Optional[str] = Field(
        default=None, description="Action verb, e.g. explain, fix, refactor, locate"
    )
    error_code: Optional[str] = Field(
        default=None, description="Error code or exception message if present"
    )
    framework: Optional[str] = Field(
        default=None, description="Framework or language, e.g. PHP, Laravel"
    )
    constraints: List[str] = Field(
        default_factory=list,
        description="Explicit constraints or requirements the user mentioned",
    )


_EXTRACTION_PROMPT = """Extract intent and entities from the user's developer request.

Return ONLY a JSON object with exactly these keys (use null for missing values):
- intent: one of "explain", "refactor", "debug", "search", "chat", "knowledge"
- target_file: the file being referred to (e.g. "UserController.php")
- class_name: the class name being referred to
- method_name: the method/function name being referred to
- action_type: short action verb (explain, fix, refactor, locate, ...)
- error_code: any error code or exception message mentioned
- framework: the language or framework (e.g. "PHP", "Laravel")
- constraints: array of explicit constraints/requirements

User request: {prompt}
"""


def extract_intent(prompt: str) -> PromptIntent:
    """Extract a `PromptIntent` from the prompt, degrading gracefully on failure."""
    if not prompt or not isinstance(prompt, str):
        return PromptIntent(intent="chat")

    try:
        result = extract(PromptIntent, _EXTRACTION_PROMPT.format(prompt=prompt))
        if isinstance(result, PromptIntent):
            return _normalize(result, prompt)
    except Exception as exc:
        get_exception_logger().warning("Intent extraction failed: %s", exc)

    return _infer_from_prompt(prompt)


def _normalize(intent: PromptIntent, prompt: str = "") -> PromptIntent:
    value = (intent.intent or "").strip().lower()
    if value in ROUTE_INTENTS:
        intent.intent = value
    else:
        intent.intent = _INTENT_SYNONYMS.get(value, "chat")
    inferred = _infer_from_prompt(prompt) if prompt else PromptIntent(intent="chat")
    if not intent.class_name and inferred.class_name:
        intent.class_name = inferred.class_name
    if not intent.target_file and inferred.target_file:
        intent.target_file = inferred.target_file
    # A named symbol is a stronger boundary than a generic model answer of
    # "knowledge" or "chat" for questions such as "how CommentService works?".
    if inferred.class_name and intent.intent in {"chat", "knowledge"}:
        intent.intent = inferred.intent
    return intent
