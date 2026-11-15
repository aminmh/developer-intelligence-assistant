"""Data management: ownership, dry-run plans, project purge of local stores, jobs and internal auth."""
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from data_admin import deletion, inventory, paths
from data_admin.journal import Journal
from data_admin.ownership import Owners, classify_collection, classify_revision_ref

SECRET = "s" * 40
REV = "rev_" + "a" * 32


def owners() -> Owners:
    return Owners(projects={"shop"}, revisions={REV: "shop"},
                  collections={"snapshot_abc": ("shop", REV)}, rule_collections={"business_rules_123": "shop"},
                  checksums={"f" * 64})


def test_collections_are_attributed_to_their_owner():
    o = owners()
    assert classify_collection("snapshot_abc", o)["owner"] == "revision"
    assert classify_collection("snapshot_abc_cohere_" + "0" * 32, o)["revision_id"] == REV
    assert classify_collection("sym_snapshot_abc", o)["project_id"] == "shop"
    assert classify_collection("business_rules_123", o)["owner"] == "project"
    assert classify_collection("domain_concepts_cohere_v5", o)["owner"] == "shared"
    assert classify_collection("snapshot_gone", o)["owner"] == "orphan"
    assert classify_collection("business_rules_999", o)["owner"] == "orphan"
    assert classify_collection("chunk_code_Archive", o)["owner"] == "legacy"
    assert classify_collection("sym_Archive", o)["owner"] == "legacy"


def test_tagged_data_is_orphaned_when_its_revision_or_project_is_gone():
    o = owners()
    assert classify_revision_ref("shop", REV, o)["owner"] == "revision"
    assert classify_revision_ref("shop", "rev_" + "b" * 32, o)["owner"] == "orphan"
    assert classify_revision_ref("gone", None, o)["owner"] == "orphan"
    assert classify_revision_ref(None, None, o)["owner"] == "legacy"


def test_project_inventory_includes_usage_and_preserves_unknown_on_failure(monkeypatch):
    import telemetry.registry as registry_module
    import telemetry.retention as retention

    class Registry:
        def project_revisions(self, project_id):
            return [{"revision_id": REV, "state": "ready"}]

    monkeypatch.setattr(registry_module, "default_registry", Registry)
    snapshot = {"projects": ["shop"], "stores": {}}
    expected = {"status": "unused", "reasons": ["Eligible for cleanup."]}
    monkeypatch.setattr(retention, "project_revision_usage", lambda *args, **kwargs: {REV: expected})
    rows = inventory.project_totals(snapshot)
    assert rows[0]["revisions"][0]["usage"] == expected

    def unavailable(*args, **kwargs):
        raise OSError("Assessment store is offline")

    monkeypatch.setattr(retention, "project_revision_usage", unavailable)
    rows = inventory.project_totals(snapshot)
    assert rows[0]["revisions"][0]["usage"]["status"] == "unknown"
    assert "Refresh" in rows[0]["revisions"][0]["usage"]["reasons"][0]


@pytest.fixture
def local_stores(tmp_path, monkeypatch):
    """A snapshot root and two project-scoped SQLite stores; network stores report nothing."""
    monkeypatch.setenv("TELEMETRY_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    files = {"snapshots": tmp_path / "snapshots" / "snapshots.sqlite", "code_quality": tmp_path / "cq.sqlite",
             "agent_runs": tmp_path / "runs.sqlite", "agent_checkpoints": tmp_path / "checkpoints.sqlite"}
    from telemetry.registry import SnapshotRegistry
    SnapshotRegistry(tmp_path / "snapshots")
    with sqlite3.connect(files["code_quality"]) as db:
        db.execute("CREATE TABLE performance_targets (id TEXT, project_id TEXT)")
        db.executemany("INSERT INTO performance_targets VALUES (?,?)", [("1", "shop"), ("2", "shop"), ("3", "other")])
        db.execute("CREATE TABLE app_settings (k TEXT)")
    with sqlite3.connect(files["agent_runs"]) as db:
        db.execute("CREATE TABLE tool_approvals (run_id TEXT, project_id TEXT)")
        db.executemany("INSERT INTO tool_approvals VALUES (?,?)", [("run-shop", "shop"), ("run-other", "other")])
    with sqlite3.connect(files["agent_checkpoints"]) as db:
        db.execute("CREATE TABLE checkpoints (thread_id TEXT)")
        db.executemany("INSERT INTO checkpoints VALUES (?)", [("run-shop",), ("run-other",)])
    maps = tmp_path / "maps"
    maps.mkdir()
    (maps / ("e" * 64 + ".json")).write_text("{}")
    monkeypatch.setattr(paths, "sqlite_files", lambda: files)
    monkeypatch.setattr(paths, "directories", lambda: {"repo_maps": maps})
    collectors = {name: (lambda o: []) for name in inventory.STORES}
    collectors["sqlite"] = inventory.collect_sqlite
    collectors["files"] = inventory.collect_files
    monkeypatch.setattr(inventory, "COLLECTORS", collectors)
    forgotten = []
    monkeypatch.setitem(deletion.EXECUTORS, "settings_forget_project", lambda project_id: forgotten.append(project_id))
    inventory.invalidate()
    yield {"files": files, "maps": maps, "forgotten": forgotten, "journal": Journal(tmp_path / "journal.sqlite")}
    inventory.invalidate()


