"""Knowledge service: resolve the installed version, ask providers in order, return cited excerpts.

Spec clauses 1-4, 7-9, 11 and failure clauses 1-5. Providers run in order: the installed copy in the
revision, then (when the project allows remote documentation) the library's files at the installed
commit or tag, or at the target version's tag for upgrade notes. GitMCP follows in a later phase.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any, Callable

from knowledge.cache import KnowledgeCache, cache_key
from knowledge.model import COMPLETE, MAX_REQUEST_CHARS, PARTIAL, UNAVAILABLE, Excerpt, cap


class KnowledgeRequestError(ValueError):
    """The request itself is malformed (HTTP 422, tool rejection)."""


# --- installed versions (clauses 1-2, failure clause 5) -------------------------------------------------

def installed_packages(archive: bytes, registry: Any = None) -> tuple[list[Any], list[str]]:
    """``INSTALLED_PACKAGES`` records of a revision, and reasons when a plugin could not read its lockfile.

    Lockfiles outside ``vendor/`` and ``node_modules/`` are extracted by ``archive_context``; the
    installed packages themselves are never extracted.
    """
    from agent.core.capability import Capability
    from code_quality.framework_evidence import archive_context, collect_capabilities

    with archive_context(archive, []) as context:
        collection = collect_capabilities(context, {Capability.INSTALLED_PACKAGES}, registry)[Capability.INSTALLED_PACKAGES]
    reasons = [gap["reason"] for gap in collection.gaps]
    if not collection.detected:
        reasons.append("No language plugin was detected for this revision, so installed versions are unknown.")
    return list(collection.records), reasons


def find_package(records: list[Any], library: str) -> tuple[Any | None, str | None]:
    """Exact name first, then a unique short name (``http-foundation`` for ``symfony/http-foundation``)."""
    wanted = library.strip().lower()
    exact = [r for r in records if r.name.lower() == wanted]
    if exact:
        return exact[0], None
    short = [r for r in records if r.name.lower().rsplit("/", 1)[-1] == wanted]
    if len(short) == 1:
        return short[0], None
    if len(short) > 1:
        return None, f"{library} matches several packages: {', '.join(sorted(r.name for r in short))}. Name one."
    return None, f"No lockfile in this revision pins {library}."


# --- lookup ---------------------------------------------------------------------------------------------

def _validate(library: Any, topic: Any, target_version: Any) -> tuple[str, str, str | None]:
    if not isinstance(library, str) or not library.strip() or len(library) > 200:
        raise KnowledgeRequestError("library is required (a package name such as symfony/http-foundation).")
    if topic is not None and (not isinstance(topic, str) or len(topic) > 300):
        raise KnowledgeRequestError("topic must be text of at most 300 characters.")
    if target_version is not None and (not isinstance(target_version, str) or not target_version.strip()):
        raise KnowledgeRequestError("target_version must be a version such as 8.0.")
    return library.strip(), (topic or "").strip(), target_version.strip() if target_version else None


def _limit_total(excerpts: list[Excerpt]) -> list[Excerpt]:
    """Failure clause 4 across a request: stop at ``MAX_REQUEST_CHARS``, truncating the excerpt that crosses it."""
    out, used = [], 0
    for excerpt in excerpts:
        room = MAX_REQUEST_CHARS - used
        if room <= 200:
            break
        if len(excerpt.text) > room:
            excerpt.text, _ = cap(excerpt.text, room)
            excerpt.truncated = True
        used += len(excerpt.text)
        out.append(excerpt)
    return out


def _vendor(archive: bytes, record: Any, topic: str, target: str | None, cache: KnowledgeCache | None) -> dict[str, Any]:
    from knowledge.providers.vendor import PROVIDER, VendorDocs, topic_excerpts, upgrade_excerpts

    key = cache_key(PROVIDER, record.name, record.version, topic, target)
    cached = cache.get(key) if cache else None
    if cached is not None:
        return cached
    docs = VendorDocs(archive)
    if not docs.present(record.path):
        found = {"excerpts": [], "versions_found": [], "missing_copy": True, "reasons": [
            f"The upload does not include the installed copy ({record.path})."]}
    elif target:
        excerpts, versions = upgrade_excerpts(docs, record, target)
        found = {"excerpts": [e.to_dict() for e in excerpts], "versions_found": versions, "reasons": [] if excerpts else [
            f"The installed copy ({record.version}) has no upgrade notes after it up to {target}."]}
    else:
        excerpts = topic_excerpts(docs, record, topic)
        found = {"excerpts": [e.to_dict() for e in excerpts], "versions_found": [], "reasons": [] if excerpts else [
            f"The installed copy of {record.name} has no documentation matching \"{topic}\"."]}
    if cache:
        cache.put(key, PROVIDER, record.name, record.version, found)
    return found


def _github(record: Any, topic: str, target: str | None, cache: KnowledgeCache | None, github: Any) -> dict[str, Any]:
    """Clause 4: the installed version at its commit or tag, or, with a target, the target's own files."""
    from knowledge.providers import github_ref
    from knowledge.providers.vendor import topic_from_documents, upgrade_from_documents

    if not record.repository:
        return {"excerpts": [], "versions_found": [], "reasons": [
            f"The lockfile names no GitHub repository for {record.name}, so its files cannot be read at a tag."]}
    refs = github_ref.target_refs(target) if target else github_ref.installed_refs(record)
    key = cache_key(github_ref.PROVIDER, record.name, "|".join(refs), topic, target)
    cached = cache.get(key) if cache else None
    if cached is not None:
        return cached
    ref, documents = github.first(record.repository, refs)
    if ref is None:  # failure clause 2
        return {"excerpts": [], "versions_found": [], "reasons": [
            f"No ref of {record.repository} matches {target or record.version} (tried {', '.join(refs)})."]}
    citation = github_ref.citation(record, ref, target or record.version)
    if target:
        excerpts, versions = upgrade_from_documents(documents, record, target, citation)
        missing = f"{record.repository} at {github_ref.display_ref(ref)} has no upgrade notes after {record.version}."
    else:
        excerpts, versions = topic_from_documents(documents, record, topic, citation), []
        missing = f"{record.repository} at {github_ref.display_ref(ref)} has no documentation matching \"{topic}\"."
    found = {"excerpts": [e.to_dict() for e in excerpts], "versions_found": versions,
             "reasons": [] if excerpts else [missing]}
    if cache:
        cache.put(key, github_ref.PROVIDER, record.name, ref, found)
    return found


