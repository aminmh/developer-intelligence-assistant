"""Answer generation — builds the final LLM answer from retrieved code context.

Replaces the web Node server's answer path (RAGService.query). Retrieval is done
by the caller (CodePropertyGraph.hybrid_retrieve); this module only assembles the
prompt and calls the local Ollama chat model.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from exception_logger import get_exception_logger
from llm import complete
from settings import settings

SYSTEM_PROMPT = (
    "You are an expert developer assistant RAG system. Answer the user's coding "
    "query strictly using the retrieved code snippets and file context. If the "
    "retrieved context does not contain enough information to solve the request, "
    "state clearly what is missing and then use your broad pre-trained software "
    "development knowledge to give a comprehensive, helpful solution. Always "
    "output Markdown with complete type declarations and clear syntax-highlighted "
    "code blocks where appropriate."
)


def build_user_prompt(
    question: str,
    contexts: List[Dict[str, Any]],
    entities: Optional[Dict[str, Any]] = None,
    extra_context: Optional[str] = None,
) -> str:
    """Assemble the user-facing prompt: tool context + code + entities + question."""
    sections: List[str] = []

    if extra_context:
        sections.append(extra_context)

    if contexts:
        blocks = []
        for i, ctx in enumerate(contexts):
            name = ctx.get("name") or ctx.get("class") or "symbol"
            file = ctx.get("file") or ""
            code = ctx.get("code") or ""
            blocks.append(
                f"[{i + 1}] {file} | {name}\n```\n{code}\n```"
            )
        sections.append(
            "Retrieved code context:\n\n" + "\n\n".join(blocks)
        )

    if entities:
        summary = ", ".join(
            filter(
                None,
                [
                    entities.get("intent") and f"Intent: {entities['intent']}",
                    entities.get("class_name") and f"Class: {entities['class_name']}",
                    entities.get("method_name") and f"Method: {entities['method_name']}",
                    entities.get("target_file") and f"File: {entities['target_file']}",
                    entities.get("error_code") and f"Error: {entities['error_code']}",
                ],
            )
        )
        if summary:
            sections.append(f"Detected context: {summary}")

    sections.append(f"User question: {question}")
    return "\n\n".join(sections)


def _log_prompt(question: str, prompt: str) -> None:
    """Append the final prompt to a log file for later inspection."""
    try:
        log_path = Path(settings.log_dir) / "llm-prompts.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = "\n".join(
            [
                f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}]",
                f"USER_QUERY: {question}",
                "FINAL_PROMPT:",
                prompt,
                "",
                "-" * 120,
                "",
            ]
        )
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(entry)
    except Exception:
        pass  # Log file is best-effort


def generate_answer(
    question: str,
    contexts: Optional[List[Dict[str, Any]]] = None,
    entities: Optional[Dict[str, Any]] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    extra_context: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate an answer from retrieved code context + entities."""
    contexts = contexts or []
    prompt = build_user_prompt(question, contexts, entities, extra_context)

    temp = settings.temperature if temperature is None else temperature
    topp = settings.top_p if top_p is None else top_p

    _log_prompt(question, prompt)

    started = time.time()
    try:
        answer = complete(
            prompt,
            system=SYSTEM_PROMPT,
            temperature=temp,
            top_p=topp,
        )
    except Exception as exc:
        get_exception_logger().error("Answer generation failed: %s", exc)
        answer = (
            "I couldn't generate an answer because the local model is "
            "temporarily unavailable. Please try again.\n\n"
            f"Error: {exc}"
        )
    duration_ms = int((time.time() - started) * 1000)

    validation: List[Dict[str, Any]] = []
    if getattr(settings, "lsp_validation_enabled", True):
        try:
            from lsp import build_note, validate_markdown

            new_answer, summary = validate_markdown(
                answer,
                question,
                max_retries=getattr(settings, "lsp_max_retries", 2),
            )
            if summary:
                answer = new_answer + "\n\n" + build_note(summary)
                validation = [
                    {
                        "language": s["language"],
                        "index": s["index"],
                        "status": s["status"],
                        "errors": s["errors"],
                        "warnings": s["warnings"],
                    }
                    for s in summary
                ]
        except Exception as exc:
            get_exception_logger().warning("LSP validation skipped: %s", exc)

    return {
        "answer": answer,
        "model": settings.ollama_llm_model,
        "duration_ms": duration_ms,
        "code_validation": validation,
    }
