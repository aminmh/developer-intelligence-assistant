"""The ``doc_source`` provider: a framework's guides in a separate repository, at the matching version line.

A framework plugin names the repository through ``DOC_SOURCES`` (``laravel/docs`` on branch ``13.x``).
Spec clauses 3-4 and 8: the installed line's guides answer topics; each major line's upgrade guide between
the installed and target versions answers upgrade questions. Only the repository, the branch and the file
names leave the machine; topics pick files locally from the plugin's list.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from knowledge.model import parse_version
from knowledge.providers.vendor import Citation, Section, _excerpt, sections, topic_from_documents

PROVIDER = "doc_source"
MAX_TOPIC_FILES = 3
MAX_UPGRADE_LINES = 4
_MARKUP_ONLY = re.compile(r"^\s*(<a name=\"[^\"]*\"></a>|</?div[^>]*>)?\s*$")


def ref_for(source: Any, version: tuple[int, int, int]) -> str:
    return source.ref.replace("{major}", str(version[0])).replace("{minor}", str(version[1]))


def topic_files(source: Any, topic: str) -> list[str]:
    """Files whose topic prefix starts a word of ``topic``, in the plugin's order, without repeats."""
    words = re.findall(r"[a-z]+", topic.lower())
    files = [path for prefix, path in source.topics if any(word.startswith(prefix) for word in words)]
    return list(dict.fromkeys(files))[:MAX_TOPIC_FILES]


_LINK_ITEM = re.compile(r"^\s*[-*]\s+\[[^\]]*\]\(#[^)]*\)\s*$")
_IMPACT = re.compile(r"Likelihood Of Impact:\s*\**\s*(High|Medium|Low)", re.I)
_IMPACT_ORDER = {"high": 0, "medium": 1, "low": 3}


def _content(section: Section) -> bool:
    """Drop heading-only sections, the anchor and ``<div>`` markup the Laravel guides wrap lists in, and
    index sections that only link to other sections of the same page."""
    lines = [line for line in section.text.splitlines() if not _MARKUP_ONLY.match(line)]
    return bool(lines) and not all(_LINK_ITEM.match(line) for line in lines)


def _impact(section: Section) -> int:
    """Rank by the guide's own "Likelihood Of Impact": high first, low last, unrated in between.

    Found live (2026-11-23): in document order, a report's notes budget ran out on Laravel 13's index and
    introduction before reaching any change.
    """
    match = _IMPACT.search(section.text)
    return _IMPACT_ORDER[match.group(1).lower()] if match else 2


def _fetch(fetcher: Any, source: Any, ref: str, path: str) -> str | None:
    body = fetcher.get(f"https://raw.githubusercontent.com/{source.repository}/{quote(ref, safe='')}/{quote(path)}",
                       accept="text/plain")
    return None if body is None else body.decode("utf-8", errors="replace")


def _citation(source: Any, ref: str, line: str) -> Citation:
    return Citation(provider=PROVIDER, version_or_ref=f"{line} ({source.repository}@{ref})",
                    prefix=f"https://github.com/{source.repository}/blob/{ref}")


def topic(fetcher: Any, source: Any, record: Any, text: str) -> dict[str, Any]:
    """Sections of the installed line's guides that match ``text``."""
    version = parse_version(record.version)
    files = topic_files(source, text)
    if version is None or not files:
        return {"excerpts": [], "versions_found": [], "reasons": []}
    ref = ref_for(source, version)
    documents = {path: body for path in files if (body := _fetch(fetcher, source, ref, path)) is not None}
    if not documents:
        return {"excerpts": [], "versions_found": [], "reasons": [
            f"{source.repository} has no {', '.join(files)} on {ref} for the installed {record.version}."]}
    excerpts = [e for e in topic_from_documents(documents, record, text, _citation(source, ref, ref), limit=8)
                if e.text.strip() != (e.section or "").strip()][:5]  # a heading alone tells the model nothing
    return {"excerpts": excerpts, "versions_found": [], "reasons": [] if excerpts else [
        f"{source.repository} at {ref} has no section matching \"{text}\"."]}


def upgrade(fetcher: Any, source: Any, record: Any, target: str) -> dict[str, Any]:
    """The upgrade guide of every major line after the installed one, up to the target's line."""
    installed, wanted = parse_version(record.version), parse_version(target)
    if not source.upgrade_path or installed is None or wanted is None or wanted[0] <= installed[0]:
        return {"excerpts": [], "versions_found": [], "reasons": []}
    lines = list(range(installed[0] + 1, wanted[0] + 1))
    excerpts, versions, reasons = [], [], []
    for major in lines[:MAX_UPGRADE_LINES]:
        ref = ref_for(source, (major, 0, 0))
        text = _fetch(fetcher, source, ref, source.upgrade_path)
        if text is None:
            reasons.append(f"{source.repository} has no {source.upgrade_path} on {ref}.")
            continue
        citation = _citation(source, ref, f"{major}.0")
        kept = sorted((s for s in sections(source.upgrade_path, text) if _content(s)), key=lambda s: (_impact(s), s.line))
        found = [_excerpt(record, s, citation) for s in kept]
        if found:
            excerpts += found
            versions.append(f"{major}.0.0")
    if len(lines) > MAX_UPGRADE_LINES:
        reasons.append(f"Only the first {MAX_UPGRADE_LINES} major lines after {record.version} were read.")
    return {"excerpts": excerpts, "versions_found": versions, "reasons": reasons}
