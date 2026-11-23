"""Knowledge service: resolve the installed version, ask providers in order, return cited excerpts.

Spec clauses 1-3, 7-8, 11 and failure clauses 4-5. Phase 1 has only the ``vendor`` provider, so a
library the upload does not contain reads ``partial``/``unavailable`` with a reason; later phases add
tag-pinned repository files and GitMCP behind the project switch (clauses 4-6, 9-10).
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


def lookup(archive: bytes, library: Any, topic: Any = None, target_version: Any = None, *,
           cache: KnowledgeCache | None = None, registry: Any = None,
           packages: Callable[[bytes], tuple[list[Any], list[str]]] | None = None) -> dict[str, Any]:
    """Cited excerpts for ``library`` at the version the revision installs."""
    from knowledge.model import parse_version
    from knowledge.providers.vendor import PROVIDER as VENDOR
    from knowledge.providers.vendor import VendorDocs, topic_excerpts, upgrade_excerpts

    library, topic, target = _validate(library, topic, target_version)
    records, reasons = (packages or (lambda a: installed_packages(a, registry)))(archive)
    record, missing = find_package(records, library)
    if record is None:
        return {"library": library, "installed_version": None, "excerpts": [], "coverage": UNAVAILABLE,
                "reasons": [missing, *reasons], "versions_found": []}
    if target and (parse_version(target) is None or parse_version(record.version) is None):
        raise KnowledgeRequestError(f"Cannot compare {record.version} with target {target}.")
    result: dict[str, Any] = {"library": record.name, "installed_version": record.version, "lockfile": record.lockfile,
                              "excerpts": [], "coverage": COMPLETE, "reasons": [], "versions_found": []}
    key = cache_key(VENDOR, record.name, record.version, topic, target)
    cached = cache.get(key) if cache else None
    if cached is None:
        docs = VendorDocs(archive)
        if not docs.present(record.path):
            cached = {"excerpts": [], "versions_found": [], "reasons": [
                f"The upload does not include the installed copy ({record.path}); later providers read the "
                f"library's files at {record.tag}."]}
        elif target:
            excerpts, versions = upgrade_excerpts(docs, record, target)
            cached = {"excerpts": [e.to_dict() for e in excerpts], "versions_found": versions, "reasons": [] if excerpts else [
                f"The installed copy ({record.version}) has no upgrade notes after it up to {target}; notes for newer "
                "versions are in the target version's own files."]}
        else:
            excerpts = topic_excerpts(docs, record, topic)
            cached = {"excerpts": [e.to_dict() for e in excerpts], "versions_found": [], "reasons": [] if excerpts else [
                f"The installed copy of {record.name} has no documentation matching \"{topic}\"."]}
        if cache:
            cache.put(key, VENDOR, record.name, record.version, cached)
    excerpts = _limit_total([Excerpt(**row) for row in cached["excerpts"]])
    result["excerpts"] = [e.to_dict() for e in excerpts]
    result["versions_found"] = cached["versions_found"]
    result["reasons"] = list(cached["reasons"])
    if not excerpts:
        result["coverage"] = UNAVAILABLE
    elif result["reasons"]:
        result["coverage"] = PARTIAL
    return result


def lookup_revision(project_id: str, revision_id: str, library: Any, topic: Any = None,
                    target_version: Any = None, *, cache: KnowledgeCache | None = None) -> dict[str, Any]:
    """``lookup`` on an indexed revision's archive."""
    from code_quality.rule_code import load_revision

    revision = load_revision(project_id, revision_id)
    return {"project_id": project_id, "revision_id": revision_id,
            **lookup(revision.archive_loader(), library, topic, target_version, cache=cache or KnowledgeCache())}


def archive_names(archive: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        return [info.filename for info in bundle.infolist() if not info.is_dir()]
