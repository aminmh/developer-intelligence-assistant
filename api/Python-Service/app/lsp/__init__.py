"""Modular LSP validation framework.

The registry loads `config/lsps.yml`, and every supported language has a
custom adapter in `lsp/adapters/`. Add a new language LSP via the
`add-language-lsp` skill (`.opencode/skills/add-language-lsp/SKILL.md`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from lsp.base import build_validation_note
from lsp.registry import LspRegistry

registry = LspRegistry(str(Path(__file__).resolve().parent.parent / "config" / "lsps.yml"))


def validate(language: str, code: str, filename: Optional[str] = None) -> Dict[str, Any]:
    """Validate a snippet for one language."""
    return registry.validate(language, code, filename)


def validate_markdown(
    text: str,
    question: str = "",
    max_retries: Optional[int] = None,
) -> tuple[str, List[Dict[str, Any]]]:
    """Validate + repair code blocks of every enabled language in `text`."""
    return registry.validate_markdown(text, question, max_retries=max_retries)


def build_note(summary: List[Dict[str, Any]]) -> str:
    """Markdown annotation for validated blocks (multi-language)."""
    return build_validation_note(summary)


def warmup() -> None:
    """Spawn enabled language servers with trivial snippets (non-blocking)."""
    registry.warmup()


def shutdown() -> None:
    """Shut down all language-server processes."""
    registry.shutdown()
