"""Tests for the bounded workspace retention registry (cons F17)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import projects.workspace_registry as reg


@pytest.fixture(autouse=True)
def _registry_path(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_REGISTRY_PATH", str(tmp_path / "reg.json"))
    yield


def _dirs(tmp_path, *names) -> list[Path]:
    out = []
    for n in names:
        d = tmp_path / n
        d.mkdir()
        (d / "composer.json").write_text("{}", encoding="utf-8")
        out.append(d)
    return out


class TestWorkspaceRegistry:
    def test_register_and_list_newest_first(self, tmp_path):
        a, b = _dirs(tmp_path, "ws-a", "ws-b")
        reg.register(str(a), "one.zip")
        reg.register(str(b), "two.zip")
        entries = reg.list_workspaces()
        assert [e["id"] for e in entries] == ["ws-b", "ws-a"]
        assert entries[0]["filename"] == "two.zip"
        assert os.path.isdir(entries[1]["path"])

    def test_register_skips_missing_dir(self, tmp_path):
        assert reg.register(str(tmp_path / "ghost"), "x.zip") is None
        assert reg.list_workspaces() == []

    def test_cap_evicts_oldest_on_disk(self, tmp_path):
        dirs = _dirs(tmp_path, "a1", "a2", "a3", "a4")
        for i, d in enumerate(dirs, start=1):
            reg.register(str(d), f"p{i}.zip")
        left = reg.list_workspaces()
        assert [e["id"] for e in left] == ["a4", "a3", "a2"]
        assert not (tmp_path / "a1").exists()  # evicted from disk

    def test_register_same_path_moves_to_front(self, tmp_path):
        a, b = _dirs(tmp_path, "dup-a", "dup-b")
        reg.register(str(a), "x.zip")
        reg.register(str(b), "y.zip")
        reg.register(str(a), "x.zip")
        entries = reg.list_workspaces()
        assert [e["id"] for e in entries] == ["dup-a", "dup-b"]
        assert entries[0]["filename"] == "x.zip"

    def test_forget_keeps_filesystem(self, tmp_path):
        (d,) = _dirs(tmp_path, "gone")
        reg.register(str(d), "x.zip")
        assert reg.forget("gone") is True
        assert reg.list_workspaces() == []
        assert (tmp_path / "gone").exists()  # caller decides removal
        assert reg.forget("gone") is False

    def test_registry_survives_restart(self, tmp_path):
        (d,) = _dirs(tmp_path, "persist")
        reg.register(str(d), "keep.zip")
        # new process would re-read the same file
        entries = reg._load()
        assert entries[0]["filename"] == "keep.zip"
