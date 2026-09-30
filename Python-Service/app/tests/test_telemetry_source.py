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
IMAGE = "sha256:" + "c" * 64


def deploy(registry, project="one", commit=SHA):
    registry.report_deployments("f" * 64, [{"container_id": "d" * 64, "project_id": project,
        "environment": "staging", "service": "web", "commit_sha": commit, "image_id": IMAGE, "state": "running"}])


def test_stale_running_deployment_is_not_reported_as_active(tmp_path):
    registry = SnapshotRegistry(tmp_path)
    deploy(registry, project="skiplanner_2")
    with registry._connect() as db:
        db.execute("UPDATE deployments SET updated_at='2026-09-27 00:00:00' WHERE project_id='skiplanner_2'")
    assert registry.running_deployments("skiplanner_2") == []
    assert all(item["project_id"] != "skiplanner_2" for item in registry.runtime_projects())


def archive(code="<?php function example() {}", path="src/example.php"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as out:
        out.writestr(path, code)
    return buffer.getvalue()


class Storage:
    def __init__(self):
        self.writes = []
        self.data = {}

    def put_snapshot_archive(self, *args):
        self.writes.append(args)
        self.data["/".join(args[:2])] = args[2]
        return "/".join(args[:2])

    def get_snapshot_archive(self, key):
        return self.data[key]

    def delete_snapshot_archive(self, key):
        self.data.pop(key, None)


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
    assert all(r["state"] == "awaiting_deployment" for r in results)
    assert len(storage.writes) == 1
    deploy(registry)
    assert registry.queue_if_deployable("one", SHA)
    assert service.process_one()
    assert registry.get("one", SHA)["state"] == "ready"
    with pytest.raises(SnapshotConflict):
        service.upload("one", SHA, archive("<?php function changed() {}"))
    assert all(r["state"] == "awaiting_deployment" for r in results)
    keys = [(p,c) for p in ("one", "two") for c in (SHA, "b" * 40) if (p,c) != ("one",SHA)]
    for p,c in keys:
        deploy(registry, p, c)
        service.upload(p,c,data)
        registry.queue_if_deployable(p,c)
        assert service.process_one()
    collections = {registry.get(p,c)["collection_name"] for p,c in keys}
    assert len(collections) == 3
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
    service.upload("one", SHA, data)
    deploy(registry)
    registry.queue_if_deployable("one", SHA)
    assert service.process_one()
    state = registry.get("one", SHA)
    assert state["state"] == "failed"
    assert "collection_name" not in state
    assert "SECRET_CANARY_SOURCE" not in json.dumps(state)
    service.indexer = successful_index
    service.upload("one", SHA, data)
    assert registry.get("one", SHA)["state"] == "queued"
    assert service.process_one()
    assert registry.get("one", SHA)["state"] == "ready"


def test_unmatched_archive_expires_but_checksum_and_exact_retry_survive(tmp_path):
    registry = SnapshotRegistry(tmp_path)
    storage = Storage()
    service = SourceService(registry, storage, successful_index)
    data = archive()
    pending = service.upload("one", SHA, data)
    assert pending["state"] == "awaiting_deployment"
    with registry._connect() as db:
        db.execute("UPDATE snapshots SET updated_at='2000-01-01 00:00:00' WHERE project_id='one'")
    service.expire_unmatched()
    after = registry.get("one", SHA)
    assert after["state"] == "awaiting_source"
    assert after["source_archive_sha256"] == pending["source_archive_sha256"]
    assert registry.archive_key("one", SHA) is None
    with pytest.raises(SnapshotConflict):
        service.upload("one", SHA, archive("different"))
    assert service.upload("one", SHA, data)["state"] == "awaiting_deployment"
    assert len(storage.writes) == 2


def test_interrupted_worker_job_is_reclaimed_after_process_restart(tmp_path):
    registry = SnapshotRegistry(tmp_path)
    storage = Storage()
    service = SourceService(registry, storage, successful_index)
    deploy(registry)
    service.upload("one", SHA, archive())
    assert registry.queue_if_deployable("one", SHA)
    assert registry.claim_job() == ("one", SHA)  # interrupted worker had claimed it
    restarted = SourceService(registry, storage, successful_index)
    assert restarted.process_one()
    assert registry.get("one", SHA)["state"] == "ready"


def test_worker_reconciles_a_missed_source_deployment_trigger(tmp_path):
    registry = SnapshotRegistry(tmp_path)
    storage = Storage()
    service = SourceService(registry, storage, successful_index)
    service.upload("one", SHA, archive())
    deploy(registry)
    # Simulate both arrival handlers missing their post-commit enqueue trigger.
    assert service.process_one()
    assert registry.get("one", SHA)["state"] == "ready"


@pytest.mark.parametrize("path", ["../escape.php", "/absolute.php", "a/../x.php", "a\\b.php", "./x.php"])
def test_unsafe_archive_paths_rejected(path):
    with pytest.raises(ValueError):
        validate_archive(archive(path=path))


def test_project_authentication_and_status(tmp_path, monkeypatch):
    registry = SnapshotRegistry(tmp_path)
    monkeypatch.setattr(http, "get_registry", lambda: registry)
    monkeypatch.setattr(http, "get_storage", lambda: storage)
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
    monkeypatch.setattr(http, "get_storage", lambda: storage)
    monkeypatch.setattr(http, "SourceService", lambda *args: SourceService(registry, storage, successful_index))
    monkeypatch.setenv("TELEMETRY_CI_PROJECT_TOKENS", json.dumps({"one": "1" * 32}))
    app = FastAPI()
    app.include_router(http.router)
    client = TestClient(app)
    url = f"/telemetry/projects/one/snapshots/{SHA}/source"
    headers = {"authorization": "Bearer " + "1" * 32, "content-type": "application/zip"}
    assert client.put(url, headers=headers, content=archive()).json()["state"] == "awaiting_deployment"
    assert client.put(url, headers=headers, content=archive("changed")).status_code == 409
    assert client.put(url.replace(SHA, "b" * 40), headers=headers, content=b"invalid").status_code == 422
    monkeypatch.setattr(http, "MAX_ARCHIVE_BYTES", 2)
    assert client.put(url, headers=headers, content=b"abc").status_code == 413


def test_deployment_first_queues_source_and_host_identity_is_ingress_bound(tmp_path, monkeypatch):
    registry = SnapshotRegistry(tmp_path)
    storage = Storage()
    monkeypatch.setattr(http, "get_registry", lambda: registry)
    monkeypatch.setattr(http, "get_storage", lambda: storage)
    fingerprint = "f" * 64
    monkeypatch.setenv("TELEMETRY_HOST_SCOPES", json.dumps({fingerprint: {"projects": {"one": ["staging"]}}}))
    app = FastAPI()
    app.include_router(http.router)
    app.include_router(http.host_router)
    client = TestClient(app)
    record = {"container_id": "d" * 64, "project_id": "one", "environment": "staging",
              "service": "web", "commit_sha": SHA, "image_id": IMAGE, "state": "running"}
    endpoint = "/telemetry/host/deployments"
    assert client.post(endpoint, json={"containers": [record]}).status_code == 401
    host_headers = {"x-verified-client-cert-sha256": fingerprint}
    report = {"host_id": "forged", "event_id": "start-1", "containers": [record]}
    assert client.post(endpoint, headers=host_headers, json=report).status_code == 200
    assert client.post(endpoint, headers=host_headers, json=report).json()["duplicate"] is True
    assert client.post(endpoint, headers=host_headers, json={"containers": [record]}).status_code == 200
    monkeypatch.setenv("TELEMETRY_CI_PROJECT_TOKENS", json.dumps({"one": "1" * 32}))
    ci_headers = {"authorization": "Bearer " + "1" * 32, "content-type": "application/zip"}
    result = client.put(f"/telemetry/projects/one/snapshots/{SHA}/source", headers=ci_headers, content=archive())
    assert result.json()["state"] == "queued"
    assert registry.claim_job() == ("one", SHA)


def test_host_scope_rejects_unprovisioned_environment(tmp_path, monkeypatch):
    fingerprint = "f" * 64
    monkeypatch.setenv("TELEMETRY_HOST_SCOPES", json.dumps({fingerprint: {"projects": {"one": ["staging"]}}}))
    monkeypatch.setattr(http, "get_registry", lambda: SnapshotRegistry(tmp_path))
    app = FastAPI()
    app.include_router(http.host_router)
    record = {"container_id": "d" * 64, "project_id": "one", "environment": "production",
              "service": "web", "commit_sha": SHA, "image_id": IMAGE, "state": "running"}
    response = TestClient(app).post("/telemetry/host/deployments", headers={
        "x-verified-client-cert-sha256": fingerprint}, json={"containers": [record]})
    assert response.status_code == 403


@pytest.mark.parametrize("environment", ["development", "staging", "production"])
def test_host_scope_fixtures_accept_provisioned_environments(tmp_path, monkeypatch, environment):
    fingerprint = "f" * 64
    monkeypatch.setenv("TELEMETRY_HOST_SCOPES", json.dumps({fingerprint: {
        "projects": {"one": ["development", "staging", "production"]}}}))
    monkeypatch.setattr(http, "get_registry", lambda: SnapshotRegistry(tmp_path))
    app = FastAPI()
    app.include_router(http.host_router)
    record = {"container_id": ("d" if environment == "development" else "e" if environment == "staging" else "a") * 64,
              "project_id": "one", "environment": environment, "service": "web",
              "commit_sha": SHA, "image_id": IMAGE, "state": "running"}
    response = TestClient(app).post("/telemetry/host/deployments", headers={
        "x-verified-client-cert-sha256": fingerprint}, json={"containers": [record]})
    assert response.status_code == 200
