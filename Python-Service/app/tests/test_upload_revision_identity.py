"""Upload revisions retain exact source identity without requiring Git."""
import base64
import hashlib
import json
from types import SimpleNamespace

import jwt
import pytest

from index.storage import Storage
from index.upload_service import UploadService
from telemetry.registry import SnapshotRegistry
from telemetry.snapshot import Snapshot, prepare_chunks, resolve_registered_revision, source_filter, source_reference, storage_id, validate_scope
from telemetry.source import index_uploaded_snapshot


class MemoryStorage(Storage):
    def __init__(self):
        super().__init__()
        self.objects = {}
        self.tags = {}

    def _put_archive(self, key, data, content_type):
        self.objects[key] = data
        return key

    def _get_client(self):
        return SimpleNamespace(set_object_tags=lambda _bucket, key, tags: self.tags.__setitem__(key, tags),
                               remove_object=lambda _bucket, key: self.objects.pop(key, None))

    def list_rag_files(self):
        return [{"path": key, "filename": key.split("/")[-1], "project_id": tags["project-id"],
                 "upload_session_id": key.split("/")[1],
                 "commit_sha": tags.get("commit-sha"), "source_ref": tags["source-ref"],
                 "revision_id": tags["revision-id"], "archive_sha256": tags["archive-sha256"]}
                for key, tags in self.tags.items()]

    def get_rag_file_bytes(self, filename, session_id=None):
        return self.objects.get(f"rag-uploads/{session_id}/{filename}")

    def copy_object(self, source_key, dest_key):
        self.objects[dest_key] = self.objects[source_key]


@pytest.fixture
def uploads(monkeypatch, tmp_path):
    registry = SnapshotRegistry(tmp_path)
    monkeypatch.setattr("telemetry.registry.default_registry", lambda: registry)
    monkeypatch.setattr("telemetry.source.default_registry", lambda: registry)
    return UploadService(), MemoryStorage(), registry


def upload(service, storage, data=b"source bytes", project="pizza", commit=None):
    return service.store_direct(storage, "project.zip", base64.b64encode(data).decode(), project, commit)


def test_upload_validation_accepts_omitted_commit_but_rejects_malformed_metadata():
    assert UploadService.validate_identity(" pizza ", None) == ("pizza", None)
    assert UploadService.validate_identity("pizza", "  ") == ("pizza", None)
    assert UploadService.validate_identity("pizza", "A" * 40) == ("pizza", "a" * 40)
    for value in ("main", "abc123", 42):
        with pytest.raises(ValueError):
            UploadService.validate_identity("pizza", value)
    with pytest.raises(ValueError):
        UploadService.validate_identity("../pizza", None)


def test_direct_upload_registers_checksum_and_reuses_only_matching_project_source(uploads):
    service, storage, registry = uploads
    first = upload(service, storage)
    checksum = hashlib.sha256(b"source bytes").hexdigest()
    assert first["commit_sha"] is None
    assert first["source_ref"] == "upload_" + checksum
    assert first["archive_sha256"] == checksum
    assert first["revision_id"].startswith("rev_")
    assert upload(service, storage)["revision_id"] == first["revision_id"]
    changed = upload(service, storage, b"changed source")
    other = upload(service, storage, project="other")
    assert len({first["revision_id"], changed["revision_id"], other["revision_id"]}) == 3
    current = registry.get_by_revision_id("pizza", first["revision_id"])
    key = registry.archive_key("pizza", source_reference(current))
    assert storage.objects[key] == b"source bytes"
    assert current["state"] == "uploaded"
    with pytest.raises(ValueError):
        validate_scope("pizza", first["source_ref"])


def test_git_identity_and_collection_keys_remain_compatible_and_immutable(uploads):
    service, storage, registry = uploads
    commit = "a" * 40
    first = upload(service, storage, commit=commit)
    expected_key = hashlib.sha256(json.dumps(["pizza", commit, ""], separators=(",", ":")).encode()).hexdigest()
    assert storage_id("pizza", commit, "") == expected_key
    assert first["commit_sha"] == first["source_ref"] == commit
    assert registry.get("pizza", commit)["revision_id"] == first["revision_id"]
    with pytest.raises(ValueError, match="different archive"):
        upload(service, storage, b"conflicting source", commit=commit)
    assert upload(service, storage)["revision_id"] != first["revision_id"]


def test_completed_resumable_upload_and_index_use_the_same_registered_revision(uploads):
    service, storage, registry = uploads
    session = service.create_tus_session("project.zip", 6, "pizza")
    from infra.settings import settings
    claims = jwt.decode(session["upload_token"], settings.upload_token_secret, algorithms=["HS256"])
    assert claims["project_id"] == "pizza" and claims["commit_sha"] is None
    data = b"source"
    checksum = hashlib.sha256(data).hexdigest()
    key = "rag-uploads/" + session["upload_session_id"] + "/project.zip"
    storage.objects[key] = data
    storage.set_rag_identity(key, "pizza", None, checksum, data=data)
    stored = storage.list_rag_files()[0]
    snapshot = resolve_registered_revision(Snapshot("pizza", None, checksum))
    assert snapshot.revision_id == stored["revision_id"]
    chunks = prepare_chunks([{"node_type": "class", "name": "Example", "file_path": "src/Example.php",
                              "start_line": 1, "end_line": 3}], snapshot)
    assert chunks[0]["source_ref"] == stored["source_ref"]
    assert chunks[0]["revision_id"] == stored["revision_id"]
    assert chunks[0]["commit_sha"] is None
    index_uploaded_snapshot(snapshot, lambda: {"chunk_count": 1, "ingested": 1, "graph_nodes": 1})
    current = registry.get_by_revision_id("pizza", stored["revision_id"])
    assert current["state"] == "ready" and current["collection_name"] == snapshot.collection
    assert source_filter(current) == {"source_ref": stored["source_ref"]}
    with pytest.raises(ValueError, match="does not match"):
        source_reference({**current, "source_ref": "upload_" + "0" * 64})


