"""Phase 5 of revision sync: keep the latest N revisions per project and remove the rest."""

import pytest

import telemetry.retention as retention
from telemetry.retention import InvalidRetention, get_policy, removable_revisions, run_cleanup, set_policy


class Store:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


def rev(n, kind="upload", state="ready"):
    ref = {"upload": "upload_", "working_tree": "wt_"}.get(kind, "")
    return {"revision_id": f"rev_{n:032x}", "state": state, "source_kind": kind,
            "commit_sha": None if kind != "git" else f"{n:040x}",
            "source_archive_sha256": f"{n:064x}", "project_id": "shop", "dirty": kind == "working_tree",
            **({"source_ref": ref + f"{n:064x}"} if ref else {})}


class Registry:
    def __init__(self, rows, deployed=()):
        self.rows, self.deployed = rows, set(deployed)

    def project_revisions(self, project_id):
        return self.rows  # newest first

    def has_deployment(self, project_id, reference):
        return reference in self.deployed


def ids(rows):
    return [int(r["revision_id"][4:], 16) for r in rows]


POLICY = {"keep_latest": 2, "pinned": []}


def test_keeps_the_latest_n_ready_revisions_and_removes_the_rest():
    rows = [rev(6), rev(5, state="indexing"), rev(4), rev(3), rev(2, state="failed"), rev(1)]
    assert ids(removable_revisions("shop", registry=Registry(rows), policy=POLICY, reported=set(), git_revisions=set())) == [3, 1]


def test_usage_explains_retention_and_agrees_with_cleanup_candidates():
    rows = [rev(9), rev(8), rev(7, "git"), rev(6), rev(5), rev(4), rev(3), rev(2, state="failed")]
    registry = Registry(rows, deployed={f"{7:040x}"})
    kwargs = {"registry": registry, "project_id": "shop", "policy": {"keep_latest": 2, "pinned": [rev(6)["revision_id"]]},
              "reported": {rev(5)["revision_id"]}, "git_revisions": {rev(4)["revision_id"]}}
    usage = retention.revision_usage(rows, **kwargs)
    expected_reasons = {9: "Latest ready", 8: "kept by retention", 7: "running deployment",
                        6: "Pinned", 5: "assessment report", 4: "Git source"}
    for n, reason in expected_reasons.items():
        assert usage[rev(n)["revision_id"]]["status"] == "kept"
        assert reason in " ".join(usage[rev(n)["revision_id"]]["reasons"])
    assert usage[rev(3)["revision_id"]]["status"] == "unused"
    assert usage[rev(2)["revision_id"]]["status"] == "not_ready"
    assert ids(removable_revisions(**kwargs)) == [3]


def test_usage_protects_the_newest_working_tree_separately():
    rows = [rev(5), rev(4, "working_tree"), rev(3, "working_tree"), rev(2)]
    usage = retention.revision_usage(rows, registry=Registry(rows), project_id="shop",
                                     policy=POLICY, reported=set(), git_revisions=set())
    assert usage[rev(4)["revision_id"]]["status"] == "kept"
    assert usage[rev(3)["revision_id"]]["status"] == "unused"
    assert usage[rev(2)["revision_id"]]["status"] == "kept"


def test_reports_pins_git_sync_and_deployments_protect_old_revisions():
    rows = [rev(5), rev(4), rev(3, "git"), rev(2), rev(1), rev(0)]
    protected = removable_revisions("shop", registry=Registry(rows, deployed={f"{3:040x}"}),
                                    policy={"keep_latest": 2, "pinned": [rev(2)["revision_id"]]},
                                    reported={rev(1)["revision_id"]}, git_revisions={rev(0)["revision_id"]})
    assert protected == []


def test_working_trees_roll_outside_n_and_the_latest_is_never_removed():
    rows = [rev(9, "working_tree"), rev(8, "working_tree"), rev(7), rev(6, "working_tree"), rev(5), rev(4)]
    removable = removable_revisions("shop", registry=Registry(rows), policy={"keep_latest": 1, "pinned": []},
                                    reported=set(), git_revisions=set())
    assert ids(removable) == [8, 6, 5, 4]
    only_old = removable_revisions("shop", registry=Registry([rev(3, "working_tree")]), policy=POLICY,
                                   reported=set(), git_revisions=set())
    assert only_old == []


def test_policy_defaults_validates_and_saves():
    store = Store()
    assert get_policy("shop", store=store) == {"project_id": "shop", "keep_latest": 3, "pinned": [], "updated_at": None}
    saved = set_policy("shop", {"keep_latest": 5, "pinned": [rev(2)["revision_id"], rev(2)["revision_id"]]}, store=store)
    assert saved["keep_latest"] == 5 and saved["pinned"] == [rev(2)["revision_id"]]
    assert set_policy("shop", {"keep_latest": 1}, store=store)["pinned"] == [rev(2)["revision_id"]]
    for change in ({"keep_latest": 0}, {"keep_latest": True}, {"keep_latest": "3"}, {"pinned": ["../x"]}, {}):
        with pytest.raises(InvalidRetention):
            set_policy("shop", change, store=store)


def test_cleanup_removes_planned_revisions_and_retries_busy_ones_later(monkeypatch):
    monkeypatch.setattr(retention, "plan_cleanup", lambda **_: {"shop": [rev(2), rev(1)]})
    calls = []

    def remove(project_id, revision_id):
        calls.append(revision_id)
        if revision_id == rev(2)["revision_id"]:
            raise RuntimeError("An indexing job is running.")
        return True

    assert run_cleanup(remove=remove) == [("shop", rev(1)["revision_id"])]
    assert len(calls) == 2


def test_removing_the_last_revision_of_an_archive_drops_its_repo_map_and_code_cache(tmp_path, monkeypatch):
    import agent_tools.repo_map as repo_map
    import agent_tools.source as source
    from code_quality.revision_deletion import _remove_archive_caches

    checksum = "a" * 64
    monkeypatch.setenv("REPO_MAP_DIR", str(tmp_path / "maps"))
    monkeypatch.setattr(source, "CACHE_ROOT", tmp_path / "cache")
    repo_map.store_repo_map(checksum, {"version": repo_map.MAP_VERSION, "files": []})
    (tmp_path / "cache" / checksum / "src").mkdir(parents=True)
    (tmp_path / "cache" / checksum / "src" / "a.py").write_text("x")
    source._make_read_only(tmp_path / "cache" / checksum)

    _remove_archive_caches(checksum)

    assert repo_map.load_repo_map(checksum) is None and not (tmp_path / "cache" / checksum).exists()
    _remove_archive_caches(checksum)  # already gone is fine


def test_a_working_tree_row_resolves_to_its_own_reference_and_collection():
    from telemetry.snapshot import Snapshot, source_reference

    row = rev(4, "working_tree")
    assert source_reference(row) == "wt_" + "0" * 63 + "4"
    assert Snapshot.from_row("shop", row).collection != Snapshot.from_row("shop", {**row, "dirty": False}).collection


def test_an_interrupted_removal_is_retried_whatever_the_policy(monkeypatch):
    class Live(Registry):
        def _connect(self):
            import sqlite3
            db = sqlite3.connect(":memory:")
            db.execute("CREATE TABLE snapshots(project_id TEXT)")
            db.execute("INSERT INTO snapshots VALUES ('shop')")
            return db

        def list_git_sources(self):
            return []

    monkeypatch.setattr(retention, "_reported_revisions", lambda project_id: set())
    stuck = rev(1, state="deleting")
    plan = retention.plan_cleanup(registry=Live([rev(2), stuck]), store=Store())
    assert ids(plan["shop"]) == [1]
