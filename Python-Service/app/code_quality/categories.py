"""The ten assessment categories and their default weights.

Decision: assessment-scores-ten-categories (accepted 2026-11-16). The five source categories
are scored from source analysis as before. The five evidence categories are scored only from
their own non-source evidence (CI, tests, docs, translations, templates); a category without
that evidence is unassessed with a reason, and the overall score is then omitted.
"""

from __future__ import annotations

from typing import Any

SOURCE_CATEGORIES = ("security", "clean_code", "maintainability", "reliability", "performance")
EVIDENCE_CATEGORIES = ("process", "testing", "documentation", "localization", "accessibility")
CATEGORIES = SOURCE_CATEGORIES + EVIDENCE_CATEGORIES

LABELS = {
    "security": "Security", "clean_code": "Architecture and design", "maintainability": "Maintainability",
    "reliability": "Reliability", "performance": "Performance", "process": "Process", "testing": "Testing",
    "documentation": "Documentation", "localization": "Localization", "accessibility": "Accessibility",
}

DEFAULT_WEIGHTS = {category: 100 // len(CATEGORIES) for category in CATEGORIES}


def label(category: str) -> str:
    return LABELS.get(category, category.replace("_", " ").capitalize())


def migrate_weights(value: Any) -> Any:
    """A saved five-category preset as ten categories; anything else is returned unchanged.

    The five source weights are scaled to half the total (largest remainder, so they still sum
    to exactly 50) and each new category gets the default 10, keeping the user's relative
    priorities among the original five.
    """
    if not isinstance(value, dict) or set(value) != set(SOURCE_CATEGORIES):
        return value
    if any(type(value[key]) is not int or value[key] < 0 for key in SOURCE_CATEGORIES) or sum(value.values()) != 100:
        return value
    share = 100 - sum(DEFAULT_WEIGHTS[key] for key in EVIDENCE_CATEGORIES)
    exact = {key: value[key] * share / 100 for key in SOURCE_CATEGORIES}
    scaled = {key: int(exact[key]) for key in SOURCE_CATEGORIES}
    remainder = share - sum(scaled.values())
    for key in sorted(SOURCE_CATEGORIES, key=lambda k: (-(exact[k] - scaled[k]), SOURCE_CATEGORIES.index(k)))[:remainder]:
        scaled[key] += 1
    return {**scaled, **{key: DEFAULT_WEIGHTS[key] for key in EVIDENCE_CATEGORIES}}
