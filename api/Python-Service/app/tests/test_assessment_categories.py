"""Ten assessment categories (decision assessment-scores-ten-categories; gap-closing plan, Phase 0)."""

import json

import pytest

from code_quality import categories
from code_quality.service import ReportStore, _category_coverage, _score


def test_ten_categories_with_equal_default_weights():
    assert categories.CATEGORIES == categories.SOURCE_CATEGORIES + categories.EVIDENCE_CATEGORIES
    assert len(categories.CATEGORIES) == 10 and set(categories.LABELS) == set(categories.CATEGORIES)
    assert categories.DEFAULT_WEIGHTS == {category: 10 for category in categories.CATEGORIES}
    assert categories.label("clean_code") == "Architecture and design"


@pytest.mark.parametrize("old, source", [
    ({"security": 20, "clean_code": 20, "maintainability": 20, "reliability": 20, "performance": 20}, [10, 10, 10, 10, 10]),
    ({"security": 40, "clean_code": 30, "maintainability": 15, "reliability": 15, "performance": 0}, [20, 15, 8, 7, 0]),
    ({"security": 33, "clean_code": 33, "maintainability": 34, "reliability": 0, "performance": 0}, [17, 16, 17, 0, 0]),
])
def test_five_category_presets_scale_into_ten(old, source):
    migrated = categories.migrate_weights(old)
    assert [migrated[key] for key in categories.SOURCE_CATEGORIES] == source
    assert all(migrated[key] == 10 for key in categories.EVIDENCE_CATEGORIES) and sum(migrated.values()) == 100


def test_other_values_are_not_migrated():
    ten = dict(categories.DEFAULT_WEIGHTS)
    broken = {"security": 50, "clean_code": 50, "maintainability": 50, "reliability": 0, "performance": 0}
    assert categories.migrate_weights(ten) is ten and categories.migrate_weights(broken) is broken
    assert categories.migrate_weights(None) is None


def test_saved_five_category_preset_reads_back_as_ten(tmp_path):
    from hashlib import sha256

    store = ReportStore(tmp_path / "reports.sqlite")
    with store._connect() as db:
        db.execute("INSERT INTO assessment_preferences(user_key,weights_json,updated_at) VALUES(?,?,?)",
                   (sha256(b"alice").hexdigest(), json.dumps({"security": 60, "clean_code": 10, "maintainability": 10,
                                                               "reliability": 10, "performance": 10}), "2026-11-01"))
    weights = store.weights("alice")
    assert weights["security"] == 30 and weights["testing"] == 10 and sum(weights.values()) == 100
    assert store.weights("bob") == categories.DEFAULT_WEIGHTS
    with pytest.raises(Exception):
        store.save_weights("alice", {"security": 20, "clean_code": 20, "maintainability": 20, "reliability": 20, "performance": 20})


def test_evidence_categories_are_supported_only_by_their_own_findings():
    evidence = [{"evidence_id": "ev_1", "category": "testing", "kind": "finding"},
                {"evidence_id": "ev_2", "category": "security", "kind": "finding"}]
    coverage = _category_coverage({"php": 3}, evidence, {"scanned_files": 3, "skipped_files": 0},
                                  evidence_reasons={"localization": "No translation files were found."})
    assert all(coverage[c]["supported"] for c in categories.SOURCE_CATEGORIES)
    assert coverage["testing"] == {"supported": True, "status": "covered", "evidence_count": 1, "reason": None}
    assert coverage["localization"]["supported"] is False and coverage["localization"]["reason"] == "No translation files were found."
    assert coverage["process"]["reason"] == "No process evidence was found in this revision."


def test_model_scores_only_categories_with_evidence(monkeypatch):
    import runtime.llm as llm

    evidence = [{"evidence_id": f"ev_{c}", "category": c, "kind": "finding", "rule": "r", "file": "a.php", "line": 1,
                 "message": "Evidence."} for c in (*categories.SOURCE_CATEGORIES, "testing")]
    coverage = _category_coverage({"php": 1}, evidence, {"scanned_files": 1, "skipped_files": 0})
    prompts = []

    def complete(prompt, **_kwargs):
        prompts.append(prompt)
        scored = (*categories.SOURCE_CATEGORIES, "testing")
        return json.dumps({"categories": {c: {"assessed": True, "score": 70, "reason": "Cited.", "evidence_ids": [f"ev_{c}"]}
                                          for c in scored}, "clean_code_findings": []}), "m", "local"

    monkeypatch.setattr(llm, "complete_with_model", complete)
    monkeypatch.setattr(llm, "remote_configured", lambda: False)
    result, *_ = _score(evidence, coverage, {"php": 1}, {"scanned_files": 1, "skipped_files": 0,
                                                          "unsupported_extensions": {}}, [])
    assert set(result) == {*categories.SOURCE_CATEGORIES, "testing"}
    assert "Score these 6 software quality categories" in prompts[0] and '"testing":{' in prompts[0]
    assert "localization" not in prompts[0].split("Evidence:")[0]
