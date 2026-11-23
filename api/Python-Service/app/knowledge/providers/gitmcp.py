"""The ``gitmcp`` provider: semantic documentation search on a repository's default branch.

Spec clauses 5-7; decision @.archcore/knowledge-layer/gitmcp-is-the-remote-documentation-provider.adr.md.
Called last, only for topics the version-exact providers could not answer. Every excerpt is marked
``versioned: false`` and names the branch it came from, because GitMCP cannot read a tag or commit.

Format seen live (2026-11-23): a match lists source blocks headed
``#### (owner/repo/path)[raw URL] (Score: 0.74)``; an unindexed or unmatched repository answers
"No relevant documentation found ..." followed by its README, which counts as no match.
"""

from __future__ import annotations

import os
import re
from typing import Any

from knowledge.model import Excerpt, cap

PROVIDER = "gitmcp"
DEFAULT_URL = "https://gitmcp.io/docs"
MIN_SCORE = 0.3
MAX_RESULTS = 5
NO_MATCH = "No relevant documentation found"
_SOURCE = re.compile(r"^#### \((?P<path>[^)\n]+)\)\[(?P<url>https://[^\]\s]+)\] \(Score: (?P<score>[0-9.]+)\)\s*$", re.M)
_LOCALE = re.compile(r"/docs/[a-z]{2}(?:[-_][a-z]{2,4})?/", re.I)
_BRANCH = re.compile(r"^https://raw\.githubusercontent\.com/[^/]+/[^/]+/(?P<branch>[^/]+)/")


def server_url() -> str:
    """``KNOWLEDGE_GITMCP_URL`` for a self-hosted instance; its host must be allowlisted too."""
    return os.environ.get("KNOWLEDGE_GITMCP_URL") or DEFAULT_URL


def parse_results(text: str) -> list[dict[str, Any]]:
    """Scored source blocks of a search answer; empty for GitMCP's no-match fallback."""
    if NO_MATCH in text[:400]:
        return []
    marks = list(_SOURCE.finditer(text))
    blocks = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        body = text[mark.end():end].strip()
        branch = _BRANCH.match(mark.group("url"))
        blocks.append({"path": mark.group("path"), "url": mark.group("url"), "score": float(mark.group("score")),
                       "branch": branch.group("branch") if branch else "default branch", "text": body})
    return blocks


def search(client: Any, record: Any, topic: str) -> tuple[list[Excerpt], str | None]:
    """Excerpts for ``topic`` and a reason when there are none. Sends owner, repository and topic words only."""
    owner, repo = record.repository.split("/", 1)
    text = client.call("search_generic_documentation", {"owner": owner, "repo": repo, "query": topic})
    blocks = [b for b in parse_results(text) if b["score"] >= MIN_SCORE]
    blocks.sort(key=lambda b: -b["score"])
    seen, unique = set(), []
    for block in blocks:  # found live: the same FastAPI page came back in three languages
        key = _LOCALE.sub("/docs/*/", block["path"])
        if key not in seen:
            seen.add(key)
            unique.append(block)
    blocks = unique
    if not blocks:
        reason = (f"GitMCP has no indexed documentation matching \"{topic}\" for {record.repository}."
                  if NO_MATCH in text[:400] else f"GitMCP found nothing above score {MIN_SCORE} for \"{topic}\".")
        return [], reason
    excerpts = []
    for block in blocks[:MAX_RESULTS]:
        body, truncated = cap(block["text"])
        excerpts.append(Excerpt(provider=PROVIDER, library=record.name, version_or_ref=f"{block['branch']} (unversioned)",
                                path_or_url=block["url"], text=body, truncated=truncated, versioned=False,
                                section=f"{block['path']} (score {block['score']:.2f})"))
    return excerpts, None
