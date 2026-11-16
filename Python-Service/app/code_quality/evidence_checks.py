"""Shared helpers for the evidence-category checks (gap-closing plan, Phase 3).

The checks for process, testing, documentation, localization and accessibility read only
bounded, read-only evidence: the revision's evidence files (``evidence_files.classify``) and
records that plugins provide. They never run project commands, builds or tests, and name no
framework. Toolchain knowledge here is per language ecosystem (PHP, Node.js, Python), as the
evidence inventory already is.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

MAX_FINDINGS_PER_CHECK = 40


def files_of_kind(context: Any, *kinds: str) -> list[str]:
    from code_quality.evidence_files import classify

    return [path for path in context.files if classify(path) in kinds]


def line_of(text: str, pattern: str | re.Pattern[str], start: int = 1) -> int:
    """1-based line of the first match (a substring or a compiled pattern) at or after ``start``."""
    for index, row in enumerate(text.splitlines()[start - 1:], start):
        if (pattern.search(row) if isinstance(pattern, re.Pattern) else pattern in row):
            return index
    return 1


def finding(file: str, line: int, rule: str, category: str, severity: str, message: str) -> dict[str, Any]:
    return {"file": file, "line": max(1, int(line)), "rule": rule, "category": category,
            "severity": severity, "message": message[:600]}


def capped(findings: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Errors first, then warnings, then info, at most MAX_FINDINGS_PER_CHECK; the summary counts the rest."""
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda row: (order.get(row["severity"], 3), row["file"], row["line"]))
    if len(findings) > MAX_FINDINGS_PER_CHECK:
        summary["findings_truncated"] = len(findings) - MAX_FINDINGS_PER_CHECK
    return findings[:MAX_FINDINGS_PER_CHECK], summary


def plural(count: int, word: str) -> str:
    if count == 1:
        return f"{count} {word}"
    return f"{count} {word}{'es' if word.endswith(('s', 'sh', 'ch', 'x')) else 's'}"


# ── toolchain versions ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Declared:
    """One place a toolchain version is declared: a pin (``8.2``) or a constraint (``>=8.2``)."""

    toolchain: str
    version: str           # major.minor (or major) as written
    pinned: bool           # an exact runtime choice (version file, image tag) vs a range constraint
    file: str
    line: int
    source: str            # what declares it, e.g. "composer.json require.php"


_IMAGE = {"php": re.compile(r"(?:^|[/:\s])(?:[\w.-]*-)?php[\w.-]*:(\d+(?:\.\d+)?)"),
          "node": re.compile(r"(?:^|[/:\s])node:(\d+(?:\.\d+)?)"),
          "python": re.compile(r"(?:^|[/:\s])python:(\d+(?:\.\d+)?)")}
_VERSION_FILES = {".php-version": "php", ".nvmrc": "node", ".node-version": "node", ".python-version": "python"}


def _major_minor(text: str) -> str | None:
    match = re.search(r"(\d+)(?:\.(\d+))?", text or "")
    if not match:
        return None
    return match.group(1) + (f".{match.group(2)}" if match.group(2) else "")


def declared_versions(context: Any) -> list[Declared]:
    rows: list[Declared] = []
    composer = context.read_text("composer.json", 512_000)
    try:
        data = json.loads(composer) if composer else {}
    except ValueError:
        data = {}
    if isinstance(data, dict):
        constraint = (data.get("require") or {}).get("php") if isinstance(data.get("require"), dict) else None
        if isinstance(constraint, str) and (version := _major_minor(constraint)):
            rows.append(Declared("php", version, False, "composer.json", line_of(composer, '"php"'), "composer.json require.php"))
        platform = ((data.get("config") or {}).get("platform") or {}).get("php") if isinstance(data.get("config"), dict) else None
        if isinstance(platform, str) and (version := _major_minor(platform)):
            rows.append(Declared("php", version, True, "composer.json", line_of(composer, '"platform"'), "composer.json config.platform.php"))
    package = context.read_text("package.json", 512_000)
    try:
        engines = (json.loads(package) or {}).get("engines") if package else None
    except ValueError:
        engines = None
    if isinstance(engines, dict) and isinstance(engines.get("node"), str) and (version := _major_minor(engines["node"])):
        rows.append(Declared("node", version, False, "package.json", line_of(package, '"node"'), "package.json engines.node"))
    for name, toolchain in _VERSION_FILES.items():
        text = context.read_text(name, 1_000)
        if text and (version := _major_minor(text.strip().lstrip("v"))):
            rows.append(Declared(toolchain, version, True, name, 1, name))
    from code_quality.evidence_files import classify

    for path in context.files:
        kind = classify(path)
        if kind not in ("container", "ci"):
            continue
        text = context.read_text(path, 512_000) or ""
        for number, row in enumerate(text.splitlines(), 1):
            stripped = row.strip()
            if stripped.startswith("#"):
                continue
            if kind == "container" and not re.match(r"(?i)^FROM\s", stripped):
                continue
            if kind == "ci" and not re.match(r"^-?\s*(image|name)\s*:", stripped) and "image:" not in stripped:
                continue
            for toolchain, pattern in _IMAGE.items():
                if (match := pattern.search(stripped.split("#", 1)[0])):
                    rows.append(Declared(toolchain, match.group(1), True, path, number,
                                         f"{'base image' if kind == 'container' else 'CI image'} in {path}"))
    return rows


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def conflicts(rows: Iterable[Declared]) -> list[tuple[Declared, Declared, str]]:
    """Pairs that disagree: two pins with different versions, or a pin below a constraint's lower bound."""
    found = []
    rows = list(rows)
    for toolchain in sorted({row.toolchain for row in rows}):
        pins = [row for row in rows if row.toolchain == toolchain and row.pinned]
        floors = [row for row in rows if row.toolchain == toolchain and not row.pinned]
        reference = next((row for row in pins if row.source.startswith((".", "composer.json config"))), pins[0] if pins else None)
        for pin in pins:
            if reference is not None and pin is not reference and "." in pin.version and "." in reference.version \
                    and pin.version != reference.version:
                found.append((reference, pin, f"{toolchain} {reference.version} in {reference.source} but {pin.version} in {pin.source}"))
        for floor in floors:
            for pin in pins:
                width = min(len(_key(pin.version)), len(_key(floor.version)))
                if _key(pin.version)[:width] < _key(floor.version)[:width]:
                    found.append((floor, pin, f"{toolchain} {pin.version} in {pin.source} is below {floor.source} (at least {floor.version})"))
    return found
