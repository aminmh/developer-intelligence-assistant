"""The ``github_ref`` provider: a library's documentation files at an exact commit or tag on GitHub.

Spec clause 4 and failure clause 2. The installed version is read at the lockfile's source commit
first, then at tag forms; a target version tries the usual tag forms (``v8.0.0``, ``8.0.0``, ``v8.0``,
``8.0``). One contents-API call lists the repository root at the ref; matching UPGRADE, CHANGELOG and
README files come from raw.githubusercontent.com. Only repository names, refs and file paths leave the
machine; topics are matched locally.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from knowledge.model import parse_version
from knowledge.providers.vendor import _DOC_NAME, MAX_DOC_BYTES, Citation

PROVIDER = "github_ref"
MAX_FILES = 8


def installed_refs(record: Any) -> list[str]:
    """The lockfile's source commit, then the tag as written, then the other tag spelling."""
    forms = [record.reference, record.tag, f"v{record.version}", record.version]
    return list(dict.fromkeys(f for f in forms if isinstance(f, str) and f and not f.startswith("dev-")))


def target_refs(target: str) -> list[str]:
    version = parse_version(target)
    if version is None:
        return []
    parts = target.strip().lstrip("vV").split(".")
    if len(parts) >= 3:
        return [f"v{target.lstrip('vV')}", target.lstrip("vV")]
    full = ".".join(map(str, version))
    short = ".".join(parts)
    return list(dict.fromkeys([f"v{full}", full, f"v{short}", short]))


def display_ref(ref: str) -> str:
    return ref[:12] if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref.lower()) else ref


class GitHubRef:
    def __init__(self, fetcher: Any):
        self.fetcher = fetcher

    def documents(self, repository: str, ref: str) -> dict[str, str] | None:
        """``{file name: text}`` of the root documentation files at ``ref``; None when the ref does not exist."""
        listing = self.fetcher.get(f"https://api.github.com/repos/{repository}/contents?ref={quote(ref, safe='')}")
        if listing is None:
            return None
        try:
            entries = json.loads(listing)
        except ValueError:
            return {}
        names = sorted(e["name"] for e in entries if isinstance(e, dict) and e.get("type") == "file"
                       and isinstance(e.get("name"), str) and _DOC_NAME.match(e["name"])
                       and int(e.get("size") or 0) <= MAX_DOC_BYTES)[:MAX_FILES]
        out: dict[str, str] = {}
        for name in names:
            body = self.fetcher.get(f"https://raw.githubusercontent.com/{repository}/{quote(ref, safe='')}/{quote(name)}",
                                    accept="text/plain")
            if body is not None:
                out[name] = body.decode("utf-8", errors="replace")
        return out

    def first(self, repository: str, refs: list[str]) -> tuple[str | None, dict[str, str] | None]:
        """The first ref that exists, with its documents (failure clause 2 when none does)."""
        for ref in refs:
            documents = self.documents(repository, ref)
            if documents is not None:
                return ref, documents
        return None, None


def citation(record: Any, ref: str, version: str) -> Citation:
    return Citation(provider=PROVIDER, version_or_ref=f"{version} ({display_ref(ref)})",
                    prefix=f"https://github.com/{record.repository}/blob/{ref}")
