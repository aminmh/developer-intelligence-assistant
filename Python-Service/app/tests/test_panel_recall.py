"""The offline panel-recall harness and its gold set (gap-closing plan, tasks 27-28)."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GOLD = ROOT / "app/tests/fixtures/panel_gold/skiplanner-09c17755.json"


def _harness():
    spec = importlib.util.spec_from_file_location("panel_recall", ROOT / "scripts/panel_recall.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gold_set_is_well_formed():
    gold = json.loads(GOLD.read_text())
    categories = {"security", "clean_code", "maintainability", "reliability", "performance",
                  "process", "testing", "documentation", "localization", "accessibility"}
    assert set(gold["panel_finals"]) == categories and gold["commit"] == "09c177555d9bcd40a9ea490dc44abb2ad39fac75"
    ids = [item["id"] for item in gold["findings"]]
    assert len(ids) == len(set(ids)) == 45
    for item in gold["findings"]:
        assert item["category"] in categories and item["claim"]
        assert item["scope"] in ("line", "file", "revision", "summary") or item["scope"].startswith("rule:")
        if item["scope"] == "line":
            assert item["file"] and item["line"] >= 1
    required = {item["id"] for item in gold["findings"] if item["deterministic"]}
    assert required == {"P1", "R1", "S1", "S5"}  # itemsPerPage and Sentry are left to the review loop (user, 2026-11-16)


def test_matching_rules():
    harness = _harness()
    line = {"id": "x", "category": "reliability", "scope": "line", "file": "src/A.php", "line": 70, "tolerance": 3,
            "also_categories": ["security"], "deterministic": True, "claim": "c"}
    assert harness.matches(line, {"file": "src/A.php", "line": 72, "category": "security", "rule": "r"})
    assert not harness.matches(line, {"file": "src/A.php", "line": 74, "category": "security", "rule": "r"})
    assert not harness.matches(line, {"file": "src/A.php", "line": 70, "category": "performance", "rule": "r"})
    assert not harness.matches(line, {"file": "src/A.php", "line": 70, "category": "reliability", "rule": "process-inventory"})
    by_rule = {**line, "scope": "rule:route-without-authorization:src/Controller/Tenant/"}
    assert harness.matches(by_rule, {"file": "src/Controller/Tenant/X.php", "line": 1, "rule": "route-without-authorization"})
    assert not harness.matches(by_rule, {"file": "src/Controller/Auth/X.php", "line": 1, "rule": "route-without-authorization"})
    summary = {**line, "scope": "summary", "summary": "route-authorization.access_rule_only"}
    assert harness.matches(summary, {"rule": "summary:route-authorization.access_rule_only", "value": 3})
    result = harness.score({"findings": [line, {**line, "id": "y", "deterministic": False}]},
                           [{"file": "src/A.php", "line": 70, "category": "reliability", "rule": "r", "stage": "semgrep"}])
    assert result["found"] == 2 and result["deterministic_found"] == 1 and result["by_category"] == {"reliability": "2/2"}


def test_a_saved_report_is_scored_with_review_ranges_and_route_counts():
    harness = _harness()
    report = {"findings": [{"file": "src/A.php", "line": 60, "end_line": 72, "category": "reliability",
                            "rule": "ai-review-reliability", "tool": "ai-category-review"}],
              "coverage": {"analyzers": [{"name": "framework-checks", "checks": [
                  {"name": "route-authorization", "summary": {"access_rule_only": 129, "public": 0}}]}]}}
    rows = harness.report_findings(report)
    gold = {"id": "x", "category": "reliability", "scope": "line", "file": "src/A.php", "line": 71, "tolerance": 0,
            "deterministic": False, "claim": "c"}
    assert harness.matches(gold, rows[0])  # line 71 lies within the cited range 60-72
    assert harness.matches({**gold, "scope": "summary", "summary": "route-authorization.access_rule_only"}, rows[1])
    assert len(rows) == 2  # zero counts are not evidence
