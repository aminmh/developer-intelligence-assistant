import json
from types import SimpleNamespace

import main
from telemetry.snapshot import Snapshot


def _upload():
    return {"project_id": "p1", "commit_sha": None, "archive_sha256": "a" * 64}


def test_index_job_returns_newest_status_for_the_upload(tmp_path, monkeypatch):
    collection = Snapshot("p1", None, "a" * 64).collection
    (tmp_path / "old.status.json").write_text(json.dumps({"status": "error", "collection_name": collection}))
    (tmp_path / "other.status.json").write_text(json.dumps({"status": "embedding", "collection_name": "x"}))
    new = tmp_path / "new.status.json"
    new.write_text(json.dumps({"status": "embedding", "total": 10, "indexed": 4, "collection_name": collection}))
    import os, time
    os.utime(tmp_path / "old.status.json", (time.time() - 100,) * 2)
    monkeypatch.setattr(main.settings, "download_dir", str(tmp_path))
    import index.storage as storage_module
    monkeypatch.setattr(storage_module.storage, "find_rag_file", lambda *a: _upload())

    job = main.rag_index_job("a.zip", "s1")

    assert job["download_id"] == "new" and job["indexed"] == 4


def test_index_job_is_none_without_a_job(tmp_path, monkeypatch):
    monkeypatch.setattr(main.settings, "download_dir", str(tmp_path))
    import index.storage as storage_module
    monkeypatch.setattr(storage_module.storage, "find_rag_file", lambda *a: _upload())

    assert main.rag_index_job("a.zip", "s1")["status"] == "none"


def test_revision_progress_comes_from_the_upload_status_file(tmp_path, monkeypatch):
    from index import job_status
    monkeypatch.setattr(job_status.settings, "download_dir", str(tmp_path))
    (tmp_path / "j1.status.json").write_text(json.dumps(
        {"status": "embedding", "total": 200, "indexed": 50, "collection_name": "snapshot_abc"}))
    found = job_status.latest_for_collection("snapshot_abc")
    assert found["download_id"] == "j1" and found["indexed"] == 50


def test_silent_job_is_reported_interrupted(tmp_path, monkeypatch):
    import os, time
    from index import job_status
    monkeypatch.setattr(job_status.settings, "download_dir", str(tmp_path))
    path = tmp_path / "j2.status.json"
    path.write_text(json.dumps({"status": "embedding", "total": 9, "indexed": 3, "collection_name": "snapshot_z"}))
    os.utime(path, (time.time() - 3600,) * 2)
    assert job_status.latest_for_collection("snapshot_z")["status"] == "error"
