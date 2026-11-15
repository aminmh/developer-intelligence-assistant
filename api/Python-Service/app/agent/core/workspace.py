"""Workspace abstraction — a path-safe directory view handed to plugins.

Plugins must never touch paths outside the workspace root. ``Workspace``
enforces that: every access resolves and validates containment.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path


class Workspace:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def path(self, *parts: str) -> Path:
        """Resolve ``parts`` under the root, refusing escapes."""
        candidate = self.root.joinpath(*parts).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(
                f"path escapes workspace root: {candidate} (root {self.root})"
            )
        return candidate

    def exists(self, *parts: str) -> bool:
        return self.path(*parts).exists()

    def read(self, *parts: str) -> str:
        return self.path(*parts).read_text(encoding="utf-8", errors="replace")

    def iter_files(self, suffix: str | None = None) -> Iterator[Path]:
        for p in self.root.rglob("*"):
            if p.is_file() and (suffix is None or p.suffix == suffix):
                yield p

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Workspace({self.root})"
