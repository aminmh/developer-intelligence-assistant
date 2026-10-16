import json

import pytest

import runtime.llm as llm
from code_quality.service import CATEGORIES, _score


@pytest.mark.parametrize(
    ("remote_available", "expected_provider"),
    [(True, "remote"), (False, "local")],
)
def test_score_uses_configured_provider(monkeypatch, remote_available, expected_provider):
    selected = {}
    response = {
        "categories": {
            category: {
                "assessed": False,
                "score": None,
                "reason": "Not enough evidence.",
                "evidence_ids": [],
            }
            for category in CATEGORIES
        }
    }

    def complete_with_model(_prompt, **kwargs):
        selected["provider"] = kwargs["provider"]
        return json.dumps(response), "test-model", kwargs["provider"]

    monkeypatch.setattr(llm, "remote_configured", lambda: remote_available)
    monkeypatch.setattr(llm, "complete_with_model", complete_with_model)

    _score(
        evidence=[],
        coverage={category: {"supported": True, "reason": None} for category in CATEGORIES},
        languages={"php": 1},
        scan_stats={"scanned_files": 1, "unsupported_extensions": {}},
        analyzer_failures=[],
    )

    assert selected["provider"] == expected_provider
