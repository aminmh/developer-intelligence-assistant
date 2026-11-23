"""Regression check service: resolves the revision pair and gathers evidence into the report.

Contract: @.archcore/regression/regression-check.spec.md. Static evidence reuses the change impact
engine through ``inspection.impact_service.run_impact`` (revision-pair input) and contract diffs
through ``api_contracts.breaking.classify``. Authorization stays with the caller, as for impact:
the web server authorizes the project before proxying, and the registry is project-scoped, so a
revision of another project is never found.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from regression.model import NOT_COVERED, new_report
from regression.store import RegressionStore

logger = logging.getLogger(__name__)


class RegressionRequestError(ValueError):
    """The request itself is malformed (HTTP 422)."""


class RegressionUnavailable(LookupError):
    """A revision or its data cannot be used right now (HTTP 409)."""


# --- dependencies (replaced in tests) -----------------------------------------------------------------

def _registry() -> Any:
    from telemetry.registry import default_registry

    return default_registry()


def _run_impact(project_id: str, revision_id: str, args: dict[str, Any]) -> dict[str, Any]:
    from inspection.impact_service import run_impact

    return run_impact(project_id, revision_id, args)


def _contract_store() -> Any:
    from api_contracts.store import default_store

    return default_store()


# --- clause 1, failure clause 1 -----------------------------------------------------------------------

def _ready(registry: Any, project_id: str, revision_id: str, role: str) -> dict[str, Any]:
    row = registry.get_by_revision_id(project_id, revision_id)
    if row is None:
        raise RegressionUnavailable(f"The {role} revision {revision_id} does not exist for the project.")
    if row.get("state") != "ready":
        raise RegressionUnavailable(f"The {role} revision {revision_id} is not ready.")
    if not row.get("commit_sha"):
        # Revision-pair comparison needs commit-identified revisions (rule_code.load_revision).
        raise RegressionUnavailable(f"The {role} revision {revision_id} has no commit; upload it from Git to compare.")
    return row


def resolve_pair(project_id: Any, base_revision_id: Any, candidate_revision_id: Any,
                 registry: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Both revision rows, ready and of this project; raises before any graph read."""
    for name, value in (("project_id", project_id), ("base_revision_id", base_revision_id),
                        ("candidate_revision_id", candidate_revision_id)):
        if not isinstance(value, str) or not value:
            raise RegressionRequestError(f"{name} is required.")
    if base_revision_id == candidate_revision_id:
        raise RegressionRequestError("Choose two different revisions.")
    registry = registry or _registry()
    return (_ready(registry, project_id, base_revision_id, "base"),
            _ready(registry, project_id, candidate_revision_id, "candidate"))


def create_check(project_id: Any, base_revision_id: Any, candidate_revision_id: Any, *,
                 created_by: str | None = None, store: RegressionStore | None = None,
                 registry: Any = None) -> dict[str, Any]:
    resolve_pair(project_id, base_revision_id, candidate_revision_id, registry)
    return (store or RegressionStore()).create(project_id, base_revision_id, candidate_revision_id, created_by)


# --- clauses 2-3, failure clause 2 --------------------------------------------------------------------

def _symbol_row(row: dict[str, Any]) -> dict[str, Any]:
    return {"symbol_id": row.get("symbol_id"), "canonical_symbol_id": row.get("canonical_symbol_id"),
            "fqn": row.get("fqn", ""), "kind": row.get("kind", ""), "language": row.get("language"),
            "path": row.get("path", ""), "line": row.get("line"), "change": row.get("change"),
            "revision_id": row.get("revision_id"), "verdict": None, "evidence": []}


def _route_row(row: dict[str, Any]) -> dict[str, Any]:
    return {"method": row.get("method", ""), "path": row.get("path", ""), "fqn": row.get("fqn", ""),
            "depth": row.get("depth"), "source": row.get("source"), "handler_path": row.get("handler_path", ""),
            "handler_line": row.get("handler_line"), "verdict": None, "evidence": []}


