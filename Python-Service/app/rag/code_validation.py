"""Programming-language resolution — LSP post-validation of generated answers.

Single abstract entry ``validate_answer`` that decides whether (and how) to
run language-server validation over the code blocks an LLM answer contains.
Language dispatch happens inside the generic ``lsp`` registry (one adapter per
markdown fence tag), so nothing here knows a concrete language or framework.

Route skip-set, settings gating and failure tolerance mirror the historical
behavior exactly — validation is best-effort and never fails an answer.
"""

from __future__ import annotations

from typing import Any

from infra.exception_logger import get_exception_logger
from infra.settings import settings

# Routes whose output is free-form prose — validating code blocks adds latency
# without signal.
_PROSE_ROUTES = ("chat", "knowledge")


def validate_answer(
    answer: str, question: str, route: str
) -> tuple[str, list[dict[str, Any]]]:
    """Run post-generation LSP validation when the route warrants it.

    Returns ``(possibly rewritten answer, validation list)``. The answer is
    rewritten only when the LSP pipeline repairs code blocks; the validation
    note (if any) is appended by the caller from the returned list's flags —
    see ``build_note`` usage below for parity with the old inline code.
    """
    if route in _PROSE_ROUTES:
        return answer, []
    if not getattr(settings, "lsp_validation_enabled", True):
        return answer, []
    try:
        from lsp import build_note, validate_markdown

        new_answer, summary = validate_markdown(
            answer,
            question,
            max_retries=getattr(settings, "lsp_max_retries", 2),
        )
        if summary:
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
            return new_answer + "\n\n" + build_note(summary), validation
    except Exception as exc:  # noqa: BLE001 — validation is best-effort; never fail the answer over it
        get_exception_logger().warning("LSP validation skipped: %s", exc)
    return answer, []