def _count(path, sql):
    with sqlite3.connect(path) as db:
        return db.execute(sql).fetchone()[0]


def test_project_purge_removes_only_that_projects_rows(local_stores):
    files, journal = local_stores["files"], local_stores["journal"]
    planned = deletion.plan({"kind": "project", "project_id": "shop"})
    actions = [step["action"] for step in planned["steps"]]
    assert actions.index("sqlite_delete_checkpoints") < next(
        i for i, s in enumerate(planned["steps"]) if s["args"].get("table") == "tool_approvals")
    job = deletion.start(planned["target"], planned["hash"], {"sub": "admin"}, journal=journal, background=False)
    assert journal.job(job["job_id"])["state"] == "succeeded"
    assert _count(files["code_quality"], "SELECT count(*) FROM performance_targets WHERE project_id='shop'") == 0
    assert _count(files["code_quality"], "SELECT count(*) FROM performance_targets WHERE project_id='other'") == 1
    assert _count(files["agent_runs"], "SELECT count(*) FROM tool_approvals") == 1
    assert _count(files["agent_checkpoints"], "SELECT thread_id FROM checkpoints") == "run-other"
    assert local_stores["forgotten"] == ["shop"]
    results = [entry["result"] for entry in journal.audit_entries(project_ids=["shop"])]
    assert results[:2] == ["succeeded", "started"]
    assert journal.audit_entries(project_ids=["nobody"]) == []


def test_execution_refuses_a_plan_that_changed_since_the_dry_run(local_stores):
    planned = deletion.plan({"kind": "project", "project_id": "shop"})
    with pytest.raises(deletion.PlanChanged):
        deletion.start(planned["target"], "0" * 64, {"sub": "admin"}, journal=local_stores["journal"], background=False)
    assert _count(local_stores["files"]["code_quality"], "SELECT count(*) FROM performance_targets") == 3


def test_orphan_files_are_deletable_and_owned_data_is_not(local_stores):
    item = next(i for i in inventory.snapshot(refresh=True)["stores"]["files"]["items"])
    assert item["owner"] == "orphan" and item["deletable"]
    planned = deletion.plan({"kind": "items", "items": [{"store": "files", "id": item["id"]}]})
    deletion.start(planned["target"], planned["hash"], {"sub": "admin"}, journal=local_stores["journal"], background=False)
    assert not any(local_stores["maps"].iterdir())
    table = next(i for i in inventory.snapshot(refresh=True)["stores"]["sqlite"]["items"] if i["id"] == "code_quality:performance_targets")
    with pytest.raises(deletion.PlanError):
        deletion.plan({"kind": "items", "items": [{"store": "sqlite", "id": table["id"]}]})


def test_file_removal_stays_inside_the_managed_directory(local_stores):
    for name in ("..", "../x", "a/b"):
        with pytest.raises(deletion.PlanError):
            deletion.EXECUTORS["files_remove"](directory="repo_maps", name=name)


@pytest.fixture
def client(local_stores, monkeypatch):
    import data_admin.http as http

    monkeypatch.setenv("RAG_CALLBACK_SECRET", SECRET)
    monkeypatch.setattr(http, "_journal", local_stores["journal"])
    app = FastAPI()
    app.include_router(http.router)
    return TestClient(app)


def test_every_endpoint_requires_the_internal_secret(client):
    for method, path in (("get", "/data-admin/summary"), ("get", "/data-admin/orphans"), ("get", "/data-admin/audit"),
                         ("post", "/data-admin/deletions/plan"), ("post", "/data-admin/deletions/execute")):
        assert getattr(client, method)(path).status_code == 401
    assert client.get("/data-admin/summary", headers={"X-Internal-Secret": SECRET}).status_code == 200


def test_a_running_job_blocks_another_deletion(client, local_stores):
    headers = {"X-Internal-Secret": SECRET, "X-User-Id": "admin"}
    planned = client.post("/data-admin/deletions/plan", headers=headers,
                          json={"target": {"kind": "project", "project_id": "shop"}}).json()
    local_stores["journal"].create_job(actor_sub="someone", target={}, steps=[])
    response = client.post("/data-admin/deletions/execute", headers=headers,
                           json={"target": planned["target"], "hash": planned["hash"]})
    assert response.status_code == 409
    assert _count(local_stores["files"]["code_quality"], "SELECT count(*) FROM performance_targets") == 3
