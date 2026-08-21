"""Per-response RAGAS evaluation (reference-free metrics).

Reuses the same Ollama judge models as the rest of the app so live scores are
comparable to the offline evaluation suite. See prompts/response_evaluation_view.MD.
"""

from __future__ import annotations

import math
import warnings
from typing import Any, Dict, List, Optional

from langchain_community.chat_models import ChatOllama
from langchain_community.embeddings import OllamaEmbeddings

# langchain-community's Ollama classes are deprecated in favour of
# langchain-ollama; suppress the noise since they still work and this project
# follows the "langchain-community" convention.
warnings.filterwarnings(
    "ignore",
    message=".*ChatOllama.*deprecated.*",
    category=DeprecationWarning,
)
warnings.filterwarnings(
    "ignore",
    message=".*OllamaEmbeddings.*deprecated.*",
    category=DeprecationWarning,
)

from ragas import evaluate
from ragas.dataset_schema import EvaluationDataset, SingleTurnSample
from ragas.embeddings import LangchainEmbeddingsWrapper
from ragas.llms import LangchainLLMWrapper
from ragas.metrics import (
    AnswerRelevancy,
    Faithfulness,
    LLMContextPrecisionWithoutReference,
)

from settings import settings

# Reference-free metrics only — context_recall needs ground truth and lives in
# the offline suite, not here. (display_name, metric) pairs; the metric's
# internal `.name` is the result-column key.
_METRICS = [
    ("faithfulness", Faithfulness()),
    ("answer_relevancy", AnswerRelevancy()),
    ("context_precision", LLMContextPrecisionWithoutReference()),
]

# Score -> human-readable tier (single source of truth, server-side).
def tier_for(score: float) -> str:
    if score >= 0.85:
        return "Excellent"
    if score >= 0.65:
        return "Good"
    if score >= 0.4:
        return "Needs review"
    return "Poor"


def _get_llm() -> LangchainLLMWrapper:
    return LangchainLLMWrapper(
        ChatOllama(
            model=settings.ollama_llm_model,
            base_url=settings.ollama_base_url,
            temperature=0,
        )
    )


def _get_embeddings() -> LangchainEmbeddingsWrapper:
    return LangchainEmbeddingsWrapper(
        OllamaEmbeddings(
            model=settings.ollama_embedding_model,
            base_url=settings.ollama_base_url,
        )
    )


def evaluate_response(
    query: str,
    answer: str,
    contexts: List[str],
) -> Dict[str, Any]:
    """Score a single (query, answer, contexts) triple.

    Returns ``{metric_name: {score, tier}}``. Raises on evaluation failure so
    the caller can surface a distinct error state.
    """
    sample = SingleTurnSample(
        user_input=query,
        response=answer,
        retrieved_contexts=contexts or [],
    )
    dataset = EvaluationDataset([sample])

    result = evaluate(
        dataset=dataset,
        metrics=[m for _, m in _METRICS],
        llm=_get_llm(),
        embeddings=_get_embeddings(),
        show_progress=False,
    )

    df = result.to_pandas()
    row = df.iloc[0]

    metrics: Dict[str, Any] = {}
    for display_name, metric in _METRICS:
        col = metric.name
        if col not in df.columns:
            continue
        value = row[col]
        if value is None or (isinstance(value, float) and math.isnan(value)):
            metrics[display_name] = {"score": None, "tier": "Unavailable"}
            continue
        score = float(value)
        metrics[display_name] = {"score": round(score, 4), "tier": tier_for(score)}

    return metrics