def lookup(archive: bytes, library: Any, topic: Any = None, target_version: Any = None, *,
           cache: KnowledgeCache | None = None, registry: Any = None,
           packages: Callable[[bytes], tuple[list[Any], list[str]]] | None = None,
           remote_allowed: bool = False, fetcher: Any = None) -> dict[str, Any]:
    """Cited excerpts for ``library`` at the version the revision installs, from providers in order.

    Clauses 3-4, 8-9 and failure clauses 1-3: the installed copy first; with remote documentation
    allowed, the library's files at the installed commit or tag when the copy is missing or silent, and
    the target version's files for upgrade notes, since an installed copy cannot describe newer versions.
    """
    from knowledge.http import Fetcher, RemoteUnavailable
    from knowledge.model import parse_version
    from knowledge.providers.github_ref import GitHubRef
    from knowledge.settings import OFF_REASON

    library, topic, target = _validate(library, topic, target_version)
    records, reasons = (packages or (lambda a: installed_packages(a, registry)))(archive)
    record, missing = find_package(records, library)
    if record is None:
        return {"library": library, "installed_version": None, "excerpts": [], "coverage": UNAVAILABLE,
                "reasons": [missing, *reasons], "versions_found": [], "providers": []}
    if target and (parse_version(target) is None or parse_version(record.version) is None):
        raise KnowledgeRequestError(f"Cannot compare {record.version} with target {target}.")
    vendor = _vendor(archive, record, topic, target, cache)
    excerpts = [Excerpt(**row) for row in vendor["excerpts"]]
    versions = set(vendor["versions_found"])
    gaps: list[str] = list(vendor["reasons"])
    providers = ["vendor"]
    needs_remote = bool(target) or not excerpts
    if needs_remote and not remote_allowed:
        gaps.append(OFF_REASON)
    elif needs_remote:
        providers.append("github_ref")
        try:
            remote = _github(record, topic, target, cache, GitHubRef(fetcher or Fetcher()))
        except RemoteUnavailable as exc:  # failure clauses 1 and 3
            remote = {"excerpts": [], "versions_found": [], "reasons": [str(exc)]}
        if remote["excerpts"]:
            gaps = []  # the later provider covered what the installed copy could not
        excerpts += [Excerpt(**row) for row in remote["excerpts"]]
        versions |= set(remote["versions_found"])
        gaps += remote["reasons"]
    excerpts = _limit_total(excerpts)
    coverage = UNAVAILABLE if not excerpts else (PARTIAL if gaps else COMPLETE)
    return {"library": record.name, "installed_version": record.version, "lockfile": record.lockfile,
            "excerpts": [e.to_dict() for e in excerpts], "coverage": coverage, "reasons": gaps,
            "versions_found": sorted(versions, key=lambda v: parse_version(v) or (0, 0, 0)), "providers": providers}


def lookup_revision(project_id: str, revision_id: str, library: Any, topic: Any = None,
                    target_version: Any = None, *, cache: KnowledgeCache | None = None) -> dict[str, Any]:
    """``lookup`` on an indexed revision's archive."""
    from code_quality.rule_code import load_revision

    from knowledge.settings import remote_docs_allowed

    revision = load_revision(project_id, revision_id)
    return {"project_id": project_id, "revision_id": revision_id,
            **lookup(revision.archive_loader(), library, topic, target_version, cache=cache or KnowledgeCache(),
                     remote_allowed=remote_docs_allowed(project_id))}


def archive_names(archive: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        return [info.filename for info in bundle.infolist() if not info.is_dir()]