@pytest.mark.parametrize("graph_count", [0, 1])
def test_upload_assessment_requires_its_exact_graph_and_vector_identity(uploads, monkeypatch, graph_count):
    service, storage, registry = uploads
    uploaded = upload(service, storage)
    snapshot = Snapshot("pizza", None, uploaded["archive_sha256"])
    index_uploaded_snapshot(snapshot, lambda: {"chunk_count": 1, "ingested": 1, "graph_nodes": 1})
    from code_quality.index_readiness import assessment_index_readiness
    import index.llamaindex_layer as index

    captured = {}
    def count(**kwargs):
        captured["filter"] = {condition.key: condition.match.value for condition in kwargs["count_filter"].must}
        return SimpleNamespace(count=1)
    def query(statement, param_map):
        captured["query"] = statement
        captured["graph"] = param_map
        return [{"count": graph_count}]
    graph = SimpleNamespace(_get_qdrant=lambda: SimpleNamespace(collection_exists=lambda _: True, count=count),
                            _get_graph_store=lambda: SimpleNamespace(structured_query=query))
    monkeypatch.setattr(index, "CodePropertyGraph", lambda: graph)
    result = assessment_index_readiness("pizza", uploaded["revision_id"], registry)
    assert result["ready"] is bool(graph_count)
    assert captured["filter"] == captured["graph"]
    assert captured["filter"]["source_ref"] == uploaded["source_ref"]
    assert "commit_sha" not in captured["filter"]
    assert "node.source_ref = $source_ref" in captured["query"]
    assert captured["filter"]["revision_id"] == uploaded["revision_id"]


def test_failed_upload_index_never_becomes_ready(uploads):
    service, storage, registry = uploads
    uploaded = upload(service, storage)
    snapshot = Snapshot("pizza", None, uploaded["archive_sha256"])
    with pytest.raises(RuntimeError, match="Incomplete"):
        index_uploaded_snapshot(snapshot, lambda: {"chunk_count": 2, "ingested": 1, "graph_nodes": 2})
    assert registry.get_by_revision_id("pizza", uploaded["revision_id"])["state"] == "failed"


def test_upload_and_parse_api_preserve_auto_revision_and_reject_identity_switches(uploads, monkeypatch):
    import main
    from fastapi import HTTPException
    service, storage, registry = uploads
    monkeypatch.setattr("index.storage.storage", storage)
    monkeypatch.setattr(main, "collection_exists", lambda _: False)
    monkeypatch.setattr(main.settings, "embedding_provider", "ollama")
    captured = {}
    monkeypatch.setattr(main, "threading", SimpleNamespace(Thread=lambda **kwargs:
                        SimpleNamespace(start=lambda: captured.update(kwargs))))
    uploaded = main.rag_upload({"filename": "project.zip", "project_id": "pizza",
                               "content": base64.b64encode(b"source bytes").decode()})
    request = {"filename": "project.zip", "project_id": "pizza", "upload_session_id": uploaded["upload_session_id"],
               "revision_id": uploaded["revision_id"], "source_ref": uploaded["source_ref"]}
    response = main.rag_parse_code(request)
    assert response["collection_name"] == Snapshot("pizza", None, uploaded["archive_sha256"]).collection
    assert captured["args"][4]["commit_sha"] is None
    assert captured["args"][4]["source_ref"] == uploaded["source_ref"]
    for replacement in ({"project_id": "other"}, {"revision_id": "rev_" + "0" * 32}, {"commit_sha": "a" * 40}):
        with pytest.raises(HTTPException) as error:
            main.rag_parse_code({**request, **replacement})
        assert error.value.status_code == 403


def test_tus_finish_hook_assigns_identity_from_completed_bytes(uploads, monkeypatch):
    import main
    service, storage, registry = uploads
    monkeypatch.setattr("index.storage.storage", storage)
    session = service.create_tus_session("project.zip", 6, "pizza")
    storage.objects["incoming-tus-object"] = b"source"
    result = main.tusd_hook({"Type": "post-finish", "Event": {"Upload": {
        "MetaData": {"upload_kind": "rag-document", "upload_token": session["upload_token"],
                     "upload_session_id": session["upload_session_id"]},
        "Storage": {"Key": "incoming-tus-object"},
    }}})
    assert result["handled"] is True
    file = storage.list_rag_files()[0]
    assert file["commit_sha"] is None
    assert file["archive_sha256"] == hashlib.sha256(b"source").hexdigest()
    assert registry.get_by_revision_id("pizza", file["revision_id"])["source_ref"] == file["source_ref"]
