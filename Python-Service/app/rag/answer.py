"""Answer generation — builds the final LLM answer from retrieved code context.

Replaces the web Node server's answer path (RAGService.query). Retrieval is done
by the caller (CodePropertyGraph.hybrid_retrieve); this module only assembles the
prompt and calls the local Ollama chat model.

Framework/language resolution does NOT live here:
  * project-profile grounding  -> project_knowledge.build_grounding()
  * LSP post-validation of code -> code_validation.validate_answer()
This module stays generic — it knows neither frameworks nor language adapters.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from rag.code_validation import validate_answer
from infra.exception_logger import get_exception_logger
from runtime.llm import complete_with_model, stream_complete_with_model
from projects.project_knowledge import build_grounding
from infra.settings import settings

SYSTEM_PROMPT = (
    "You are an expert developer assistant RAG system. Answer the user's coding "
    "query strictly using the retrieved code snippets and file context. If the "
    "retrieved context does not contain enough information to solve the request, "
    "state clearly what is missing and then use your broad pre-trained software "
    "development knowledge to give a comprehensive, helpful solution. Always "
    "output Markdown with complete type declarations and clear syntax-highlighted "
    "code blocks where appropriate.\n\n"
    "CRITICAL: Answer the user's question directly. Never restate, quote, or "
    "reproduce the retrieved context verbatim in your answer. The retrieved code "
    "is source material only — cite or summarize from it, do not dump it."
)

# Cap retrieved context so the model answers instead of echoing the dump.
MAX_CONTEXT_BLOCKS = 8
MAX_CONTEXT_CHARS = 2500

# Documentation grounding (Context7/GitMCP) rides in as its own section with a
# larger budget than code blocks — it is concise reference material, not source.
MAX_DOC_SECTIONS = 6
MAX_DOC_CHARS = 4000


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n… (truncated)"


def render_context_window(system: str, prompt: str) -> str:
    """Render the exact context window (system + user prompt) as markdown."""
    return f"# System prompt\n\n{system}\n\n---\n\n# Context window sent to model\n\n{prompt}"


def build_user_prompt(
    question: str,
    contexts: List[Dict[str, Any]],
    entities: Optional[Dict[str, Any]] = None,
    extra_context: Optional[str] = None,
    doc_chunks: Optional[List[str]] = None,
    project_profile: Optional[Dict[str, Any]] = None,
) -> str:
    """Assemble the user-facing prompt: project + docs + tool context + code."""
    sections: List[str] = []

    if extra_context:
        sections.append(_cap(extra_context, MAX_CONTEXT_CHARS))

    project_block = build_grounding(project_profile, question)
    if project_block:
        sections.append(
            "Analyzed project (framework-aware context — answer using these "
            "facts; if the routes above contain no match, say so):\n\n"
            + project_block
        )

    if doc_chunks:
        docs = []
        for i, chunk in enumerate(doc_chunks[:MAX_DOC_SECTIONS]):
            docs.append(f"--- Library doc {i + 1} ---\n{_cap(chunk, MAX_DOC_CHARS)}")
        sections.append(
            "Current library documentation (use it when it is relevant; "
            "ignore what is not):\n\n" + "\n\n".join(docs)
        )

    if contexts:
        blocks = []
        for i, ctx in enumerate(contexts[:MAX_CONTEXT_BLOCKS]):
            name = ctx.get("name") or ctx.get("class") or "symbol"
            file = ctx.get("file") or ""
            code = ctx.get("code") or ""
            if len(code) > MAX_CONTEXT_CHARS:
                code = code[:MAX_CONTEXT_CHARS] + "\n… (truncated)"
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
    doc_chunks: Optional[List[str]] = None,
    project_profile: Optional[Dict[str, Any]] = None,
    use_remote: bool = False,
    route: str = "chat",
    preview_window: str | None = None,
    preview_only: bool = False,
    *,
    preview_mode: str = "off",
) -> Dict[str, Any]:
    """Generate an answer from retrieved code context + entities.

    With ``preview_only`` the model is never invoked: the caller receives only
    the rendered context window (``context_preview`` + ``preview_only: True``).
    Otherwise the window is always attached as ``context_preview`` so inline
    callers can surface what the model saw. ``preview_mode`` is accepted for
    signature parity with the stream path; callers decide whether to surface
    ``context_preview`` based on the resolved mode.
    """
    contexts = contexts or []
    prompt = build_user_prompt(
        question, contexts, entities, extra_context, doc_chunks, project_profile
    )
    window = (
        preview_window
        if preview_window is not None
        else render_context_window(SYSTEM_PROMPT, prompt)
    )

    if preview_only:
        return {
            "answer": None,
            "model": None,
            "provider": None,
            "duration_ms": 0,
            "code_validation": [],
            "context_preview": window,
            "preview_only": True,
        }

    temp = settings.temperature if temperature is None else temperature
    topp = settings.top_p if top_p is None else top_p

    _log_prompt(question, prompt)

    started = time.time()
    try:
        answer, model_used, provider_used = complete_with_model(
            prompt,
            system=SYSTEM_PROMPT,
            temperature=temp,
            top_p=topp,
            provider="remote" if use_remote else "local",
        )
    except Exception as exc:
        get_exception_logger().error("Answer generation failed: %s", exc)
        answer = (
            "I couldn't generate an answer because the local model is "
            "temporarily unavailable. Please try again.\n\n"
            f"Error: {exc}"
        )
        model_used = settings.ollama_llm_model
        provider_used = "local"
    duration_ms = int((time.time() - started) * 1000)

    answer, validation = validate_answer(answer, question, route)

    return {
        "answer": answer,
        "model": model_used,
        "provider": provider_used,
        "duration_ms": duration_ms,
        "code_validation": validation,
        "context_preview": window,
    }


def generate_answer_stream(
    question: str,
    contexts: Optional[List[Dict[str, Any]]] = None,
    entities: Optional[Dict[str, Any]] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    extra_context: Optional[str] = None,
    doc_chunks: Optional[List[str]] = None,
    project_profile: Optional[Dict[str, Any]] = None,
    use_remote: bool = False,
    route: str = "chat",
    preview_window: str | None = None,
    preview_only: bool = False,
    *,
    preview_mode: str = "off",
) -> Any:
    """Generator yielding answer events for SSE streaming.

    Events: ``start`` (model/provider), ``delta`` (text), ``error``,
    ``validation`` (code_validation list), ``done`` (answer, model, provider,
    duration_ms). A leading ``context`` event (the rendered context window)
    precedes the stream whenever ``preview_mode`` is not "off"; with
    ``preview_only`` that context event is the whole response (emitted
    regardless of ``preview_mode``) and the model is never invoked.
    """
    contexts = contexts or []
    prompt = build_user_prompt(
        question, contexts, entities, extra_context, doc_chunks, project_profile
    )
    window = (
        preview_window
        if preview_window is not None
        else render_context_window(SYSTEM_PROMPT, prompt)
    )

    if preview_only:
        yield {"type": "context", "preview": window}
        yield {
            "type": "done",
            "answer": None,
            "model": None,
            "provider": None,
            "duration_ms": 0,
            "truncated": False,
            "code_validation": [],
            "preview_only": True,
        }
        return

    temp = settings.temperature if temperature is None else temperature
    topp = settings.top_p if top_p is None else top_p

    _log_prompt(question, prompt)

    if preview_mode != "off":
        yield {"type": "context", "preview": window}

    started = time.time()
    buffer: List[str] = []
    try:
        for delta, model_used, provider_used in stream_complete_with_model(
            prompt,
            system=SYSTEM_PROMPT,
            temperature=temp,
            top_p=topp,
            provider="remote" if use_remote else "local",
        ):
            if delta:
                buffer.append(delta)
                yield {"type": "delta", "text": delta}
            elif not buffer:
                yield {"type": "start", "model": model_used, "provider": provider_used}
    except Exception as exc:  # noqa: BLE001 — report any failure shape to the caller as an error event
        get_exception_logger().error("Streaming answer failed: %s", exc)
        if not buffer:
            yield {
                "type": "error",
                "detail": "I couldn't generate an answer because the model is "
                "temporarily unavailable. Please try again.",
            }
            return
        # Mid-stream failure — surface what we have and stop.
        answer = "".join(buffer)
        yield {
            "type": "done",
            "answer": answer,
            "model": settings.ollama_llm_model,
            "provider": "local",
            "duration_ms": int((time.time() - started) * 1000),
            "truncated": True,
            "code_validation": [],
        }
        return

    answer = "".join(buffer)
    answer, validation = validate_answer(answer, question, route)
    yield {"type": "validation", "code_validation": validation}
    yield {
        "type": "done",
        "answer": answer,
        "model": model_used,
        "provider": provider_used,
        "duration_ms": int((time.time() - started) * 1000),
        "truncated": False,
        "code_validation": validation,
    }
