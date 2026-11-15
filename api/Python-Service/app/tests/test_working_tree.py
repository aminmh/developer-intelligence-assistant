"""Phase 6 of revision sync: working-tree grants, uploads, and latest-wins indexing."""

import io
import zipfile

import pytest

from telemetry.registry import SnapshotRegistry
from telemetry.source import SourceService
from telemetry.working_tree import GrantError, WorkingTreeGrants, validate_base_commit


class Storage:
    def __init__(self):
        self.archives = {}

    def put_snapshot_archive(self, key, checksum, data):
        self.archives[f"{key}/{checksum}"] = data
        return f"{key}/{checksum}"


def archive(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("app.py", text)
    return buffer.getvalue()


@pytest.fixture
def registry(tmp_path):
    return SnapshotRegistry(tmp_path)


def test_grants_store_only_a_token_hash_and_verify_until_revoked(registry):
    grants = WorkingTreeGrants(registry)
    created = grants.create("shop", {"sub": "u1", "email": "dev@example.test", "extra": "dropped"})
    token, grant_id = created["token"], created["grant_id"]

    with registry._connect() as db:
        stored = db.execute("SELECT token_sha256, user_json FROM working_tree_grants").fetchone()
    assert token not in stored["token_sha256"] and "extra" not in stored["user_json"]
    assert grants.verify(grant_id, token)["user"] == {"sub": "u1", "email": "dev@example.test"}
    assert grants.verify(grant_id, token + "x") is None and grants.verify("wtg_bad", token) is None
    assert [g["grant_id"] for g in grants.list("shop")] == [grant_id] and "token" not in grants.list("shop")[0]

    assert grants.revoke("shop", grant_id) and not grants.revoke("shop", grant_id)
    assert grants.verify(grant_id, token) is None
    with pytest.raises(GrantError):
        grants.create("shop", {"email": "no-subject"})


def test_base_commit_is_optional_lineage_but_must_be_a_full_sha():
    assert validate_base_commit(None) is None and validate_base_commit("a" * 40) == "a" * 40
    with pytest.raises(GrantError):
        validate_base_commit("HEAD")


def test_a_newer_working_tree_supersedes_unfinished_ones_and_a_running_one_ends_superseded(registry):
    service = SourceService(registry, Storage())
    first = service.upload_working_tree("shop", archive("v1"), "c" * 40)
    assert (first["source_kind"], first["base_commit_sha"], first["state"]) == ("working_tree", "c" * 40, "queued")
    assert first["commit_sha"] is None and first["source_ref"].startswith("wt_")

    second = service.upload_working_tree("shop", archive("v2"), "c" * 40)

    assert registry.get("shop", first["source_ref"])["state"] == "superseded"
    assert registry.claim_job() == ("shop", second["source_ref"])
    # A third save arrives while the second indexes: the second stops and is recorded superseded.
    third = service.upload_working_tree("shop", archive("v3"), None)
    assert registry.is_superseded("shop", second["source_ref"])
    registry.finish_job("shop", second["source_ref"], success=True)
    assert registry.get("shop", second["source_ref"])["state"] == "superseded"
    assert registry.claim_job() == ("shop", third["source_ref"])


def test_returning_to_an_indexed_tree_makes_it_the_latest_again(registry):
    service = SourceService(registry, Storage())
    old = service.upload_working_tree("shop", archive("same"))
    registry.claim_job()
    registry.finish_job("shop", old["source_ref"], success=True)
    newer = service.upload_working_tree("shop", archive("other"))
    registry.claim_job()
    registry.finish_job("shop", newer["source_ref"], success=True)
    assert registry.latest_ready("shop")["revision_id"] == newer["revision_id"]

    again = service.upload_working_tree("shop", archive("same"))

    assert again["revision_id"] == old["revision_id"] and again["state"] == "ready"
    assert registry.latest_ready("shop")["revision_id"] == old["revision_id"]


def test_working_tree_revisions_are_never_assessed(registry):
    from code_quality.index_readiness import assessment_index_readiness

    row = SourceService(registry, Storage()).upload_working_tree("shop", archive("v1"))
    result = assessment_index_readiness("shop", row["revision_id"], registry)
    assert result["status"] == "working_tree_unavailable" and not result["ready"]


def test_retention_cleans_superseded_working_trees(registry, monkeypatch):
    import telemetry.retention as retention

    service = SourceService(registry, Storage())
    first = service.upload_working_tree("shop", archive("v1"))
    service.upload_working_tree("shop", archive("v2"))
    monkeypatch.setattr(retention, "_reported_revisions", lambda project_id: set())

    class Store:
        def get(self, key, default=None):
            return default

    plan = retention.plan_cleanup(registry=registry, store=Store())
    assert [row["revision_id"] for row in plan["shop"]] == [first["revision_id"]]


def test_a_superseded_index_stops_between_embedding_batches(registry, monkeypatch):
    import index.llamaindex_layer as layer
    from telemetry.source import Superseded

    calls = []
    def embed(texts, input_type, progress=None):
        calls.append(texts)
        progress(len(texts))  # the real providers report each finished batch
        return [[1.0]] * len(texts)

    monkeypatch.setattr(layer, "embed_texts", embed)
    monkeypatch.setattr(layer, "embed_profile_key", lambda: "p")
    cpg = layer.CodePropertyGraph.__new__(layer.CodePropertyGraph)

    class Qdrant:
        def __getattr__(self, name):
            return lambda *a, **k: None

    cpg._get_qdrant = lambda: Qdrant()
    chunks = [{"type": "function", "name": f"f{i}", "class_fqn": f"f{i}", "namespace": "", "file": "a.py",
               "start_line": i, "end_line": i, "code": f"return {i}"} for i in range(200)]

    def stop(stage, done, total):
        if done >= 96:
            raise Superseded("newer tree")

    with pytest.raises(Superseded):
        cpg._ingest_vectors(chunks, "c", replace=False, progress_callback=stop)
    assert len(calls) == 1  # the second batch never reached the embedding provider
