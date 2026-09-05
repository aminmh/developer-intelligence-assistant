"""Per-response RAGAS evaluation (reference-free metrics).

Reuses the same Ollama judge models as the rest of the app so live scores are
comparable to the offline evaluation suite. See prompts/response_evaluation_view.MD.
"""

from __future__ import annotations

import math
import warnings
from typing import Any

from langchain_community.chat_models import ChatOllama

# langchain-community's Ollama classes are deprecated in favour of
# langchain-ollama; suppress the noise since they still work and this project
# follows the "langchain-community" convention.
warnings.filterwarnings(
    "ignore",
    message=".*ChatOllama.*deprecated.*",
    category=DeprecationWarning,
)

from ragas import evaluate
from ragas.dataset_schema import EvaluationDataset, SingleTurnSample
from ragas.embeddings import LangchainEmbeddingsWrapper
from ragas.llms import LangchainLLMWrapper
from ragas.metrics import (
    AnswerRelevancy,
    Faithfulness,
)

from infra.settings import settings

# Reference-free metrics only — context_recall needs ground truth and lives in
# the offline suite, not here. (display_name, metric) pairs; the metric's
# internal `.name` is the result-column key. Context precision is computed
# locally from embeddings (see _embedding_context_precision): RAGAS's LLM
# variant demands strict-JSON verdicts that small local judge models do not
# produce reliably, which made it always return 0.
_METRICS = [
    ("faithfulness", Faithfulness()),
    ("answer_relevancy", AnswerRelevancy()),
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
            model=settings.ollama_eval_model,
            base_url=settings.ollama_base_url,
            temperature=0,
        )
    )


def _get_embeddings() -> LangchainEmbeddingsWrapper:
    from index.embeddings import get_langchain_embeddings

    return LangchainEmbeddingsWrapper(get_langchain_embeddings())


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity with zero-vector safety."""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _embedding_context_precision(
    query: str,
    contexts: list[str],
    embeddings: LangchainEmbeddingsWrapper,
    threshold: float = 0.5,
) -> dict[str, Any]:
    """Reference-free context precision from embeddings (no LLM involved).

    Mirrors RAGAS average-precision semantics: a context counts as relevant
    when its cosine similarity to the question exceeds ``threshold``; the
    score is the mean precision@k over the retrieval order.
    """
    q_vec = embeddings.embed_query(query)
    c_vecs = embeddings.embed_documents(contexts)
    verdicts = [_cosine(q_vec, c_vec) > threshold for c_vec in c_vecs]
    relevant = sum(verdicts)
    if relevant == 0:
        score = 0.0
    else:
        hits = 0
        precisions = []
        for i, verdict in enumerate(verdicts):
            if not verdict:
                continue
            hits += 1
            precisions.append(hits / (i + 1))
        score = sum(precisions) / relevant
    return {"score": round(score, 4), "tier": tier_for(score)}


def evaluate_response(
    query: str,
    answer: str,
    contexts: list[str],
) -> dict[str, Any]:
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

    metrics: dict[str, Any] = {}
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

    # Local, deterministic context precision (embeddings only — no LLM call).
    if contexts:
        metrics["context_precision"] = _embedding_context_precision(
            query, contexts, _get_embeddings()
        )
    else:
        metrics["context_precision"] = {"score": None, "tier": "Unavailable"}

    return metrics
