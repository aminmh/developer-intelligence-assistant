from telemetry.http import project_scope


class FakeRegistry:
    def revisions_by_project(self):
        return {
            "pizza": [
                {"project_id": "pizza", "revision_id": "rev_" + "a" * 32, "state": "ready", "commit_sha": "1670d9e",
                 "source_kind": "git", "created_at": "2026-11-06", "collection_name": "snapshot_alive"},
                {"project_id": "pizza", "revision_id": "rev_" + "b" * 32, "state": "uploaded", "commit_sha": "c0ffee",
                 "source_kind": "upload", "created_at": "2026-11-05"},
            ],
            "Pizza": [
                {"project_id": "Pizza", "revision_id": "rev_" + "c" * 32, "state": "ready", "commit_sha": "upload_45e9",
                 "source_kind": "upload", "created_at": "2026-11-01", "collection_name": "snapshot_gone"},
            ],
        }


def test_ready_revision_without_vectors_is_not_indexed():
    projects = {item["project_id"]: item for item in project_scope(FakeRegistry(), {"snapshot_alive"})}

    pizza = projects["pizza"]
    assert pizza["indexed"] is True
    assert [rev["indexed"] for rev in pizza["revisions"]] == [True, False]
    assert pizza["revisions"][1]["vectors_missing"] is False
    assert pizza["revisions"][0]["requested"] == "pinned"

    stale = projects["Pizza"]
    assert stale["indexed"] is False
    assert stale["revisions"][0]["state"] == "ready"
    assert stale["revisions"][0]["vectors_missing"] is True


def test_unreachable_qdrant_falls_back_to_registry_state():
    projects = {item["project_id"]: item for item in project_scope(FakeRegistry(), None)}

    assert projects["Pizza"]["revisions"][0]["indexed"] is True
    assert projects["pizza"]["revisions"][1]["indexed"] is False
