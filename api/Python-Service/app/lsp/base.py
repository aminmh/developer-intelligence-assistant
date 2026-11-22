"""LanguageAdapter contract + shared helpers for the LSP validation framework.

Every supported language implements the `LanguageAdapter` protocol in
`app/lsp/adapters/<lang>.py` (see `adapters/php.py` for a complete reference).
The shared functions here (fence extraction, repair prompt, validation note)
are language-agnostic and reused by all adapters.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Protocol, Tuple

from infra.exception_logger import get_exception_logger

_FENCE_RE = re.compile(
    r"(?ms)^(?P<fence>`{3,}|~{3,})(?P<lang>[a-zA-Z0-9_+-]*)[ \t]*\n"
    r"(?P<code>.*?)^(?P=fence)[ \t]*$"
)


class LanguageAdapter(Protocol):
    """Interface every language validator must implement."""

    name: str
    fence_tags: List[str]

    def wrap(self, code: str) -> Tuple[str, int]:
        """Wrap a code fragment so it parses as a complete file.

        Returns (wrapped_code, line_offset) where line_offset is the number of
        lines prepended before the original snippet (diagnostic line numbers
        shift by it).
        """
        ...

    def map_diagnostics(
        self, diagnostics: List[Dict[str, Any]], line_offset: int
    ) -> List[Dict[str, Any]]:
        """Map LSP publishDiagnostics to normalized error dicts.

        Each entry: {message, line, column, severity, source, code}. Severity
        values follow LSP (1=Error .. 4=Hint). Adapters may demote certain
        diagnostics to warning severity here.
        """
        ...

    def fallback(self, code: str) -> Dict[str, Any]:
        """Non-LSP fallback validator (syntax gate) used when the language
        server is unavailable. Must return {valid, errors, php_version?}."""
        ...

    def validate(
        self,
        code: str,
        filename: Optional[str],
        client: Any,
    ) -> Dict[str, Any]:
        """Validate a snippet via the shared LSP/fallback flow.

        `client` is the language's LspClient instance (or None when the LSP
        toolchain is unavailable). Returns
        {valid, errors, warnings, source, php_version?}.
        """
        ...

    def is_truncated(self, code: str) -> bool:
        """True when a block contains truncation markers (illustrative code
        that must not be auto-repaired)."""
        ...

    def extract_blocks(self, text: str) -> List[Dict[str, Any]]:
        """Extract fenced code blocks for this language from LLM markdown.

        Each block: {index, lang, code, start, end, full_text}.
        """
        ...


class IndexingAdapter(Protocol):
    """Optional adapter part for the index-time symbol driver (`lsp/indexer.py`).

    A language adapter that implements these members can feed the symbol graph.
    Settings MUST turn off every server feature that runs project code, loads the
    project's dependencies, or reaches the network (external-analyzer spec 32-33).
    """

    extensions: List[str]

    def index_settings(self) -> Dict[str, Any]:
        """`initializationOptions` for an indexing run (features off, no project code)."""
        ...

    def prepare_argv(self, server_argv: List[str], settings: Dict[str, Any]) -> Optional[List[str]]:
        """Optional command run once before the server starts (e.g. building the
        server's own index); None when the server needs no preparation."""
        ...

    def namespace_of(self, text: str) -> str:
        """Namespace prefix of a source file, or '' when the language has none."""
        ...

    def qualify(self, namespace: str, name: str) -> str:
        """Fully qualified name of a top-level symbol in that namespace."""
        ...

    def member_name(self, kind: str, name: str) -> str:
        """Graph name for a member, e.g. PHP properties keep their `$`."""
        ...

    def declaration_kind(self, kind: str, line: str, name: str) -> Optional[str]:
        """Optional: correct a type's graph kind from its declaration line (a PHP
        `trait` arrives as an LSP class); None keeps the LSP kind."""
        ...


# ─── Shared validation flow ───────────────────────────────────────────────────

def validate_with_lsp(
    adapter: LanguageAdapter,
    code: str,
    filename: Optional[str],
    client: Any,
) -> Dict[str, Any]:
    """Run the LSP validation flow: wrap → LSP diagnostics → fallback.

    Falls back to `adapter.fallback()` when the language server is unavailable
    or errors, so validation never hard-fails.
    """
    wrapped, line_offset = adapter.wrap(code)

    if client is not None:
        try:
            result = client.validate(wrapped, filename)
            mapped = adapter.map_diagnostics(result.get("diagnostics", []), line_offset)
            errors = [e for e in mapped if e.get("severity", 1) == 1]
            warnings = [e for e in mapped if e.get("severity", 1) != 1]
            return {
                "valid": not errors,
                "errors": errors,
                "warnings": warnings,
                "php_version": "",
                "source": "lsp",
            }
        except Exception as exc:
            get_exception_logger().warning(
                "LSP validation failed (%s); falling back", exc
            )

    fallback = adapter.fallback(wrapped)
    errors = []
    for e in fallback.get("errors", []):
        e = dict(e)
        e["line"] = max(1, int(e.get("line", 1)) - line_offset)
        e["severity"] = 1
        errors.append(e)
    return {
        "valid": not errors and bool(fallback.get("valid")),
        "errors": errors,
        "warnings": [],
        "php_version": fallback.get("php_version", ""),
        "source": "lint",
    }


# ─── Block extraction ─────────────────────────────────────────────────────────

def extract_fenced_blocks(text: str, fence_tags: List[str]) -> List[Dict[str, Any]]:
    """Extract fenced blocks whose language tag is in `fence_tags`."""
    tags = set(t.lower() for t in fence_tags)
    blocks: List[Dict[str, Any]] = []
    for match in _FENCE_RE.finditer(text):
        lang = (match.group("lang") or "").strip().lower()
        if lang not in tags:
            continue
        blocks.append(
            {
                "index": len(blocks),
                "lang": lang,
                "code": match.group("code"),
                "start": match.start(),
                "end": match.end(),
                "full_text": match.group(0),
            }
        )
    return blocks


# ─── Repair loop ──────────────────────────────────────────────────────────────

_REPAIR_SYSTEM = (
    "You are a senior software engineer. Fix the code below based on the "
    "syntax and static-analysis diagnostics. Preserve the original structure "
    "unless the diagnostics require a change. Output ONLY the corrected code — "
    "no markdown fences, no explanation, no line numbers."
)


def is_truncated(code: str) -> bool:
    """True when a block contains truncation markers (illustrative, not real code)."""
    return bool(
        re.search(r"(?m)(\.\.\.|//\s*\.\.\.|/\*[\s\S]*?\.\.\.[\s\S]*?\*/)", code)
        or re.search(r"(?m)^\s*(public|function|def|func)\b.*(…|\.\.\.)\s*$", code)
    )


def build_repair_prompt(question: str, code: str, diagnostics: str) -> str:
    return (
        f"A code block was generated for this question: {question}\n\n"
        "The block failed validation:\n\n"
        f"```\n{code}\n```\n\n"
        "Validation diagnostics:\n"
        f"{diagnostics}\n\n"
        "Fix all reported issues and output ONLY the corrected code."
    )


def format_diagnostics(errors: List[Dict[str, Any]]) -> str:
    if not errors:
        return "No diagnostics available."
    lines = []
    for e in errors:
        lines.append(
            f"- line {e.get('line', '?')}: [{e.get('source') or 'lsp'}] "
            f"{e.get('message', '')}"
        )
    return "\n".join(lines)


def _extract_code_from_response(response: str) -> str:
    """Pull the code out of the LLM's repair response (may be fenced or bare)."""
    response = (response or "").strip()
    if not response:
        return ""
    match = _FENCE_RE.search(response)
    if match:
        return match.group("code").strip()
    lines = [
        ln
        for ln in response.splitlines()
        if not re.match(r"^\s*(```+|~~~+)[a-zA-Z0-9_+-]*\s*$", ln)
    ]
    return "\n".join(lines).strip()


def validate_and_repair_blocks(
    adapter: LanguageAdapter,
    text: str,
    question: str,
    client: Any,
    max_retries: int = 2,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Validate every block of `adapter.name` in `text`, repairing invalid ones
    via the LLM. Returns (new_text, summary) where summary is a per-block list
    of {language, index, status: ok|repaired|warning|failed, errors, warnings}.
    """
    from runtime.llm import complete

    blocks = adapter.extract_blocks(text)
    if not blocks:
        return text, []

    summary: List[Dict[str, Any]] = []
    new_text = text

    for block in reversed(blocks):
        result = validate_with_lsp(adapter, block["code"], f"Block_{block['index']}.{adapter.name}", client)
        status = "ok"
        errors = result.get("errors", [])

        if not result["valid"] and not adapter.is_truncated(block["code"]):
            repaired = False
            for _ in range(max(int(max_retries or 0), 1)):
                try:
                    diagnostics = format_diagnostics(errors or result.get("warnings", []))
                    response = complete(
                        build_repair_prompt(question, block["code"], diagnostics),
                        system=_REPAIR_SYSTEM,
                    )
                except Exception as exc:
                    get_exception_logger().warning("Repair LLM call failed: %s", exc)
                    break
                candidate = _extract_code_from_response(response)
                if not candidate or candidate == block["code"]:
                    break
                attempt = validate_with_lsp(adapter, candidate, f"Block_{block['index']}.{adapter.name}", client)
                if attempt["valid"]:
                    block["code"] = candidate
                    errors = []
                    repaired = True
                    break
                errors = attempt.get("errors", [])
            status = "repaired" if repaired else "failed"

        elif not result["valid"] and adapter.is_truncated(block["code"]):
            status = "warning"

        if status == "repaired":
            opener = block["full_text"].splitlines()[0] if block["full_text"].splitlines() else ""
            fence_match = re.match(r"^(?P<fence>`{3,}|~{3,})(?P<lang>[a-zA-Z0-9_+-]*)\s*$", opener)
            fence = fence_match.group("fence") if fence_match else "```"
            lang = (fence_match.group("lang") if fence_match else "").strip() or adapter.name
            new_block = f"{fence}{lang}\n{block['code']}\n{fence}"
            new_text = new_text[: block["start"]] + new_block + new_text[block["end"] :]

        summary.append(
            {
                "language": adapter.name,
                "index": block["index"],
                "status": status,
                "errors": errors,
                "warnings": result.get("warnings", []),
            }
        )

    summary.reverse()
    return new_text, summary


# ─── Validation note ──────────────────────────────────────────────────────────

def build_validation_note(summary: List[Dict[str, Any]]) -> str:
    """Build the markdown annotation for validated code blocks (multi-language).

    summary entries: {language, index, status, errors, warnings}.
    """
    if not summary:
        return ""

    by_lang: Dict[str, List[Dict[str, Any]]] = {}
    for s in summary:
        by_lang.setdefault(s["language"], []).append(s)

    notes: List[str] = []
    for language, entries in by_lang.items():
        total = len(entries)
        ok = sum(1 for s in entries if s["status"] == "ok")
        repaired = sum(1 for s in entries if s["status"] == "repaired")
        warnings = sum(1 for s in entries if s["status"] == "warning")
        failed = [s for s in entries if s["status"] == "failed"]

        parts = [f"{total} block(s)"]
        if repaired:
            parts.append(f"{repaired} auto-repaired")
        if warnings:
            parts.append(f"{warnings} truncated/illustrative")
        label = language.upper() if language.lower() == "php" else language.capitalize()

        if not failed:
            notes.append(f"> ✅ {label} validation passed — {', '.join(parts)}.")
            continue

        lines = [f"> ⚠️ {label} validation — {len(failed)} block(s) still invalid:"]
        for s in failed[:5]:
            err = (s.get("errors") or [{}])[0]
            line = err.get("line", "?")
            msg = (err.get("message") or "").strip()
            lines.append(f">   block {s['index'] + 1}, line {line}: {msg}")
        if len(failed) > 5:
            lines.append(f">   ... and {len(failed) - 5} more.")
        notes.append("\n".join(lines))

    return "\n\n".join(notes)


def flatten_summary(summary: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize a summary for API responses (strip non-serializable bits)."""
    out = []
    for s in summary:
        out.append(
            {
                "language": s.get("language"),
                "index": s.get("index"),
                "status": s.get("status"),
                "errors": s.get("errors", []),
                "warnings": s.get("warnings", []),
            }
        )
    return out
