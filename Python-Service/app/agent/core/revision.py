"""Read-only view of one extracted revision for framework capability providers.

Providers get the revision's files, bounded text reads, and the language facts
(@api/Python-Service/app/agent/core/facts.py). They never execute project code
and never read secret-bearing files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agent.core.types import Project

MAX_TEXT_BYTES = 2 * 1024 * 1024
_SKIP_DIRS = {"vendor", "node_modules", ".git", "var", "storage", "__MACOSX"}
_SECRET_NAMES = {"auth.json", ".git-credentials", "id_rsa", "id_ecdsa", "id_ed25519"}


def is_secret_path(relative: str) -> bool:
    name = relative.rsplit("/", 1)[-1]
    if name.startswith(".env") and not name.endswith((".example", ".dist", ".sample", ".template")):
        return True
    return name in _SECRET_NAMES or name.endswith((".pem", ".key", ".p12", ".pfx"))


def application_root(root: str | Path) -> Path:
    """The shallowest directory holding a dependency manifest (archives often wrap the project in one folder)."""
    base = Path(root)
    best: tuple[int, Path] | None = None
    for directory, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        depth = len(Path(directory).relative_to(base).parts)
        if depth > 3:
            dirnames[:] = []
            continue
        if {"composer.json", "package.json", "pyproject.toml", "go.mod"} & set(filenames):
            if best is None or depth < best[0]:
                best = (depth, Path(directory))
    return best[1] if best else base


@dataclass
class RevisionContext:
    """One extracted revision: application root, file list, lazy language facts."""

    root: Path
    files: tuple[str, ...]
    archive_sha256: str | None = None
    _facts_loader: Callable[[str], Any] | None = None
    _facts: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_directory(cls, root: str | Path, archive_sha256: str | None = None,
                       facts_loader: Callable[[str], Any] | None = None) -> "RevisionContext":
        app_root = application_root(root)
        files: list[str] = []
        for directory, dirnames, filenames in os.walk(app_root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for name in sorted(filenames):
                files.append((Path(directory, name).relative_to(app_root)).as_posix())
        return cls(root=app_root, files=tuple(files), archive_sha256=archive_sha256, _facts_loader=facts_loader)

    @property
    def project(self) -> Project:
        return Project(root=self.root, files=tuple(self.root / f for f in self.files))

    def exists(self, relative: str) -> bool:
        return relative in set(self.files)

    def glob(self, *suffixes: str, under: str = "") -> list[str]:
        prefix = under.rstrip("/") + "/" if under else ""
        return [f for f in self.files if f.startswith(prefix) and f.lower().endswith(suffixes) and not is_secret_path(f)]

    def read_text(self, relative: str, max_bytes: int = MAX_TEXT_BYTES) -> str | None:
        """File text, or None when missing, too large, outside the root or secret-bearing."""
        if is_secret_path(relative):
            return None
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError:
            return None
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > max_bytes:
                return None
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def facts(self, language: str) -> Any:
        """Language facts (e.g. ``PhpFacts``), loaded once; None when unavailable."""
        if language not in self._facts:
            loader = self._facts_loader or _default_loader(self)
            try:
                self._facts[language] = loader(language)
            except Exception:  # noqa: BLE001 — a missing facts export is a coverage gap, not a crash
                self._facts[language] = None
        return self._facts[language]


def _default_loader(context: RevisionContext) -> Callable[[str], Any]:
    def load(language: str) -> Any:
        if language != "php":
            return None
        from agent.core.facts import facts_for_workspace

        return facts_for_workspace(context.root, context.archive_sha256)
    return load