def static_evidence(project_id: str, base_revision_id: str, candidate_revision_id: str, report: dict[str, Any], *,
                    impact: Callable[[str, str, dict[str, Any]], dict[str, Any]] = _run_impact,
                    depth: int | None = None, max_nodes: int | None = None) -> dict[str, Any]:
    """Fill changed symbols, impacted routes and tests from the impact engine; returns the raw impact report."""
    args: dict[str, Any] = {"base_revision_id": base_revision_id}
    if depth is not None:
        args["depth"] = depth
    if max_nodes is not None:
        args["max_nodes"] = max_nodes
    raw = impact(project_id, candidate_revision_id, args)
    report["symbols"] = [_symbol_row(row) for row in raw.get("changed", [])]
    report["routes"] = [_route_row(row) for row in raw.get("routes", [])]
    report["impacted"] = [{key: row.get(key) for key in ("symbol_id", "canonical_symbol_id", "fqn", "path", "line",
                                                          "depth", "via", "confirmed", "test")}
                          for row in raw.get("impacted", [])]
    report["tests"] = [{key: row.get(key) for key in ("fqn", "path", "line")} for row in raw.get("tests", [])]
    report["impact"] = {key: raw.get(key) for key in ("truncated", "limit_reached", "coverage", "coverage_reasons",
                                                      "unresolved")}
    report["runtime_observed"] = bool((raw.get("runtime") or {}).get("observed"))
    if raw.get("coverage") == "unavailable":
        # Failure clause 2: no symbol may read unchanged when the comparison itself failed.
        report["coverage"] = "unavailable"
        report["notes"].append("Change impact is unavailable for this revision pair; symbol verdicts are not covered.")
        for row in report["symbols"]:
            row["verdict"] = NOT_COVERED
    elif raw.get("coverage") == "partial" or raw.get("truncated"):
        report["coverage"] = "partial"
    return raw


def contract_changes(project_id: str, base_revision_id: str, candidate_revision_id: str,
                     store: Any = None) -> list[dict[str, Any]]:
    """Clause 3: breaking-change items for each provider contract with a version on both revisions."""
    from api_contracts.breaking import classify

    try:
        store = store or _contract_store()
        attachments = store.list_attachments(project_id=project_id)
    except Exception:  # noqa: BLE001 — contracts enrich the report; it stands without them
        return []
    items: list[dict[str, Any]] = []
    for attachment in attachments:
        if attachment.get("role") != "provider":
            continue
        source_id = attachment["source_id"]
        base = store.version_for_revision(source_id, base_revision_id)
        candidate = store.version_for_revision(source_id, candidate_revision_id)
        if not base or not candidate or base.get("state") != "valid" or candidate.get("state") != "valid":
            continue
        if base["version_id"] == candidate["version_id"]:
            continue
        old = store.get_version(base["version_id"], with_model=True).get("model")
        new = store.get_version(candidate["version_id"], with_model=True).get("model")
        if not old or not new:
            continue
        items.extend({**item, "source_id": source_id} for item in classify(old, new))
    return items


def _load_revision(project_id: str, revision_id: str) -> Any:
    from code_quality.rule_code import RevisionUnavailable, load_revision

    try:
        return load_revision(project_id, revision_id)
    except RevisionUnavailable as exc:
        raise RegressionUnavailable(str(exc)) from exc


def _revision_evidence(project_id: str, revision: Any) -> tuple[dict[Any, Any], dict[str, str]]:
    """Capability collections for coverage and the checklist, plus plain project files, on one revision."""
    import io
    import zipfile

    from agent.core.capability import Capability
    from code_quality.framework_evidence import archive_context, collect_capabilities
    from regression.coverage import NEUTRAL_FILES

    archive = revision.archive_loader()
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = [info.filename for info in bundle.infolist() if not info.is_dir()]
    with archive_context(archive, names) as context:
        collections = collect_capabilities(context, {Capability.TEST_CONVENTIONS, Capability.RUN_HINTS})
        files = {name: text for name in NEUTRAL_FILES if (text := context.read_text(name)) is not None}
    return collections, files


