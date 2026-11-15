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

import itertools
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


def _estimate_tokens(text: str) -> int:
    """Conservative tokenizer-free estimate for providers without usage data."""
    return max(1, (len(text) + 3) // 4) if text else 0


def _usage_metadata(prompt: str, answer: str, provider_usage: dict | None) -> tuple[dict, dict | None]:
    if provider_usage:
        usage = dict(provider_usage)
    else:
        usage = {
            "prompt_tokens": _estimate_tokens(SYSTEM_PROMPT + "\n" + prompt),
            "completion_tokens": _estimate_tokens(answer),
            "total_tokens": _estimate_tokens(SYSTEM_PROMPT + "\n" + prompt) + _estimate_tokens(answer),
            "estimated": True,
            "source": "estimate",
        }
    limit = int(getattr(settings, "llm_context_window_tokens", 0) or 0)
    input_tokens = usage.get("prompt_tokens")
    context_usage = None
    if limit > 0 and isinstance(input_tokens, int):
        context_usage = {
            "input_tokens": input_tokens,
            "limit_tokens": limit,
            "percent": round(input_tokens / limit * 100, 1),
            "estimated": bool(usage.get("estimated")),
        }
    return usage, context_usage


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


def _start_agent(prompt: str, tool_context: Any, temperature: float, top_p: float,
                 run_meta: Optional[Dict[str, Any]] = None):
    """Start the tool-calling agent; return ``(first_event, events)`` or None.

    None means the agent could not start and the caller keeps the existing
    single-call generation (LangChain plan task 18).
    """
    if tool_context is None:
        return None
    from agent_tools.runtime import AgentUnavailable, stream_agent_answer

    events = stream_agent_answer(prompt, SYSTEM_PROMPT, tool_context, temperature=temperature, top_p=top_p,
                                 run_meta=run_meta)
    try:
        return next(events), events
    except AgentUnavailable as exc:
        get_exception_logger().warning("Agent unavailable, answering without tools: %s", exc)
    except StopIteration:
        pass
    return None


def _agent_done(event: Dict[str, Any], question: str, prompt: str, route: str, started: float) -> Dict[str, Any]:
    answer, validation = validate_answer(event.get("answer") or "", question, route)
    usage, context_usage = _usage_metadata(prompt, answer, event.get("usage"))
    return {
        "answer": answer,
        "model": event.get("model"),
        "provider": event.get("provider", "remote"),
        "duration_ms": int((time.time() - started) * 1000),
        "truncated": bool(event.get("truncated")),
        "code_validation": validation,
        "usage": usage,
        "context_usage": context_usage,
        "tool_calls": event.get("tool_calls") or [],
    }


def _agent_paused(event: Dict[str, Any], approval: Dict[str, Any] | None, started: float) -> Dict[str, Any]:
    """A ``done`` payload for a run waiting on the user's approval of a tool call."""
    return {
        "answer": event.get("answer") or "",
        "model": event.get("model"),
        "provider": event.get("provider", "remote"),
        "duration_ms": int((time.time() - started) * 1000),
        "truncated": False,
        "code_validation": [],
        "usage": event.get("usage"),
        "context_usage": None,
        "tool_calls": event.get("tool_calls") or [],
        "paused": True,
        "approval": approval,
    }


def _run_meta(question: str, prompt: str, route: str, run_meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {**(run_meta or {}), "question": question, "prompt": prompt, "route": route}


def resume_answer_stream(approval: Any) -> Any:
    """Continue a paused answer after its approval was decided; yields answer events.

    The decision was already consumed (``ApprovalStore.decide``), so this runs
    at most once per approval. The final ``done`` carries the extras the
    original request attached (matches, business rules, tool status).
    """
    from agent_tools.runtime import resume_agent_answer

    run = approval.run
    started = time.time()
    extras = run.get("extras") or {}
    yield {"type": "start", "model": run.get("model"), "provider": "remote"}
    approval_event: Dict[str, Any] | None = None
    try:
        for event in resume_agent_answer(approval):
            if event.get("type") == "approval_required":
                approval_event = event
                yield event
            elif event.get("type") == "agent_paused":
                yield {"type": "done", **_agent_paused(event, approval_event, started), **extras}
                return
            elif event.get("type") == "agent_done":
                done = _agent_done(event, run.get("question", ""), run.get("prompt", ""), run.get("route", "chat"), started)
                yield {"type": "validation", "code_validation": done["code_validation"]}
                yield {"type": "done", **done, **extras}
                return
            else:
                yield event
    except Exception as exc:  # noqa: BLE001 — the decision is consumed; report, never retry silently
        get_exception_logger().error("Resuming agent run failed: %s", exc)
        yield {"type": "error", "detail": "The approved run could not continue. Ask the question again."}


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
    tool_context: Any = None,
    run_meta: Optional[Dict[str, Any]] = None,
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
    agent = _start_agent(prompt, tool_context, temp, topp, _run_meta(question, prompt, route, run_meta))
    if agent is not None:
        first, events = agent
        approval_event: Dict[str, Any] | None = None
        for event in itertools.chain([first], events):
            if event.get("type") == "approval_required":
                approval_event = event
            elif event.get("type") == "agent_paused":
                return {**_agent_paused(event, approval_event, started), "context_preview": window}
            elif event.get("type") == "agent_done":
                return {**_agent_done(event, question, prompt, route, started), "context_preview": window}
    try:
        completion = complete_with_model(
            prompt,
            system=SYSTEM_PROMPT,
            temperature=temp,
            top_p=topp,
            provider="remote" if use_remote else "local",
        )
        answer, model_used, provider_used = completion
        provider_usage = getattr(completion, "usage", None)
    except Exception as exc:
        get_exception_logger().error("Answer generation failed: %s", exc)
        answer = (
            "I couldn't generate an answer because the local model is "
            "temporarily unavailable. Please try again.\n\n"
            f"Error: {exc}"
        )
        model_used = settings.ollama_llm_model
        provider_used = "local"
        provider_usage = None
    duration_ms = int((time.time() - started) * 1000)

    answer, validation = validate_answer(answer, question, route)
    usage, context_usage = _usage_metadata(prompt, answer, provider_usage)

    return {
        "answer": answer,
        "model": model_used,
        "provider": provider_used,
        "duration_ms": duration_ms,
        "code_validation": validation,
        "context_preview": window,
        "usage": usage,
        "context_usage": context_usage,
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
    tool_context: Any = None,
    run_meta: Optional[Dict[str, Any]] = None,
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
    agent = _start_agent(prompt, tool_context, temp, topp, _run_meta(question, prompt, route, run_meta))
    if agent is not None:
        first, events = agent
        from runtime.llm import remote_endpoint

        yield {"type": "start", "model": remote_endpoint()[2], "provider": "remote"}
        approval_event: Dict[str, Any] | None = None
        for event in itertools.chain([first], events):
            if event.get("type") == "approval_required":
                approval_event = event
            if event.get("type") == "agent_paused":
                yield {"type": "done", **_agent_paused(event, approval_event, started)}
                return
            if event.get("type") == "agent_done":
                done = _agent_done(event, question, prompt, route, started)
                yield {"type": "validation", "code_validation": done["code_validation"]}
                yield {"type": "done", **done}
                return
            yield event
        return

    buffer: List[str] = []
    provider_usage = None
    model_used = settings.ollama_llm_model
    provider_used = "local"
    try:
        for chunk in stream_complete_with_model(
            prompt,
            system=SYSTEM_PROMPT,
            temperature=temp,
            top_p=topp,
            provider="remote" if use_remote else "local",
        ):
            delta, model_used, provider_used = chunk
            provider_usage = getattr(chunk, "usage", None) or provider_usage
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
            "usage": _usage_metadata(prompt, answer, provider_usage)[0],
            "context_usage": _usage_metadata(prompt, answer, provider_usage)[1],
        }
        return

    answer = "".join(buffer)
    answer, validation = validate_answer(answer, question, route)
    usage, context_usage = _usage_metadata(prompt, answer, provider_usage)
    yield {"type": "validation", "code_validation": validation}
    yield {
        "type": "done",
        "answer": answer,
        "model": model_used,
        "provider": provider_used,
        "duration_ms": int((time.time() - started) * 1000),
        "truncated": False,
        "code_validation": validation,
        "usage": usage,
        "context_usage": context_usage,
    }
