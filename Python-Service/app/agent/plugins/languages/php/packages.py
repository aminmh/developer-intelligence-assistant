"""PHP ``installed_packages``: what ``composer.lock`` pins, as normalized ``PackageRecord`` rows.

Read as JSON data; Composer is never run. The lockfile names the exact version, the tag as written,
and usually the source commit, which lets the knowledge layer read documentation at exactly the
installed code (@.archcore/knowledge-layer/framework-knowledge-layer.spec.md).
"""

from __future__ import annotations

import json
import re

from agent.core.records import PackageRecord
from agent.core.revision import RevisionContext

LOCKFILE = "composer.lock"
_GITHUB = re.compile(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def normalize_version(tag: str) -> str:
    """``v7.4.18`` → ``7.4.18``; branch aliases such as ``dev-main`` stay as written."""
    return tag[1:] if re.match(r"^v\d", tag) else tag


def github_repository(url: str | None) -> str | None:
    match = _GITHUB.search(url or "")
    return f"{match.group(1)}/{match.group(2)}" if match else None


def _record(row: dict, dev: bool) -> PackageRecord | None:
    name, tag = row.get("name"), row.get("version")
    if not isinstance(name, str) or not isinstance(tag, str) or "/" not in name:
        return None
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    dist = row.get("dist") if isinstance(row.get("dist"), dict) else {}
    reference = source.get("reference") or dist.get("reference")
    return PackageRecord(ecosystem="composer", name=name, version=normalize_version(tag), tag=tag, lockfile=LOCKFILE,
                         path=f"vendor/{name}", repository=github_repository(source.get("url") or dist.get("url")),
                         reference=reference if isinstance(reference, str) and reference else None, dev=dev)


def provide_installed_packages(context: RevisionContext) -> list[PackageRecord]:
    """Every package in ``composer.lock``; an absent lockfile gives none, an unreadable one raises."""
    from agent.interfaces.capabilities import CapabilityUnavailable

    if not context.exists(LOCKFILE):
        return []
    text = context.read_text(LOCKFILE)
    try:
        data = json.loads(text or "")
    except ValueError as exc:
        raise CapabilityUnavailable("composer.lock is unreadable or too large, so installed versions are unknown.") from exc
    records = []
    for key, dev in (("packages", False), ("packages-dev", True)):
        for row in data.get(key) or []:
            record = _record(row, dev) if isinstance(row, dict) else None
            if record:
                records.append(record)
    return records