MAX_UPGRADE_LOOKUPS = 6
MAX_NOTES_CHARS = 6000
_UPGRADE_ORDER = {"major": 0, "minor": 1, "patch": 2}


def _trim_notes(found: dict[str, Any]) -> dict[str, Any]:
    """Cited excerpts within ``MAX_NOTES_CHARS`` per package, so a report stays readable."""
    from knowledge.model import cap

    excerpts, used = [], 0
    for excerpt in found.get("excerpts") or []:
        room = MAX_NOTES_CHARS - used
        if room <= 200:
            break
        text, truncated = cap(excerpt.get("text") or "", min(room, 2000))
        used += len(text)
        excerpts.append({key: excerpt.get(key) for key in ("provider", "version_or_ref", "path_or_url", "line", "section",
                                                            "versioned")} | {"text": text,
                                                                             "truncated": truncated or excerpt.get("truncated")})
    return {"coverage": found.get("coverage"), "versions_found": found.get("versions_found") or [],
            "reasons": found.get("reasons") or [], "providers": found.get("providers") or [], "excerpts": excerpts}


def _package_upgrades(project_id: str, base: Any, candidate: Any, *, remote_allowed: bool | None = None,
                      fetcher: Any = None, registry: Any = None) -> list[dict[str, Any]]:
    """Packages the candidate's lockfile changes, with upgrade notes for the most significant upgrades.

    Notes come from the knowledge layer with the base version as installed and the candidate version as
    target, read first from the candidate's own installed copy, whose changelog already covers the new
    versions. No cache: a vendor entry is keyed by installed version and target, and the base revision's
    copy, which cannot hold these notes, would share the key.
    """
    from knowledge.service import KnowledgeRequestError, doc_sources, installed_packages, lookup
    from knowledge.settings import remote_docs_allowed
    from regression.coverage import package_changes

    base_archive, candidate_archive = base.archive_loader(), candidate.archive_loader()
    before, _ = installed_packages(base_archive, registry)
    after, _ = installed_packages(candidate_archive, registry)
    rows = package_changes(before, after)
    remote = remote_docs_allowed(project_id) if remote_allowed is None else remote_allowed
    installed = {(r.ecosystem, r.name): r for r in before}
    upgrades = sorted((row for row in rows if row["change"] in _UPGRADE_ORDER),
                      key=lambda row: (row["dev"], _UPGRADE_ORDER[row["change"]], row["name"]))
    guides: list[Any] | None = None
    for index, row in enumerate(upgrades):
        if index >= MAX_UPGRADE_LOOKUPS:
            row["notes"] = {"coverage": "unavailable", "versions_found": [], "providers": [], "excerpts": [],
                            "reasons": [f"Not looked up: notes are read for the first {MAX_UPGRADE_LOOKUPS} upgrades only."]}
            continue
        if remote and guides is None:
            guides = doc_sources(candidate_archive, registry)
        record = installed[(row["ecosystem"], row["name"])]
        try:
            found = lookup(candidate_archive, row["name"], target_version=row["to"], registry=registry,
                           packages=lambda _archive, record=record: ([record], []), remote_allowed=remote,
                           fetcher=fetcher, sources=lambda _archive: guides or [])
        except KnowledgeRequestError as exc:
            found = {"coverage": "unavailable", "reasons": [str(exc)]}
        row["notes"] = _trim_notes(found)
    return rows


def _review(project_id: str, report: dict[str, Any], base: Any, candidate: Any) -> dict[str, Any]:
    from regression.equivalence import review

    return review(project_id, report, base_definitions=base.definitions, candidate_definitions=candidate.definitions,
                  base_archive=base.archive_loader, candidate_archive=candidate.archive_loader)


