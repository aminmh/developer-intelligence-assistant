from __future__ import annotations

import json

from infra import log_viewer


def test_list_logs_parses_all_formats_and_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(log_viewer.settings, "log_dir", str(tmp_path))
    (tmp_path / "llm-prompts.log").write_text(
        "[2026-09-08T10:00:00+00:00]\nUSER_QUERY: explain auth\nFINAL_PROMPT:\nSystem\nCode\n"
        + "-" * 120 + "\n",
        encoding="utf-8",
    )
    (tmp_path / "exceptions.log").write_text(
        "[2026-09-08T10:01:00] ERROR MainThread: boom\nTraceback\nValueError: bad\n",
        encoding="utf-8",
    )
    (tmp_path / "exceptions.log.1").write_text(
        "[2026-09-08T09:00:00] WARNING Worker: old\n", encoding="utf-8"
    )
    (tmp_path / "router-fallback.log").write_text(
        json.dumps({"timestamp": "2026-09-08T10:02:00Z", "query": "route this", "final_choice": "search"})
        + "\nnot-json\n",
        encoding="utf-8",
    )

    result = log_viewer.list_logs(search="auth")
    assert result["total"] == 1
    assert result["events"][0]["type"] == "llm_prompt"
    assert result["events"][0]["details"]["prompt"] == "System\nCode"
    assert log_viewer.list_logs(severity="warning")["total"] == 3


def test_soft_and_hard_clear_modes(tmp_path, monkeypatch):
    monkeypatch.setattr(log_viewer.settings, "log_dir", str(tmp_path))
    for name in ("exceptions.log", "exceptions.log.1", "llm-prompts.log"):
        (tmp_path / name).write_text("data", encoding="utf-8")
    result = log_viewer.clear_logs("exception", "soft")
    assert result["files"] == ["exceptions.log"]
    assert (tmp_path / "exceptions.log.1").read_text(encoding="utf-8") == "data"
    result = log_viewer.clear_logs("exception", "hard")
    assert "exceptions.log" in result["files"]
    assert not (tmp_path / "exceptions.log").exists()
    assert not (tmp_path / "exceptions.log.1").exists()
