"""Phase 4 of revision sync: chat answers from a project's latest (or pinned) ready revision."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import rag.revision_source as revision_source
from rag.revision_source import RevisionUnavailable, resolve_revision

LATEST = {"project_id": "shop", "revision_id": "rev_" + "a" * 32, "state": "ready", "collection_name": "snapshot_new",
          "source_kind": "working_tree", "commit_sha": None, "base_commit_sha": "c" * 40,
          "created_at": "2026-11-12 13:00:00"}
OLDER = {"project_id": "shop", "revision_id": "rev_" + "b" * 32, "state": "ready", "collection_name": "snapshot_old",
         "source_kind": "git", "commit_sha": "d" * 40, "created_at": "2026-11-11 10:00:00"}
INDEXING = {"revision_id": "rev_" + "e" * 32, "state": "indexing", "collection_name": None, "source_kind": "upload"}


class Registry:
    def __init__(self, latest=LATEST):
        self.latest = latest

    def latest_ready(self, project_id):
        return self.latest if project_id == "shop" else None

    def get_by_revision_id(self, project_id, revision_id):
        rows = {row["revision_id"]: row for row in (LATEST, OLDER, INDEXING)}
        return rows.get(revision_id) if project_id == "shop" else None


@pytest.fixture(autouse=True)
def active_collections(monkeypatch):
    monkeypatch.setattr("index.embedding_rebuild.active_vector_collection", lambda collection: collection)


def test_latest_resolves_to_the_newest_ready_revision_including_working_trees():
    body = resolve_revision({"query": "q", "project_id": "shop", "revision": "latest"}, Registry())

    assert (body["revision_id"], body["collection"], body["commit_sha"]) == (LATEST["revision_id"], "snapshot_new", None)
    assert body["resolved_revision"] == {"project_id": "shop", "revision_id": LATEST["revision_id"], "source_kind": "working_tree",
                                         "commit_sha": None, "base_commit_sha": "c" * 40,
                                         "created_at": "2026-11-12 13:00:00", "requested": "latest"}
    assert "revision" not in body and body["query"] == "q"


def test_a_pinned_revision_stays_pinned_and_client_source_fields_are_replaced():
    body = resolve_revision({"query": "q", "project_id": "shop", "revision": OLDER["revision_id"],
                             "collection": "snapshot_someone_else", "filename": "x.zip",
                             "upload_session_id": "s", "commit_sha": "f" * 40}, Registry())

    assert body["collection"] == "snapshot_old" and body["commit_sha"] == "d" * 40
    assert "filename" not in body and "upload_session_id" not in body
    assert body["resolved_revision"]["requested"] == "pinned"


def test_requests_without_a_revision_are_unchanged():
    original = {"query": "q", "filename": "x.zip", "collection": "c"}
    assert resolve_revision(original, Registry()) is original


@pytest.mark.parametrize("body, message", [
    ({"project_id": "shop", "revision": "rev_" + "e" * 32}, "not indexed and ready"),
    ({"project_id": "shop", "revision": "rev_" + "9" * 32}, "does not exist"),
    ({"project_id": "shop", "revision": "../etc"}, "Invalid revision"),
    ({"project_id": "other", "revision": "latest"}, "no indexed revision"),
    ({"revision": "latest"}, "project is required"),
])
def test_unavailable_revisions_are_refused_with_a_reason(body, message):
    with pytest.raises(RevisionUnavailable, match=message):
        resolve_revision(body, Registry())


def test_a_revision_without_its_active_cohere_generation_is_refused(monkeypatch):
    monkeypatch.setattr("index.embedding_rebuild.active_vector_collection", lambda collection: None)
    with pytest.raises(RevisionUnavailable, match="Cohere"):
        resolve_revision({"project_id": "shop", "revision": "latest"}, Registry())


def test_citation_route_refuses_an_unavailable_revision(monkeypatch):
    from routers.code_tools import router

    monkeypatch.setattr("telemetry.registry.default_registry", lambda: Registry(latest=None))
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).post("/code-tools/read", json={"project_id": "shop", "revision": "latest",
                                                               "path": "a.go", "line": 1})
    assert response.status_code == 409 and "no indexed revision" in response.json()["detail"]
