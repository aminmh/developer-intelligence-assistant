from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from telemetry import http
from telemetry.registry import SnapshotConflict, SnapshotRegistry
from telemetry.source import SourceService, validate_archive

SHA = "a" * 40


def archive(code="<?php function example() {}", path="src/example.php"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as out:
        out.writestr(path, code)
    return buffer.getvalue()


class Storage:
    def __init__(self):
        self.writes = []

    def put_snapshot_archive(self, *args):
        self.writes.append(args)
        return "/".join(args[:2])


def successful_index(workspace, snapshot):
    assert Path(workspace, "src/example.php").is_file()
    return {"chunk_count": 1, "ingested": 1, "graph_nodes": 1}


def test_source_lifecycle_retry_conflict_and_isolation(tmp_path):
    registry = SnapshotRegistry(tmp_path)
    storage = Storage()
    service = SourceService(registry, storage, successful_index)
    assert registry.get("one", SHA)["state"] == "awaiting_source"
    data = archive()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.upload("one", SHA, data), range(2)))
    assert all(r["state"] == "ready" for r in results)
    assert len(storage.writes) == 1
    with pytest.raises(SnapshotConflict):
        service.upload("one", SHA, archive("<?php function changed() {}"))
    assert registry.get("one", SHA) == results[0]
    collections = {service.upload(p, c, data)["collection_name"]
                   for p in ("one", "two") for c in (SHA, "b" * 40)}
    assert len(collections) == 4
    assert len(storage.writes) == 4
    assert not list(tmp_path.glob("source_*"))
    assert SnapshotRegistry(tmp_path).get("one", SHA)["state"] == "ready"


@pytest.mark.parametrize("result", [None, {"chunk_count": 2, "ingested": 1, "graph_nodes": 2},
                                      {"chunk_count": 1, "ingested": 1, "graph_nodes": 0}])
def test_failed_indexing_never_publishes_ready_and_retries(tmp_path, result):
    registry = SnapshotRegistry(tmp_path)
    def fail(*args):
        if result is None:
            raise RuntimeError("SECRET_CANARY_SOURCE")
        return result
    service = SourceService(registry, Storage(), fail)
    data = archive()
    state = service.upload("one", SHA, data)
    assert state["state"] == "failed"
    assert "collection_name" not in state
    assert "SECRET_CANARY_SOURCE" not in json.dumps(state)
    service.indexer = successful_index
    assert service.upload("one", SHA, data)["state"] == "ready"


@pytest.mark.parametrize("path", ["../escape.php", "/absolute.php", "a/../x.php", "a\\b.php", "./x.php"])
def test_unsafe_archive_paths_rejected(path):
    with pytest.raises(ValueError):
        validate_archive(archive(path=path))


def test_project_authentication_and_status(tmp_path, monkeypatch):
    registry = SnapshotRegistry(tmp_path)
    monkeypatch.setattr(http, "get_registry", lambda: registry)
    monkeypatch.setenv("TELEMETRY_CI_PROJECT_TOKENS", json.dumps({"one": "1" * 32, "two": "2" * 32}))
    app = FastAPI()
    app.include_router(http.router)
    client = TestClient(app)
    url = f"/telemetry/projects/one/snapshots/{SHA}"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"authorization": "Bearer " + "2" * 32}).status_code == 401
    headers = {"authorization": "Bearer " + "1" * 32}
    assert client.get(url, headers=headers).json()["state"] == "awaiting_source"
    assert client.put(url + "/source", headers=headers, content=archive()).status_code == 415
    assert client.get(url.replace(SHA, "short"), headers=headers).status_code == 422
    monkeypatch.setenv("TELEMETRY_CI_PROJECT_TOKENS", "invalid")
    assert client.get(url, headers=headers).status_code == 401


def test_upload_http_seam(tmp_path, monkeypatch):
    registry = SnapshotRegistry(tmp_path)
    storage = Storage()
    monkeypatch.setattr(http, "get_registry", lambda: registry)
    monkeypatch.setattr(http, "SourceService", lambda *args: SourceService(registry, storage, successful_index))
    monkeypatch.setenv("TELEMETRY_CI_PROJECT_TOKENS", json.dumps({"one": "1" * 32}))
    app = FastAPI()
    app.include_router(http.router)
    client = TestClient(app)
    url = f"/telemetry/projects/one/snapshots/{SHA}/source"
    headers = {"authorization": "Bearer " + "1" * 32, "content-type": "application/zip"}
    assert client.put(url, headers=headers, content=archive()).json()["state"] == "ready"
    assert client.put(url, headers=headers, content=archive("changed")).status_code == 409
    assert client.put(url.replace(SHA, "b" * 40), headers=headers, content=b"invalid").status_code == 422
    monkeypatch.setattr(http, "MAX_ARCHIVE_BYTES", 2)
    assert client.put(url, headers=headers, content=b"abc").status_code == 413
