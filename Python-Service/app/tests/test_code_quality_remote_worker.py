from __future__ import annotations

import base64
import hashlib
import io
import sys
import zipfile
from uuid import uuid4

import pytest

from code_quality import remote_worker
from code_quality.external_analyzers import ExternalAnalyzerError, effective_worker_target, normalize_settings


def _archive(entries: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return output.getvalue()


def test_remote_bundle_contains_only_inventoried_source_files():
    original = _archive({"src/app.py": "first\nsecond\n", "vendor/copied.py": "dependency\n"})

    bundle, line_counts = remote_worker._filtered_source_bundle(original, ["src/app.py"])

    with zipfile.ZipFile(io.BytesIO(bundle)) as result:
        assert result.namelist() == ["src/app.py"]
        assert result.read("src/app.py") == b"first\nsecond\n"
    assert line_counts == {"src/app.py": 3}


def test_remote_bundle_rejects_missing_inventory_entries():
    with pytest.raises(remote_worker.RemoteWorkerError, match="inventory"):
        remote_worker._filtered_source_bundle(_archive({"src/app.py": "ok\n"}), ["src/missing.py"])


def test_developer_worker_target_requires_explicit_consent(monkeypatch):
    monkeypatch.setenv("APP_DEPLOYMENT_MODE", "developer")
    settings = normalize_settings({"worker": {
        "enabled": False,
        "endpoint": "https://worker.example.test",
        "certificate_sha256": "a" * 64,
        "source_transfer_consent": False,
    }})

    assert effective_worker_target(settings) is None


def test_developer_mode_toggle_disables_and_enables_project_remote_worker(monkeypatch):
    monkeypatch.setenv("APP_DEPLOYMENT_MODE", "developer")
    base = {"endpoint": "https://worker.example.test", "certificate_sha256": "a" * 64,
            "source_transfer_consent": True}

    disabled = normalize_settings({"worker": {**base, "enabled": False}})
    enabled = normalize_settings({"worker": {**base, "enabled": True}})

    assert effective_worker_target(disabled) is None
    assert effective_worker_target(enabled) is not None


def test_production_worker_target_is_deployment_owned(monkeypatch):
    monkeypatch.setenv("APP_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("ANALYZER_WORKER_URL", "https://analyzer-worker:9443")
    monkeypatch.setenv("ANALYZER_WORKER_CERT_SHA256", "b" * 64)

    assert effective_worker_target({}) == {
        "enabled": True,
        "endpoint": "https://analyzer-worker:9443",
        "certificate_sha256": "b" * 64,
        "source_transfer_consent": True,
        "deployment_owned": True,
    }


def test_production_rejects_project_remote_worker_settings(monkeypatch):
    monkeypatch.setenv("APP_DEPLOYMENT_MODE", "production")
    with pytest.raises(ExternalAnalyzerError, match="Production Mode"):
        normalize_settings({"worker": {"endpoint": "https://worker.example.test"}})


def test_worker_findings_must_reference_selected_source_and_valid_lines():
    finding = {"rule": "joern:unsafe-call", "category": "security", "severity": "warning",
               "file": "src/app.py", "line": 2, "message": "Review the call.", "tool": "joern",
               "version": "4.0.664"}
    output = {"name": "joern", "version": "4.0.664", "status": "complete", "findings": [finding],
              "scanned_files": 1, "skipped_files": 0}

    normalized = remote_worker._validate_result("joern", output, {"src/app.py"}, {"src/app.py": 2})
    assert normalized["findings"] == [finding]

    finding["line"] = 3
    with pytest.raises(remote_worker.RemoteWorkerError, match="out-of-revision"):
        remote_worker._validate_result("joern", output, {"src/app.py"}, {"src/app.py": 2})


def test_worker_job_payload_binds_source_digest_and_revision(monkeypatch):
    app_path = str(__import__("pathlib").Path(__file__).parents[3] / "analyzer-worker")
    api_path = str(__import__("pathlib").Path(__file__).parents[1])
    monkeypatch.syspath_prepend(app_path)
    monkeypatch.syspath_prepend(api_path)
    import worker

    archive = _archive({"src/app.py": "print('ok')\n"})
    payload = {
        "schema_version": 1,
        "job_id": str(uuid4()),
        "revision_id": "rev_" + "d" * 32,
        "commit_sha": "c" * 40,
        "archive_base64": base64.b64encode(archive).decode("ascii"),
        "bundle_sha256": hashlib.sha256(archive).hexdigest(),
        "files": ["src/app.py"],
        "file_line_counts": {"src/app.py": 2},
        "languages": {"python": 1},
        "analyzers": ["joern"],
        "settings": {"sonarqube": {}, "joern": {"enabled": True}},
        "sonarqube_token": None,
    }

    monkeypatch.setattr(worker, "run_joern", lambda *_args: {
        "name": "joern", "version": "test", "status": "complete", "findings": [],
        "scanned_files": 1, "skipped_files": 0,
    })
    result = worker._run_job(payload)

    assert result["job_id"] == payload["job_id"]
    assert result["revision_id"] == payload["revision_id"]
    assert result["commit_sha"] == payload["commit_sha"]
    assert result["bundle_sha256"] == payload["bundle_sha256"]
    assert result["analyzers"]["joern"]["status"] == "complete"


def test_worker_rejects_uuid_revision_id_instead_of_app_revision_format(monkeypatch):
    app_path = str(__import__("pathlib").Path(__file__).parents[3] / "analyzer-worker")
    api_path = str(__import__("pathlib").Path(__file__).parents[1])
    monkeypatch.syspath_prepend(app_path)
    monkeypatch.syspath_prepend(api_path)
    import worker
    from fastapi import HTTPException

    archive = _archive({"src/app.py": "print('ok')\n"})
    payload = {
        "schema_version": 1,
        "job_id": str(uuid4()),
        "revision_id": str(uuid4()),
        "commit_sha": "c" * 40,
        "archive_base64": base64.b64encode(archive).decode("ascii"),
        "bundle_sha256": hashlib.sha256(archive).hexdigest(),
        "files": ["src/app.py"],
        "file_line_counts": {"src/app.py": 2},
        "languages": {"python": 1},
        "analyzers": ["joern"],
        "settings": {"sonarqube": {}, "joern": {"enabled": True}},
        "sonarqube_token": None,
    }
    with pytest.raises(HTTPException, match="Analyzer job revision identity is invalid"):
        worker._validate_job(payload)

    payload["files"] = ["src/app.py", "vendor/hidden.py"]
    with pytest.raises(worker.HTTPException):
        worker._validate_job(payload)
