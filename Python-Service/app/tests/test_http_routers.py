from __future__ import annotations

import pytest
from fastapi import HTTPException

from routers import developer_logs, sandbox, system


def test_sandbox_project_stem_is_safe_and_stable():
    assert sandbox._project_stem("my project.zip") == "my_project"
    assert sandbox._project_stem("../escape.zip") == "escape"
    assert sandbox._project_stem("") == "project"


def test_log_router_rejects_invalid_internal_secret(monkeypatch):
    monkeypatch.setattr(developer_logs.settings, "internal_secret", "expected")
    with pytest.raises(HTTPException) as caught:
        developer_logs._authorize("wrong")
    assert caught.value.status_code == 401


def test_sandbox_command_validates_input_before_constructing_executor(monkeypatch):
    def fail_if_called():
        raise AssertionError("executor should not be constructed")

    monkeypatch.setattr(sandbox, "default_executor", fail_if_called)
    assert sandbox.sandbox_command({}) == {"success": False, "error": "Missing command"}
    assert sandbox.sandbox_command({"command": "x"}) == {
        "success": False,
        "error": "Missing filename",
    }


def test_system_probes_report_unconfigured_optional_services(monkeypatch):
    monkeypatch.setattr(system.settings, "runpod_api_key", "")
    monkeypatch.setattr(system.settings, "llm_remote_base_url", "")
    assert system._probe_runpod()["configured"] is False
    assert system._probe_remote_llm()["configured"] is False
    assert system.runpod_list_endpoints()["ok"] is False
