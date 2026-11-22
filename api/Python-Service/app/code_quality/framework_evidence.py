"""Framework-neutral Code Quality checks over plugin-provided records.

Decision: @.archcore/code-quality/framework-knowledge-lives-in-plugins-over-language-facts.adr.md.
Each check declares the capabilities it needs and reads only normalized records
(@api/Python-Service/app/agent/core/records.py) that ``PluginRegistry.collect``
gathers from every detected framework plugin. Adding a framework means adding a
plugin that provides those capabilities; checks here never name a framework.

Coverage: for every capability a check needs, the report gets its status,
providers and gaps. A detected framework that does not provide a capability
makes it ``partial`` with the plugin named (plan task 6e); no detected framework
providing it makes it ``unavailable``.
"""

from __future__ import annotations

import io
import logging
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from agent.core.capability import Capability

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FrameworkCheck:
    """A framework-neutral check: capabilities in, normalized findings out."""

    name: str
    version: str
    capabilities: tuple[Capability, ...]
    # collections by capability → findings, or (findings, summary) where the summary joins the coverage row
    run: Callable[..., Any]
    # when True, run(collections, settings) also receives the project's check settings
    accepts_settings: bool = False
    # when True, the RevisionContext is passed as the last argument (evidence-file collectors read files)
    accepts_context: bool = False
    # when True, the check runs even if no detected plugin provides its capabilities (it has neutral evidence too)
    runs_without_providers: bool = False
    # the assessment category the check collects evidence for, when it has one (evidence categories)
    category: str | None = None


def _registered_checks() -> list[FrameworkCheck]:
    from code_quality import (accessibility_checks, config_risks, documentation_checks, localization_checks,
                              process_checks, route_authorization, testing_checks)

    return [route_authorization.check(), config_risks.check(), process_checks.check(), testing_checks.check(),
            documentation_checks.check(), localization_checks.check(), accessibility_checks.check()]


# Registered framework-neutral checks.
FRAMEWORK_CHECKS: list[FrameworkCheck] = _registered_checks()


def collect_capabilities(context: Any, capabilities: set[Capability], registry: Any = None) -> dict[Capability, Any]:
    """``CapabilityCollection`` per capability for one revision."""
    if registry is None:
        from agent import default_registry

        registry = default_registry()
    return {capability: registry.collect(capability, context) for capability in sorted(capabilities, key=lambda c: c.value)}


