"""Deterministic identifier normalizer for the domain-concept layer.

Turns code identifiers, namespace paths, route declarations and file names into
one canonical space-separated, casefolded token string so concept lookups by
name are stable regardless of casing convention or separator style:

    PaymentService            -> "payment service"
    payment_service           -> "payment service"
    App\\Services\\PaymentService -> "app services payment service"
    POST /api/orders          -> "post api orders"
    DTOMapper                 -> "dto mapper"

Rules (applied in order, all pure stdlib ``re``):
  1. strip trailing file extensions,
  2. split namespace/path/word separators (backslash, slash, dot, dash,
     underscore) into spaces,
  3. split PascalCase/camelCase boundaries while leaving acronym runs
     (``API``, ``DTO``) intact,
  4. strip punctuation,
  5. collapse whitespace and casefold.

The normalizer never raises and never mutates state; the same input always
yields the same output. The original identifier is preserved untouched on the
concept itself — normalization is only the lookup key.
"""

from __future__ import annotations

import re

# Known file suffixes, longest first so nested suffixes ("home.blade.php") can
# be peeled one layer at a time.
_FILE_EXTENSIONS: tuple[str, ...] = (
    ".blade.php",
    ".d.ts",
    ".php",
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".jsx",
    ".ts",
    ".tsx",
    ".vue",
    ".svelte",
    ".java",
    ".kt",
    ".kts",
    ".go",
    ".rb",
    ".rs",
    ".cs",
    ".cpp",
    ".hpp",
    ".cc",
    ".c",
    ".h",
    ".json",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".html",
    ".htm",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".md",
    ".txt",
    ".sql",
    ".sh",
    ".bash",
    ".zsh",
    ".env",
    ".lock",
    ".log",
)

_EXT_PATTERN = re.compile(
    r"(?i)\.(?:"
    + "|".join(re.escape(ext.lstrip(".")) for ext in _FILE_EXTENSIONS)
    + r")$"
)

# Namespace, path and word separators -> space. Backslash comes first so PHP
# namespace separators and Windows paths behave like forward-slash paths.
_SEPARATOR = re.compile(r"[\\/._-]+")

# CamelCase boundaries, both flavours:
#   paymentService -> payment Service   (lower/digit then Upper)
#   DTOMapper      -> DTO Mapper        (Upper run then Upper+lower word)
# Acronym runs ("API", "DTO") are NOT exploded — they stay one token.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# Anything that is neither a word character nor whitespace (punctuation,
# braces, colons, operators...) becomes a space. Unicode letters survive.
_NOT_WORD = re.compile(r"[^\w\s]")

_WHITESPACE = re.compile(r"\s+")

_MAX_EXT_PASSES = 4


class ConceptNormalizer:
    """Stateless identifier -> canonical space-separated token string."""

    def normalize(self, text: str) -> str:
        """Normalize ``text`` to a canonical lookup string (never raises)."""
        if not isinstance(text, str):
            return ""
        s = text.strip()
        if not s:
            return ""
        # Peel file extensions before dots become separators.
        for _ in range(_MAX_EXT_PASSES):
            stripped = _EXT_PATTERN.sub("", s)
            if stripped == s:
                break
            s = stripped
        s = _SEPARATOR.sub(" ", s)
        s = _CAMEL_BOUNDARY.sub(" ", s)
        s = _NOT_WORD.sub(" ", s)
        s = _WHITESPACE.sub(" ", s)
        return s.casefold().strip()

    def display_tokens(self, text: str) -> list[str]:
        """Internal helper: the normalized text as display-ready tokens."""
        normalized = self.normalize(text)
        return normalized.split() if normalized else []