def finish_static(report: dict[str, Any], collections: dict[Any, Any], files: dict[str, str]) -> None:
    """Coverage, partial evidence, tier and checklist once symbols carry verdicts (clauses 6-9)."""
    from agent.core.capability import Capability
    from regression.coverage import apply_coverage, baseline_checklist, partial_from, tier_for

    tests = getattr(collections.get(Capability.TEST_CONVENTIONS), "records", []) or []
    hints = getattr(collections.get(Capability.RUN_HINTS), "records", []) or []
    apply_coverage(report, tests=tests)
    report["partial"] = partial_from(collections)
    report["tier"] = tier_for(report)
    report["baseline_checklist"] = baseline_checklist(files, hints)


def run_static(project_id: str, check_id: str, *, store: RegressionStore | None = None, registry: Any = None,
               impact: Callable[[str, str, dict[str, Any]], dict[str, Any]] = _run_impact,
               contracts: Any = None, revisions: Callable[[str, str], Any] = _load_revision,
               reviewer: Callable[..., dict[str, Any]] = _review,
               evidence: Callable[[str, Any], tuple[dict[Any, Any], dict[str, str]]] = _revision_evidence,
               upgrades: Callable[[str, Any, Any], list[dict[str, Any]]] = _package_upgrades,
               ) -> dict[str, Any]:
    """Run the static part of a check and save the report."""
    store = store or RegressionStore()
    check = store.get(project_id, check_id)
    base_id, candidate_id = check["base_revision_id"], check["candidate_revision_id"]
    store.save(project_id, check_id, state="running")
    try:
        resolve_pair(project_id, base_id, candidate_id, registry)
        report = new_report(check_id, project_id, base_id, candidate_id)
        static_evidence(project_id, base_id, candidate_id, report, impact=impact)
        report["contract_changes"] = contract_changes(project_id, base_id, candidate_id, contracts)
        base, candidate = revisions(project_id, base_id), revisions(project_id, candidate_id)
        if report["coverage"] != "unavailable":
            report["review"] = reviewer(project_id, report, base, candidate)
        try:
            report["package_changes"] = upgrades(project_id, base, candidate)
        except Exception as exc:  # noqa: BLE001 — the rest of the report stands; the gap is stated
            logger.warning("regression_package_changes_failed error_type=%s", type(exc).__name__)
            report["notes"].append("Locked package versions could not be compared between the two revisions.")
        try:
            collections, files = evidence(project_id, candidate)
        except Exception as exc:  # noqa: BLE001 — coverage still runs; the gap is stated
            logger.warning("regression_evidence_failed error_type=%s", type(exc).__name__)
            collections, files = {}, {}
            report["notes"].append("Tests and run hints could not be read from the candidate revision.")
        finish_static(report, collections, files)
    except (RegressionRequestError, RegressionUnavailable) as exc:
        return store.save(project_id, check_id, state="failed", error=str(exc))
    except Exception as exc:  # noqa: BLE001 — impact errors map to a failed check, not a crash
        return store.save(project_id, check_id, state="failed", error=f"{type(exc).__name__}: {exc}")
    return store.save(project_id, check_id, state="done", report=report)


# --- background runs ----------------------------------------------------------------------------------

def start_static(project_id: str, base_revision_id: Any, candidate_revision_id: Any, *,
                 created_by: str | None = None, store: RegressionStore | None = None,
                 runner: Callable[..., Any] | None = None, background: bool = True) -> dict[str, Any]:
    """Create a check and run its static part; one active check per project (409 otherwise)."""
    import threading

    store = store or RegressionStore()
    active = [c for c in store.list(project_id) if c["state"] in ("pending", "running")]
    if active:
        raise RegressionBusy(active[0]["check_id"])
    check = create_check(project_id, base_revision_id, candidate_revision_id, created_by=created_by, store=store)
    runner = runner or run_static
    if background:
        threading.Thread(target=runner, args=(project_id, check["check_id"]), kwargs={"store": store},
                         name=f"regression-{check['check_id']}", daemon=True).start()
    else:
        runner(project_id, check["check_id"], store=store)
    return store.get(project_id, check["check_id"])


class RegressionBusy(RuntimeError):
    """A check is already pending or running for the project (HTTP 409)."""

    def __init__(self, check_id: str):
        super().__init__(f"Regression check {check_id} is still running for this project.")
        self.check_id = check_id