def run_framework_checks(context: Any, checks: list[FrameworkCheck] | None = None, registry: Any = None,
                         settings: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Findings of every registered check, and coverage per check and capability. Never raises."""
    checks = FRAMEWORK_CHECKS if checks is None else checks
    if not checks:
        return [], {"checks": [], "capabilities": []}
    needed = {capability for check in checks for capability in check.capabilities}
    collections = collect_capabilities(context, needed, registry)
    findings: list[dict[str, Any]] = []
    check_rows = []
    for check in checks:
        own = {capability: collections[capability] for capability in check.capabilities}
        statuses = {collection.status for collection in own.values()}
        if (statuses == {"unavailable"} or not statuses) and not check.runs_without_providers:
            check_rows.append({"name": check.name, "version": check.version, "status": "unavailable",
                               "reason": "No detected framework plugin provides "
                                         + ", ".join(c.value for c in check.capabilities) + "."})
            continue
        summary = None
        try:
            arguments: list[Any] = [own]
            if check.accepts_settings:
                arguments.append(settings or {})
            if check.accepts_context:
                arguments.append(context)
            produced = check.run(*arguments) or []
            if isinstance(produced, tuple):
                produced, summary = produced
            produced = list(produced)
        except Exception as exc:  # noqa: BLE001 — a failing check is reported, never fatal
            logger.warning("framework check %s failed: %s", check.name, exc)
            check_rows.append({"name": check.name, "version": check.version, "status": "failed",
                               "reason": f"{check.name} failed ({type(exc).__name__}).",
                               **({"category": check.category} if check.category else {})})
            continue
        for finding in produced:
            finding.setdefault("tool", check.name)
            finding.setdefault("tool_version", check.version)
        findings.extend(produced)
        check_rows.append({"name": check.name, "version": check.version,
                           "status": "partial" if "partial" in statuses or "unavailable" in statuses else "complete",
                           "findings": len(produced), **({"summary": summary} if summary is not None else {}),
                           **({"category": check.category} if check.category else {})})
    return findings, {"checks": check_rows,
                      "capabilities": [collection.coverage() for collection in collections.values()]}


@contextmanager
def archive_context(archive_data: bytes, files: list[str], archive_sha256: str | None = None) -> Iterator[Any]:
    """A ``RevisionContext`` over the inventoried files plus manifests and configuration (never secrets)."""
    from agent.core.revision import RevisionContext, is_secret_path
    from code_quality.service import MAX_FILE_BYTES, _safe_archive_path

    selected = set(files)
    names: list[str] = []
    with tempfile.TemporaryDirectory(prefix="framework-evidence-") as root:
        with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
            for member in archive.infolist():
                relative = _safe_archive_path(member.filename)
                if relative and not member.is_dir():
                    names.append(relative)
                if (not relative or member.is_dir() or member.file_size > MAX_FILE_BYTES or is_secret_path(relative)
                        or (relative not in selected and not _is_framework_evidence_file(relative))):
                    continue
                target = Path(root, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
        context = RevisionContext.from_directory(root, archive_sha256=archive_sha256)
        prefix = context.root.relative_to(Path(root).resolve()).as_posix() if context.root != Path(root).resolve() else ""
        strip = prefix + "/" if prefix and prefix != "." else ""
        context.archive_files = frozenset(name[len(strip):] for name in names if name.startswith(strip))
        yield context


def run_on_archive(archive_data: bytes, files: list[str], archive_sha256: str | None = None,
                   checks: list[FrameworkCheck] | None = None, registry: Any = None,
                   settings: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract the inventoried files to a temporary tree and run the framework checks on it."""
    checks = FRAMEWORK_CHECKS if checks is None else checks
    if not checks:
        return [], {"checks": [], "capabilities": []}
    with archive_context(archive_data, files, archive_sha256) as context:
        return run_framework_checks(context, checks, registry, settings)


def taint_stubs(archive_data: bytes, files: list[str], analyzer: str = "psalm", language: str = "php",
                registry: Any = None) -> tuple[dict[str, str], dict[str, Any]]:
    """``({file name: stub}, coverage)`` of the taint models the detected framework plugins provide.

    Coverage gaps are kept only for detected frameworks of ``language``: a front-end
    framework is not expected to model PHP taint.
    """
    from agent.core.records import TaintStubRecord

    if registry is None:
        from agent import default_registry

        registry = default_registry()
    with archive_context(archive_data, files) as context:
        collection = collect_capabilities(context, {Capability.TAINT_MODEL}, registry)[Capability.TAINT_MODEL]
    stubs = {record.name: record.content for record in collection.records
             if isinstance(record, TaintStubRecord) and record.analyzer == analyzer}
    languages = {name: getattr(plugin, "language", None) for name, plugin in registry.frameworks.items()}
    coverage = collection.coverage()
    coverage["gaps"] = [gap for gap in coverage.get("gaps") or [] if languages.get(gap.get("framework")) == language]
    return stubs, coverage


# Non-source files framework plugins read (manifests and configuration); source files come from the inventory.
_EVIDENCE_SUFFIXES = (".json", ".yaml", ".yml", ".xml", ".neon", ".lock", ".dist")


def _is_framework_evidence_file(relative: str) -> bool:
    name = relative.rsplit("/", 1)[-1]
    return name.endswith(_EVIDENCE_SUFFIXES) and "/vendor/" not in f"/{relative}" and "/node_modules/" not in f"/{relative}"
