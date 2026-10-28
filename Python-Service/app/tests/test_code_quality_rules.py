from pathlib import Path
from types import SimpleNamespace

import pytest

import code_quality.analyzers as analyzers
from code_quality.analyzers import _normalize_results
from code_quality.rules import CustomRuleStore, validate_rule_yaml
from code_quality.service import AssessmentError


VALID_RULE = """rules:
  - id: unsafe-query
    message: Avoid executing untrusted input.
    severity: WARNING
    languages: [python]
    pattern: $DB.execute($INPUT)
    metadata:
      category: security
      confidence: high
"""


def test_validate_rule_yaml_accepts_supported_rule():
    rules = validate_rule_yaml(VALID_RULE)

    assert len(rules) == 1
    assert rules[0]["id"] == "unsafe-query"


def test_validate_semgrep_yaml_uses_local_scan_without_remote_validation(monkeypatch):
    captured = {}
    monkeypatch.setattr(analyzers.shutil, "which", lambda _name: "/usr/local/bin/semgrep")

    def scan(command, **_kwargs):
        captured["command"] = command
        source = Path(command[-1])
        assert (source / "validation.py").is_file()
        return SimpleNamespace(stdout=b'{"errors": []}', stderr=b"", returncode=0)

    monkeypatch.setattr(analyzers.subprocess, "run", scan)

    analyzers.validate_semgrep_yaml(VALID_RULE)

    assert captured["command"][1] == "scan"
    assert "--validate" not in captured["command"]


def test_validate_semgrep_yaml_rejects_scan_parse_errors(monkeypatch):
    monkeypatch.setattr(analyzers.shutil, "which", lambda _name: "/usr/local/bin/semgrep")
    monkeypatch.setattr(
        analyzers.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=b'{"errors": [{"message": "invalid"}]}', stderr=b"", returncode=0),
    )

    with pytest.raises(AssessmentError, match="Semgrep rejected the rule syntax"):
        analyzers.validate_semgrep_yaml(VALID_RULE)


@pytest.mark.parametrize(
    "document",
    [
        "rules: []",
        VALID_RULE.replace("[python]", "[unknown-language]"),
        VALID_RULE.replace("category: security", "category: unknown"),
        VALID_RULE.replace("    pattern:", "    unsupported-field: true\n    pattern:"),
    ],
)
def test_validate_rule_yaml_rejects_invalid_rule(document):
    with pytest.raises(AssessmentError):
        validate_rule_yaml(document)


def test_custom_rule_store_isolates_users_and_preserves_enabled_state(tmp_path):
    store = CustomRuleStore(tmp_path / "rules.db")

    created = store.save("user-a", "Security checks", VALID_RULE)

    assert created["enabled"] is True
    assert store.list("user-b") == []
    assert store.set_enabled("user-a", created["rule_id"], False)["enabled"] is False
    assert store.enabled("user-a") == []
    assert store.delete("user-a", created["rule_id"]) is True
    assert store.get("user-a", created["rule_id"]) is None


def test_normalize_results_only_returns_allowed_source_files_and_categories():
    payload = {
        "results": [
            {
                "path": "source/src/query.py",
                "check_id": "custom.unsafe-query",
                "start": {"line": 12},
                "extra": {
                    "message": "Avoid executing untrusted input.",
                    "severity": "WARNING",
                    "metadata": {"category": "security"},
                },
            },
            {
                "path": "source/tests/query_test.py",
                "extra": {"message": "Test finding", "metadata": {"category": "security"}},
            },
            {
                "path": "source/src/other.py",
                "extra": {"message": "Unsupported category", "metadata": {"category": "unknown"}},
            },
        ]
    }

    findings = _normalize_results(payload, {"src/query.py"})

    assert findings == [
        {
            "file": "src/query.py",
            "line": 12,
            "rule": "custom.unsafe-query",
            "category": "security",
            "message": "Avoid executing untrusted input.",
            "severity": "WARNING",
            "kind": "finding",
        }
    ]


def test_builtin_ruleset_omits_the_non_structural_nested_loop_heuristic():
    rules, _, _, _ = analyzers._rule_document([], include_builtins=True)

    assert "builtin-nested-loop" not in rules
    assert analyzers.BUILTIN_RULESET_VERSION == "code-quality-builtins-5"
