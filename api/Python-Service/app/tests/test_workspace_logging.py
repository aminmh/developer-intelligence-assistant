from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def test_workspace_prepare_failure_is_preserved(monkeypatch):
    from pipelines.orchestrator import Orchestrator

    runner = Orchestrator.__new__(Orchestrator)
    monkeypatch.setattr(
        runner,
        "_call",
        lambda *args, **kwargs: {"detail": "File not found: missing.zip"},
    )
    ctx = SimpleNamespace(job_id="job-1", filename="missing.zip", upload_session_id=None)

    with pytest.raises(RuntimeError, match="Workspace preparation failed for missing.zip") as error:
        runner._prepare_workspace(ctx)

    assert "File not found: missing.zip" in str(error.value)


def test_local_default_log_dir_is_repository_canonical_path():
    from infra.settings import settings

    expected = Path(__file__).resolve().parents[2] / "logs"
    assert Path(settings.log_dir).resolve() == expected
