"""Application service for answer generation endpoints.

The FastAPI composition root supplies retrieval and optional grounding
callbacks; this service owns request normalization, enrichment ordering, match
serialization, and SSE framing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any


class InvalidAnswerRequest(ValueError):
    """Raised when an answer request has no usable query."""


class AnswerService:
    def __init__(
        self,
        *,
        answer_context: Callable[[dict, str, str | None], tuple[list, list, Any]],
        resolve_profile: Callable[[dict, str], dict | None],
        inspect_context: Callable[[list, dict], str | None],
        domain_context: Callable[[list, dict], str | None],
        resolve_preview_mode: Callable[[dict], str],
        report_stream_error: Callable[[Exception], None],
        rule_context: Callable[[dict, str], tuple[str | None, list[dict]]] | None = None,
    ) -> None:
        self._answer_context = answer_context
        self._resolve_profile = resolve_profile
        self._inspect_context = inspect_context
        self._domain_context = domain_context
        self._resolve_preview_mode = resolve_preview_mode
        self._report_stream_error = report_stream_error
        self._rule_context = rule_context

    def answer(self, body: dict, trace_id: str | None = None) -> dict:
        query = self._query(body)
        doc_chunks = self._doc_chunks(body)
        results, contexts, extra_context, rule_sources = self._ground(body, query, trace_id)
        preview_mode = self._resolve_preview_mode(body)
        preview_only = preview_mode == "gate" and body.get("preview") is True

        from rag.answer import generate_answer

        generated = generate_answer(
            query,
            contexts=contexts,
            entities=body.get("entities") or {},
            temperature=body.get("temperature"),
            top_p=body.get("top_p"),
            extra_context=extra_context,
            doc_chunks=doc_chunks,
            project_profile=self._resolve_profile(body, query),
            use_remote=body.get("use_remote", True) is True,
            route=body.get("route", "chat"),
            preview_only=preview_only,
            preview_mode=preview_mode,
        )
        response = self._response(results, generated, preview_mode, body.get("runtime_symbol_metrics"))
        response["business_rules"] = rule_sources
        return response

    def stream(self, body: dict, trace_id: str | None = None) -> Iterator[str]:
        query = self._query(body)
        doc_chunks = self._doc_chunks(body)
        results, contexts, extra_context, rule_sources = self._ground(body, query, trace_id)
        preview_mode = self._resolve_preview_mode(body)
        preview_only = preview_mode == "gate" and body.get("preview") is True
        profile = self._resolve_profile(body, query)

        from rag.answer import generate_answer_stream

        try:
            for event in generate_answer_stream(
                query,
                contexts=contexts,
                entities=body.get("entities") or {},
                temperature=body.get("temperature"),
                top_p=body.get("top_p"),
                extra_context=extra_context,
                doc_chunks=doc_chunks,
                project_profile=profile,
                use_remote=body.get("use_remote", True) is True,
                route=body.get("route", "chat"),
                preview_only=preview_only,
                preview_mode=preview_mode,
            ):
                if event.get("type") == "done":
                    event["matches"] = self._matches(results, body.get("runtime_symbol_metrics"))
                    event["business_rules"] = rule_sources
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:  # noqa: BLE001 — preserve SSE error contract
            self._report_stream_error(exc)
            yield f"data: {json.dumps({'type': 'error', 'detail': str(exc)})}\n\n"

    def _query(self, body: dict) -> str:
        query = body.get("query", "")
        if not query or not isinstance(query, str):
            raise InvalidAnswerRequest("Missing or invalid query")
        return query

    @staticmethod
    def _doc_chunks(body: dict) -> list[str] | None:
        values = body.get("doc_chunks")
        if not isinstance(values, list):
            return None
        return [value for value in values if isinstance(value, str) and value.strip()]

    def _ground(self, body: dict, query: str, trace_id: str | None) -> tuple[list, list, Any, list[dict]]:
        results, contexts, extra_context = self._answer_context(body, query, trace_id)
        rule_text, rule_sources = None, []
        if self._rule_context is not None:
            try:
                rule_text, rule_sources = self._rule_context(body, query)
            except Exception as exc:  # noqa: BLE001 — business rules are optional grounding
                self._report_stream_error(exc)
        for enrichment in (self._inspect_context(results, body), self._domain_context(results, body), rule_text):
            if enrichment:
                extra_context = (
                    f"{extra_context}\n\n{enrichment}"
                    if isinstance(extra_context, str) and extra_context.strip()
                    else enrichment
                )
        return results, contexts, extra_context, rule_sources

    @staticmethod
    def _response(results: list, generated: dict, preview_mode: str, runtime_metrics: Any = None) -> dict:
        matches = AnswerService._matches(results, runtime_metrics)
        response = {
            "success": True,
            "answer": None,
            "matches": matches,
            "llmUsed": None,
            "provider": generated.get("provider", "local"),
            "durationMs": generated["duration_ms"],
            "code_validation": generated.get("code_validation", []),
            "usage": generated.get("usage"),
            "context_usage": generated.get("context_usage"),
        }
        if not generated.get("preview_only"):
            response["answer"] = generated["answer"]
            response["llmUsed"] = (
                f"Remote GPU LLM ({generated['model']})"
                if generated.get("provider") == "remote"
                else f"Local LLM ({generated['model']})"
            )
        if preview_mode != "off":
            response["context_preview"] = generated.get("context_preview")
            response["preview_mode"] = preview_mode
            if generated.get("preview_only"):
                response["preview_only"] = True
        return response

    @staticmethod
    def _matches(results: list, runtime_metrics: Any = None) -> list[dict]:
        runtime_metrics = runtime_metrics if isinstance(runtime_metrics, dict) else {}
        matches = []
        for index, result in enumerate(results):
            payload = result.get("payload") or {}
            name = f"{payload.get('class') or ''}::{payload.get('name') or ''}".strip(":")
            symbol_id = payload.get("symbol_id")
            match = {
                "chunk": {
                    "id": payload.get("node_id") or f"c{index}",
                    "fileId": payload.get("file") or "",
                    "fileName": name.strip() or payload.get("file") or "symbol",
                    "text": payload.get("code") or "",
                    "index": index,
                },
                "score": result.get("score", 0.0),
            }
            if symbol_id:
                match["symbol_id"] = symbol_id
                metric = runtime_metrics.get(symbol_id)
                if isinstance(metric, dict):
                    match["runtime_usage"] = metric
            matches.append(match)
        return matches
