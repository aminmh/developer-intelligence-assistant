"""Deterministic, evidence-backed category scoring for Code Quality reports."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from code_quality.categories import CATEGORIES

SCORING_VERSION = "deterministic-category-score-2"  # ten categories

# Penalties are deliberately small and versioned. The score is a stable baseline;
# rule authors can refine a rule's severity without making the model choose the
# official number for a report.
SEVERITY_PENALTIES = {
    "critical": 24,
    "error": 18,
    "major": 12,
    "warning": 7,
    "minor": 4,
    "info": 2,
    "note": 1,
}


def _kind(row: dict[str, Any]) -> str:
    return str(row.get("kind") or "").casefold()


def _is_deterministic(row: dict[str, Any]) -> bool:
    """Exclude model-generated observations from the official baseline."""
    if _kind(row) != "finding":
        return False
    tool = str(row.get("tool") or "").casefold()
    return not tool.startswith("ai-") and tool != "php-solid-design-signals"


def _confidence(status: str, findings: list[dict[str, Any]], failures: list[dict[str, Any]]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if status == "unavailable":
        return "low", ["No supported source coverage was available for this category."]
    if status == "partial":
        reasons.append("One or more applicable analyzers were partial, skipped, or failed.")
    if not findings:
        reasons.append("No deterministic finding was emitted; the score reflects completed scan coverage only.")
    if failures:
        reasons.append("Analyzer failures are recorded in the report and limit the scope of this score.")
    reasons.append("Experimental rule weights and category coverage have not been calibrated.")
    return "low", reasons


def score_categories(
    evidence: Iterable[dict[str, Any]],
    coverage: dict[str, dict[str, Any]],
    analyzer_failures: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return official scores without using model-generated numeric opinions."""
    rows = list(evidence)
    failures = analyzer_failures or []
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    coverage_ids: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        category = row.get("category")
        if category not in CATEGORIES:
            continue
        if _is_deterministic(row):
            by_category[category].append(row)
        if _kind(row) == "coverage" and isinstance(row.get("evidence_id"), str):
            coverage_ids[category].append(row["evidence_id"])

    result: dict[str, dict[str, Any]] = {}
    for category in CATEGORIES:
        category_coverage = coverage.get(category) or {}
        supported = bool(category_coverage.get("supported"))
        status = str(category_coverage.get("status") or ("covered" if supported else "unavailable"))
        category_rows = by_category[category]
        finding_ids = [row["evidence_id"] for row in category_rows if isinstance(row.get("evidence_id"), str)]
        evidence_ids = list(dict.fromkeys([*finding_ids, *coverage_ids[category]]))
        confidence, confidence_reasons = _confidence(status, category_rows, failures)
        if not supported:
            result[category] = {
                "assessed": False,
                "score": None,
                "reason": category_coverage.get("reason") or "No supported source coverage was available for this category.",
                "evidence_ids": [],
                "score_method": SCORING_VERSION,
                "assessment_status": "unavailable",
                "confidence": confidence,
                "confidence_reasons": confidence_reasons,
                "finding_count": 0,
                "deterministic_evidence_ids": [],
            }
            continue

        penalty = min(100, sum(
            SEVERITY_PENALTIES.get(str(row.get("severity") or "info").casefold(), SEVERITY_PENALTIES["info"])
            for row in category_rows
        ))
        score = max(0, 100 - penalty)
        assessment_status = "provisional"
        scope = "completed analyzer coverage" if status == "covered" else "partial analyzer coverage"
        reason = (
            f"Deterministic baseline score from {len(category_rows)} category findings and {scope}. "
            "A zero-finding result means no configured issue pattern was detected in the scanned scope; it does not prove defect-free code."
            if not category_rows else
            f"Deterministic baseline score from {len(category_rows)} cited category findings and {scope}."
        )
        result[category] = {
            "assessed": True,
            "score": score,
            "reason": reason,
            "evidence_ids": evidence_ids,
            "score_method": SCORING_VERSION,
            "assessment_status": assessment_status,
            "confidence": confidence,
            "confidence_reasons": confidence_reasons,
            "finding_count": len(category_rows),
            "deterministic_evidence_ids": evidence_ids,
        }
    return result
