import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from code_quality.index_readiness import assessment_index_readiness
from telemetry.snapshot import Snapshot


PROJECT = "pizza"
REVISION = "rev_" + "b" * 32
SNAPSHOT = Snapshot(PROJECT, "a" * 40, "c" * 64, revision_id=REVISION)


def registry_for(snapshot, archive="source.zip"):
    return SimpleNamespace(get_by_revision_id=lambda *_: snapshot, archive_key=lambda *_: archive)


def test_unindexed_and_pending_revisions_do_not_probe_backends():
    for snapshot in (None, {"state": "failed"}, {"state": "awaiting_source"}):
        result = assessment_index_readiness(PROJECT, REVISION, registry_for(snapshot))
        assert result["ready"] is False
        assert result["status"] == "index_required"
        assert "Knowledge Base" in result["reason"]
    result = assessment_index_readiness(PROJECT, REVISION, registry_for({"state": "indexing"}))
    assert result["status"] == "indexing"


@pytest.mark.parametrize(("exists", "vector_count", "graph_count", "expected"), [
    (False, 0, 0, False), (True, 0, 0, False), (True, 3, 0, False),
    (True, 3, 2, False), (True, 3, 3, True),
])
def test_readiness_requires_matching_persisted_revision_indexes(monkeypatch, exists, vector_count, graph_count, expected):
    import index.llamaindex_layer as index

    captured = {}
    def count(**kwargs):
        captured["filter"] = {condition.key: condition.match.value for condition in kwargs["count_filter"].must}
        return SimpleNamespace(count=vector_count)
    def query(_query, param_map):
        captured["graph"] = param_map
        return [{"count": graph_count}]
    graph = SimpleNamespace(_get_qdrant=lambda: SimpleNamespace(collection_exists=lambda _: exists, count=count),
                            _get_graph_store=lambda: SimpleNamespace(structured_query=query))
    monkeypatch.setattr(index, "CodePropertyGraph", lambda: graph)
    snapshot = {**SNAPSHOT.metadata(), "state": "ready", "collection_name": SNAPSHOT.collection}
    result = assessment_index_readiness(PROJECT, REVISION, registry_for(snapshot))
    assert result["ready"] is expected
    if vector_count and exists:
        assert captured["graph"] == captured["filter"] == {
            "project_id": PROJECT, "revision_id": REVISION, "commit_sha": SNAPSHOT.commit_sha,
            "source_archive_sha256": SNAPSHOT.source_archive_sha256, "collection": SNAPSHOT.collection, "dirty": False,
        }


def test_backend_failure_is_distinct_from_absent_index(monkeypatch):
    import index.llamaindex_layer as index
    def unavailable():
        raise ConnectionError("backend unavailable")
    monkeypatch.setattr(index, "CodePropertyGraph", unavailable)
    snapshot = {**SNAPSHOT.metadata(), "state": "ready", "collection_name": SNAPSHOT.collection}
    result = assessment_index_readiness(PROJECT, REVISION, registry_for(snapshot))
    assert result["ready"] is False
    assert result["status"] == "index_unavailable"


def test_assessment_rejects_missing_index_before_reading_archive_or_calling_model(monkeypatch):
    import code_quality.http as http
    import telemetry.registry as registry
    monkeypatch.setenv("RAG_CALLBACK_SECRET", "x" * 40)
    monkeypatch.setattr(registry, "default_registry", lambda: registry_for(None))
    def unexpected_assessment(*_):
        pytest.fail("Assessment must not start without an index")
    monkeypatch.setattr(http, "assess_revision", unexpected_assessment)
    async def empty_body():
        return {"type": "http.request", "body": b"{}", "more_body": False}
    request = Request({"type": "http", "method": "POST", "path": "/assess",
                       "headers": [(b"x-internal-secret", b"x" * 40), (b"x-user-id", b"alice")]}, receive=empty_body)
    with pytest.raises(HTTPException) as error:
        asyncio.run(http.assess(PROJECT, REVISION, request))
    assert error.value.status_code == 409
    assert "Index this project revision" in error.value.detail


def test_precheck_requires_internal_authentication(monkeypatch):
    import code_quality.http as http
    monkeypatch.setenv("RAG_CALLBACK_SECRET", "x" * 40)
    request = Request({"type": "http", "method": "GET", "path": "/precheck", "headers": []})
    with pytest.raises(HTTPException) as error:
        asyncio.run(http.assessment_precheck(PROJECT, REVISION, request))
    assert error.value.status_code == 401
