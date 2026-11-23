"""Result shapes and version arithmetic for the knowledge layer (spec Surface)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

MAX_EXCERPT_CHARS = 4_000
MAX_REQUEST_CHARS = 20_000

COMPLETE = "complete"
PARTIAL = "partial"
UNAVAILABLE = "unavailable"


@dataclass
class Excerpt:
    provider: str                      # "vendor" | "github_ref" | "gitmcp"
    library: str
    version_or_ref: str
    path_or_url: str
    text: str
    line: int | None = None
    versioned: bool = True
    truncated: bool = False
    section: str = ""
    retrieved_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def cap(text: str, limit: int = MAX_EXCERPT_CHARS) -> tuple[str, bool]:
    """Failure clause 4: truncate to ``limit`` characters and say so."""
    return (text, False) if len(text) <= limit else (text[:limit].rstrip() + "\n…", True)


_VERSION = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def parse_version(text: str) -> tuple[int, int, int] | None:
    """``v7.4.18`` → (7, 4, 18); ``8.0`` → (8, 0, 0); anything without a leading number → None."""
    match = _VERSION.match(text.strip().lstrip("vV[ "))
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def in_range(version: tuple[int, int, int], installed: tuple[int, int, int], target: tuple[int, int, int],
             precision: int = 3) -> bool:
    """Whether ``version`` is after ``installed`` and at most ``target``, compared to ``precision`` parts.

    An ``UPGRADE-8.0.md`` heading names a minor line, so it compares on two parts: 8.0 covers 8.0.x.
    """
    v, i, t = version[:precision], installed[:precision], target[:precision]
    return i < v <= t
