"""Report shape of a regression check (spec Surface: report fields and verdicts)."""

from __future__ import annotations

from typing import Any

UNCHANGED = "unchanged"
CHANGED_EXPECTED = "changed_expected"
SUSPECTED_REGRESSION = "suspected_regression"
NOT_COVERED = "not_covered"
VERDICTS = (UNCHANGED, CHANGED_EXPECTED, SUSPECTED_REGRESSION, NOT_COVERED)

TIER_STATIC = "static"
TIER_REPLAY = "replay"
TIER_TELEMETRY = "telemetry"
TIERS = (TIER_STATIC, TIER_REPLAY, TIER_TELEMETRY)

STATES = ("pending", "running", "done", "failed")

# Every equivalence verdict carries this label (spec clause 5).
JUDGMENT_LABEL = "model judgment, not proof"


def new_report(check_id: str, project_id: str, base_revision_id: str, candidate_revision_id: str) -> dict[str, Any]:
    """An empty report: every list present, so readers never test for missing keys."""
    return {
        "check_id": check_id,
        "project_id": project_id,
        "base_revision_id": base_revision_id,
        "candidate_revision_id": candidate_revision_id,
        "tier": TIER_STATIC,
        "coverage": "full",
        "routes": [],
        "symbols": [],
        "uncovered": [],
        "partial": [],
        "contract_changes": [],
        "baseline_checklist": [],
        "notes": [],
    }


def verdict_counts(report: dict[str, Any]) -> dict[str, int]:
    """How many routes and symbols carry each verdict."""
    counts = {verdict: 0 for verdict in VERDICTS}
    for row in [*report.get("routes", []), *report.get("symbols", [])]:
        verdict = row.get("verdict")
        if verdict in counts:
            counts[verdict] += 1
    return counts
