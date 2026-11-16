"""Language facts for framework plugins, and their per-revision store.

Decision: @.archcore/code-quality/framework-knowledge-lives-in-plugins-over-language-facts.adr.md.
Framework plugins read facts produced by each language's own parser instead of
parsing source themselves. For PHP that is ``php bin/project-inspect facts``
(nikic/php-parser, schema ``php-facts-1``; @api/Python-Service/php/src/Inspection/FactsExporter.php).

Facts are stored per revision, keyed by the source archive's SHA-256, under
``FACTS_DIR`` (default ``/app/data/facts``) as ``<sha256>.<language>.json.gz``.
``facts_for_workspace`` loads them, or exports and stores them on first use.

Argument and constant values arrive encoded (see the exporter): JSON scalars,
``{"array": [...]}``, ``{"class": FQN}``, ``{"const": {"class", "name"}}``,
``{"call": {"name", "args"}}``, ``{"closure": true}`` or ``{"expr": text}``.
The ``literal_*`` helpers below decode them.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

PHP_FACTS_SCHEMA = "php-facts-1"
_SHA = re.compile(r"[0-9a-f]{64}\Z")


# ── value decoding ────────────────────────────────────────────────────────────

def literal_str(value: Any, facts: "PhpFacts | None" = None) -> str | None:
    """A string value, resolving ``X::class`` and class constants when ``facts`` is given."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "class" in value and isinstance(value["class"], str):
            return value["class"]
        const = value.get("const")
        if facts is not None and isinstance(const, dict):
            resolved = facts.constant(const.get("class"), const.get("name"))
            return resolved if isinstance(resolved, str) else None
    return None


def literal_items(value: Any) -> list[tuple[Any, Any]]:
    """``(key, value)`` pairs of an encoded array; empty for anything else."""
    if isinstance(value, dict) and isinstance(value.get("array"), list):
        return [(item.get("key"), item.get("value")) for item in value["array"] if isinstance(item, dict)]
    return []


def literal_strings(value: Any, facts: "PhpFacts | None" = None) -> list[str]:
    """A string or a list of strings (``'GET'`` or ``['GET', 'POST']``)."""
    single = literal_str(value, facts)
    if single is not None:
        return [single]
    return [text for _key, item in literal_items(value) if (text := literal_str(item, facts)) is not None]


def argument(args: list[dict[str, Any]], position: int, name: str | None = None) -> Any:
    """An attribute or call argument by name, else by position among positional ones."""
    if name is not None:
        for arg in args:
            if arg.get("name") == name:
                return arg.get("value")
    positional = [arg for arg in args if arg.get("name") is None]
    return positional[position].get("value") if position < len(positional) else None


def short_name(fqn: str | None) -> str:
    return (fqn or "").rsplit("\\", 1)[-1]


# ── PHP facts ────────────────────────────────────────────────────────────────

