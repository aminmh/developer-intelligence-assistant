"""Framework-neutral source linking for evaluator reports."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from .models import SourceTrace


_NAMESPACE = re.compile(r"^\s*(?:namespace|module)\s+([\\A-Za-z_][\\\w.]*)")
_CLASS = re.compile(r"^\s*(?:abstract\s+|final\s+)?class\s+([A-Za-z_][\w]*)")
_METHOD = re.compile(
    r"^\s*(?:(?:public|private|protected|static|async|function)\s+)*"
    r"(?:function\s+)?([A-Za-z_][\w]*)\s*\("  # PHP/JS/Ruby style
)
_PY_METHOD = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][\w]*)\s*\(")
_JS_FUNCTION = re.compile(r"^\s*(?:async\s+)?function\s+([A-Za-z_][\w]*)\s*\(")


class SourceLinker:
    """Resolve a bounded source location without knowing a framework.

    ``source_hint`` can be a mapping supplied by a runtime tagger, for example
    ``{"file": "src/orders.py", "line": 18}``, or a ``SourceTrace``.  The
    linker never follows a path outside ``source_root``.
    """

    def __init__(self, source_root: str | Path, *, context_lines: int = 3, max_file_bytes: int = 2_000_000):
        self.source_root = Path(source_root).resolve()
        self.context_lines = max(0, int(context_lines))
        self.max_file_bytes = max(1024, int(max_file_bytes))

    def link(
        self,
        query: str | None = None,
        source_hint: Mapping[str, Any] | SourceTrace | None = None,
        *,
        file: str | None = None,
        line: int | None = None,
    ) -> SourceTrace:
        hint: dict[str, Any] = {}
        if isinstance(source_hint, SourceTrace):
            hint = source_hint.to_dict()
        elif source_hint:
            hint = dict(source_hint)
        file = file or hint.get("file") or hint.get("source_file") or hint.get("path")
        line = line or hint.get("line") or hint.get("source_line")
        if not file:
            return SourceTrace(available=False, error="source file was not provided")
        path = self._safe_path(str(file))
        if path is None:
            return SourceTrace(file=str(file), available=False, error="source path is outside project root")
        if not path.is_file():
            return SourceTrace(file=str(file), available=False, error="source file was not found")
        try:
            if path.stat().st_size > self.max_file_bytes:
                return SourceTrace(file=str(file), available=False, error="source file exceeds bounded read size")
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return SourceTrace(file=str(file), available=False, error=f"source file could not be read: {exc}")
        target = self._line_number(line, hint, lines)
        namespace, class_name, method_name = self._enclosing_symbols(lines, target)
        start = max(1, target - self.context_lines)
        end = min(len(lines), target + self.context_lines)
        language = self._language(path)
        return SourceTrace(
            file=str(path.relative_to(self.source_root)),
            line=target,
            class_name=hint.get("class_name") or hint.get("class") or class_name,
            method=hint.get("method") or hint.get("method_name") or method_name,
            namespace=hint.get("namespace") or namespace,
            context=lines[start - 1 : end],
            language=hint.get("language") or language,
            confidence=1.0 if line else 0.6,
            available=True,
        )

    resolve = link

    def _safe_path(self, filename: str) -> Path | None:
        candidate = Path(filename)
        if not candidate.is_absolute():
            candidate = self.source_root / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(self.source_root)
        except (OSError, ValueError):
            return None
        return resolved

    @staticmethod
    def _line_number(line: Any, hint: Mapping[str, Any], lines: list[str]) -> int:
        try:
            value = int(line)
            return min(max(value, 1), max(1, len(lines)))
        except (TypeError, ValueError):
            for key in ("line_number", "start_line"):
                try:
                    value = int(hint[key])
                    return min(max(value, 1), max(1, len(lines)))
                except (KeyError, TypeError, ValueError):
                    continue
        return 1

    @staticmethod
    def _enclosing_symbols(lines: list[str], target: int) -> tuple[str | None, str | None, str | None]:
        namespace = class_name = method_name = None
        class_indent: int | None = None
        method_indent: int | None = None
        for raw in lines[:target]:
            stripped = raw.strip()
            if not stripped or stripped.startswith(("#", "//", "/*", "*")):
                continue
            match = _NAMESPACE.match(raw)
            if match:
                namespace = match.group(1).rstrip(";")
            match = _CLASS.match(raw)
            if match:
                class_name = match.group(1)
                class_indent = len(raw) - len(raw.lstrip())
                method_name = None
                method_indent = None
                continue
            match = _PY_METHOD.match(raw) or _JS_FUNCTION.match(raw) or _METHOD.match(raw)
            if match:
                indent = len(raw) - len(raw.lstrip())
                if class_indent is None or indent > class_indent:
                    method_name = match.group(1)
                    method_indent = indent
            elif method_indent is not None and len(raw) - len(raw.lstrip()) <= (class_indent or 0):
                method_name = None
                method_indent = None
        return namespace, class_name, method_name

    @staticmethod
    def _language(path: Path) -> str | None:
        return {
            ".py": "python",
            ".php": "php",
            ".js": "javascript",
            ".ts": "typescript",
            ".rb": "ruby",
            ".java": "java",
            ".go": "go",
        }.get(path.suffix.lower())

