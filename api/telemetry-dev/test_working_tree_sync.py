"""Desktop helper working-tree sync: packaging and the poll/upload cycle."""

import hashlib
import subprocess
import urllib.error

import pytest

import working_tree_sync as sync


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def make_folder(tmp_path, git=True):
    folder = tmp_path / "project"
    (folder / "src").mkdir(parents=True)
    (folder / "src" / "app.py").write_text("print(1)\n")
    (folder / ".env").write_text("SECRET=1\n")
    (folder / "config").mkdir()
    (folder / "config" / "service-account.json").write_text("{}")
    (folder / "node_modules" / "x").mkdir(parents=True)
    (folder / "node_modules" / "x" / "i.js").write_text("x")
    (folder / "ignored.log").write_text("log")
    if git:
        (folder / ".gitignore").write_text("*.log\nnode_modules/\n")
        subprocess.run(["git", "init", "-q", str(folder)], check=True)
    return folder


@pytest.mark.parametrize("git", [True, False])
def test_packaging_skips_secrets_dependencies_and_ignored_files(tmp_path, git):
    folder = make_folder(tmp_path, git)
    names = sync.list_files(folder)
    assert "src/app.py" in names
    assert not {".env", "config/service-account.json", "node_modules/x/i.js"} & set(names)
    if git:
        assert "ignored.log" not in names and ".gitignore" in names


def test_packaging_is_deterministic(tmp_path):
    folder = make_folder(tmp_path)
    names = sync.list_files(folder)
    first = sync.package(folder, names)
    (folder / "src" / "app.py").touch()  # a new mtime alone changes nothing in the archive
    assert sync.package(folder, names) == first


def test_poll_waits_for_quiet_uploads_once_and_stops_when_revoked(tmp_path, monkeypatch):
    folder = make_folder(tmp_path)
    sync.write_grants([{"grant_id": "wtg_" + "a" * 32, "project_id": "shop", "folder": str(folder),
                        "web_origin": "http://localhost:3000"}])
    uploads = []
    monkeypatch.setattr(sync, "upload", lambda grant, payload, commit: uploads.append(payload) or
                        {"revision": {"revision_id": "rev_" + "1" * 32}})
    grant = lambda: sync.read_grants()[0]  # noqa: E731

    assert sync.sync_once(grant(), now=1000) == "changed"
    assert sync.sync_once(grant(), now=1010) == "settling"
    assert sync.sync_once(grant(), now=1031) == "uploaded" and len(uploads) == 1
    assert grant()["uploaded_archive_sha256"] == hashlib.sha256(uploads[0]).hexdigest()
    assert sync.sync_once(grant(), now=1100) == "unchanged"

    (folder / "src" / "app.py").write_text("print(2)\n")
    assert sync.sync_once(grant(), now=1110) == "changed"
    assert sync.sync_once(grant(), now=1150) == "waiting"  # 3 minutes between uploads
    assert sync.sync_once(grant(), now=1300) == "uploaded" and len(uploads) == 2

    def revoked(grant, payload, commit):
        raise urllib.error.HTTPError("u", 401, "revoked", {}, None)

    monkeypatch.setattr(sync, "upload", revoked)
    monkeypatch.setattr(sync, "remove_token", lambda grant_id: None)
    (folder / "src" / "app.py").write_text("print(3)\n")
    sync.sync_once(grant(), now=2000)
    assert sync.sync_once(grant(), now=2100) == "revoked" and grant()["status"] == "revoked"