class PhpFacts:
    """Read-only view over one revision's ``php-facts-1`` export."""

    language = "php"

    def __init__(self, payload: dict[str, Any]) -> None:
        if payload.get("schema") != PHP_FACTS_SCHEMA:
            raise ValueError(f"unsupported facts schema {payload.get('schema')!r}")
        self.payload = payload
        self._classes: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for file in self.files():
            for cls in file.get("classes") or []:
                self._classes.setdefault(cls["fqn"], (cls, file))

    def files(self) -> list[dict[str, Any]]:
        return [file for file in self.payload.get("files") or [] if isinstance(file, dict)]

    def file(self, path: str) -> dict[str, Any] | None:
        return next((file for file in self.files() if file.get("path") == path), None)

    def classes(self) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        """``(class, file)`` for every class-like declaration."""
        yield from self._classes.values()

    def class_(self, fqn: str | None) -> dict[str, Any] | None:
        found = self._classes.get((fqn or "").lstrip("\\"))
        return found[0] if found else None

    def file_of(self, fqn: str | None) -> dict[str, Any] | None:
        found = self._classes.get((fqn or "").lstrip("\\"))
        return found[1] if found else None

    def constant(self, class_fqn: str | None, name: str | None) -> Any:
        """A class constant's literal value, following ``extends`` and interfaces."""
        seen: set[str] = set()
        queue = [(class_fqn or "").lstrip("\\")]
        while queue:
            current = queue.pop(0)
            if not current or current in seen:
                continue
            seen.add(current)
            cls = self.class_(current)
            if cls is None:
                continue
            for const in cls.get("constants") or []:
                if const.get("name") == name:
                    return const.get("value")
            queue.extend(cls.get("extends") or [])
            queue.extend(cls.get("implements") or [])
        return None

    def ancestors(self, fqn: str) -> list[str]:
        """Parent classes, nearest first (only those defined in the project, plus the first outside one)."""
        out: list[str] = []
        current = self.class_(fqn)
        while current and current.get("extends"):
            parent = current["extends"][0]
            if parent in out:
                break
            out.append(parent)
            current = self.class_(parent)
        return out

    def calls(self, path: str | None = None) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        """``(call, file)`` for every recorded call, optionally in one file."""
        for file in self.files():
            if path is not None and file.get("path") != path:
                continue
            for call in file.get("calls") or []:
                yield call, file

    def strict_types_ratio(self, prefix: str = "") -> tuple[int, int]:
        files = [f for f in self.files() if str(f.get("path", "")).startswith(prefix) and "parse_error" not in f]
        return sum(1 for f in files if f.get("strict_types")), len(files)


# ── per-revision store ────────────────────────────────────────────────────────

def facts_dir() -> Path:
    return Path(os.environ.get("FACTS_DIR", "/app/data/facts"))


def _facts_path(archive_sha256: str, language: str) -> Path | None:
    if not isinstance(archive_sha256, str) or not _SHA.match(archive_sha256) or not language.isalnum():
        return None
    return facts_dir() / f"{archive_sha256}.{language}.json.gz"


def store_facts(archive_sha256: str | None, language: str, payload: dict[str, Any]) -> bool:
    path = _facts_path(archive_sha256 or "", language)
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, suffix=".tmp") as handle:
            handle.write(gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8")))
            temporary = handle.name
        os.replace(temporary, path)
        return True
    except OSError as exc:
        logger.warning("Language facts not stored for %s: %s", archive_sha256, exc)
        return False


def load_facts(archive_sha256: str | None, language: str) -> dict[str, Any] | None:
    path = _facts_path(archive_sha256 or "", language)
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(gzip.decompress(path.read_bytes()))
    except (OSError, ValueError):
        return None


def has_language_files(root: str | Path, suffixes: tuple[str, ...] = (".php",)) -> bool:
    skip = {"vendor", "node_modules", ".git"}
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        if any(name.lower().endswith(suffixes) for name in filenames):
            return True
    return False


def export_php_facts(root: str | Path) -> dict[str, Any] | None:
    """Run the PHP facts export on an extracted tree; None when the CLI is unavailable or fails."""
    try:
        from infra.php_cli import PhpCli, PhpCliError

        return PhpCli().inspect_facts(str(root))
    except Exception as exc:  # noqa: BLE001 — facts are an enhancement; callers report the gap
        logger.warning("PHP facts export failed for %s: %s", root, exc)
        return None


def facts_for_workspace(root: str | Path, archive_sha256: str | None = None) -> PhpFacts | None:
    """PHP facts for an extracted revision: stored ones when present, else exported (and stored)."""
    stored = load_facts(archive_sha256, "php") if archive_sha256 else None
    if stored is None:
        if not has_language_files(root):
            return None
        stored = export_php_facts(root)
        if stored is None:
            return None
        if archive_sha256:
            store_facts(archive_sha256, "php", stored)
    try:
        return PhpFacts(stored)
    except ValueError:
        return None
