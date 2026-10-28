from __future__ import annotations

import sys
import zipfile
from urllib.error import HTTPError
from uuid import uuid4

import pytest

from code_quality.external_analyzers import (
    ExternalAnalyzerError,
    ExternalAnalyzerSettingsStore,
    _sonar_category,
    _sonar_json,
    normalize_settings,
    run_joern,
    run_sonarqube,
)


@pytest.mark.parametrize(("status", "expected"), [
    (401, "valid and not expired"),
    (403, "User token"),
])
def test_sonarqube_http_errors_explain_credential_or_project_permission(status, expected, monkeypatch):
    class ForbiddenOpener:
        def open(self, _request, timeout):
            raise HTTPError("https://sonar.example.test/api/test", status, "Denied", {}, None)

    monkeypatch.setattr("code_quality.external_analyzers.build_opener", lambda _handler: ForbiddenOpener())

    with pytest.raises(ExternalAnalyzerError, match=expected):
        _sonar_json("https://sonar.example.test", "secret-token", "project_analyses/search", {})


def test_external_analyzer_settings_require_source_upload_consent_for_sonar():
    with pytest.raises(ExternalAnalyzerError, match="consent"):
        normalize_settings({"sonarqube": {
            "enabled": True,
            "server_url": "https://sonar.example.test",
            "project_key": "pizza",
            "source_upload_consent": False,
        }})


def test_external_analyzer_store_keeps_only_non_secret_configuration(tmp_path):
    path = tmp_path / "quality.sqlite"
    store = ExternalAnalyzerSettingsStore(path)
    credential_id = str(uuid4())
    saved = store.save("pizza", {"sonarqube": {
        "enabled": True,
        "server_url": "https://sonar.example.test",
        "project_key": "pizza",
        "source_upload_consent": True,
        "credential_id": credential_id,
        "token": "must-not-be-persisted",
    }, "joern": {"enabled": True}})

    assert store.get("pizza")["sonarqube"]["credential_id"] == credential_id
    assert "must-not-be-persisted" not in str(saved)
    assert b"must-not-be-persisted" not in path.read_bytes()


def test_sonarqube_requires_explicit_upload_consent_before_running():
    result = run_sonarqube({"sonarqube": {"enabled": True, "source_upload_consent": False}}, b"archive", [], "token")

    assert result["status"] == "unavailable"
    assert "consent" in result["reason"].lower()
    assert result["findings"] == []


def test_sonarqube_normalizes_only_the_completed_revision_analysis(tmp_path, monkeypatch):
    scanner = tmp_path / "sonar-scanner"
    scanner.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        "if '--version' in sys.argv: print('SonarScanner CLI 7.2.0'); raise SystemExit(0)\n"
        "import os\n"
        "assert os.environ.get('SONAR_SCANNER_SKIP_JRE_PROVISIONING') == 'true'\n"
        "assert '-Dsonar.nodejs.executable=/usr/local/bin/node' in sys.argv\n"
        "assert '-Dsonar.scanner.skipNodeProvisioning=true' in sys.argv\n"
        "pathlib.Path('.scannerwork').mkdir()\n"
        "pathlib.Path('.scannerwork/report-task.txt').write_text('serverUrl=https://sonar.example.test\\nceTaskId=task-1\\n')\n",
        encoding="utf-8",
    )
    scanner.chmod(0o755)
    monkeypatch.setattr("code_quality.external_analyzers.shutil.which",
                        lambda name: str(scanner) if name == "sonar-scanner" else None)
    monkeypatch.setattr("code_quality.external_analyzers._sonar_server_version", lambda _server, _token: "10.8.0")

    def sonar_json(_server, _token, path, query):
        if path == "languages/list":
            return {"languages": [{"key": "py"}]}
        if path == "ce/task":
            return {"task": {"status": "SUCCESS", "componentKey": "pizza", "analysisId": "analysis-1"}}
        if path == "project_analyses/search":
            return {"analyses": [{"key": "analysis-1"}]}
        if path == "issues/search":
            return {"issues": [{"type": "VULNERABILITY", "component": "pizza:src/app.py",
                                "line": 3, "message": "Review input validation", "rule": "py:S123",
                                "severity": "CRITICAL"}]}
        raise AssertionError((path, query))

    monkeypatch.setattr("code_quality.external_analyzers._sonar_json", sonar_json)
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as source:
        source.writestr("src/app.py", "def run(): pass\n")
    settings = {"sonarqube": {"enabled": True, "server_url": "https://sonar.example.test",
                              "project_key": "pizza", "source_upload_consent": True}}

    result = run_sonarqube(settings, archive.read_bytes(), ["src/app.py"], "temporary-token")

    assert result["status"] == "complete"
    assert result["version"] == "7.2.0"
    assert result["supported_languages"] == ["python"]
    assert result["findings"] == [{"rule": "sonarqube:py:S123", "category": "security",
                                    "severity": "critical", "file": "src/app.py", "line": 3,
                                    "message": "Review input validation", "tool": "sonarqube",
                                    "version": "7.2.0"}]


def test_sonarqube_failure_includes_bounded_redacted_scanner_diagnostic(tmp_path, monkeypatch):
    scanner = tmp_path / "sonar-scanner"
    scanner.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "if '--version' in sys.argv: print('SonarScanner CLI 8.1.0'); raise SystemExit(0)\n"
        "print('ERROR: Not authorized; token=' + os.environ['SONAR_TOKEN'], file=sys.stderr)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    scanner.chmod(0o755)
    monkeypatch.setattr("code_quality.external_analyzers.shutil.which",
                        lambda name: str(scanner) if name == "sonar-scanner" else None)
    monkeypatch.setattr("code_quality.external_analyzers._sonar_server_version", lambda _server, _token: "26.9.0")
    monkeypatch.setattr("code_quality.external_analyzers._sonar_json",
                        lambda _server, _token, path, _query: {"languages": [{"key": "py"}]}
                        if path == "languages/list" else {})
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as source:
        source.writestr("src/app.py", "def run(): pass\n")
    settings = {"sonarqube": {"enabled": True, "server_url": "https://sonar.example.test",
                              "project_key": "pizza", "source_upload_consent": True}}

    result = run_sonarqube(settings, archive.read_bytes(), ["src/app.py"], "secret-token")

    assert result["status"] == "failed"
    assert "Not authorized" in result["reason"]
    assert "secret-token" not in result["reason"]


def test_disabled_joern_does_not_attempt_to_resolve_or_run_tool():
    result = run_joern({"joern": {"enabled": False}}, b"archive", [], {})

    assert result["status"] == "disabled"
    assert result["findings"] == []


@pytest.mark.parametrize(("issue_type", "expected"), [
    ("VULNERABILITY", "security"),
    ("SECURITY_HOTSPOT", "security"),
    ("BUG", "reliability"),
    ("CODE_SMELL", "maintainability"),
    ("UNKNOWN", None),
])
def test_sonarqube_issue_type_normalizes_to_supported_category(issue_type, expected):
    assert _sonar_category({"type": issue_type}) == expected
