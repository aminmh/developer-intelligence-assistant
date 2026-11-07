"""Synced Outline rule index and rule <-> code links."""
import pytest

from code_quality.rule_index import RuleIndex, RuleIndexError

PROJECT = "pizza"
DOC_A = "0b5c3a10-1111-4c1e-9d55-aaaaaaaaaaaa"
DOC_B = "0b5c3a10-2222-4c1e-9d55-bbbbbbbbbbbb"


def _rule(doc_id, revision=1, text="Orders over 500 need approval.", title="Refund approval"):
    return {"rule_id": doc_id, "version": revision, "title": title, "statement": text,
            "content_hash": f"{doc_id}:{revision}:{text}", "updated_at": "2026-11-07", "url_id": doc_id[:8]}


@pytest.fixture
def index(tmp_path):
    return RuleIndex(tmp_path / "cq.sqlite")


def test_sync_records_added_changed_and_removed_documents(index):
    first = index.sync(PROJECT, [_rule(DOC_A), _rule(DOC_B)])
    assert sorted(event["kind"] for event in first["events"]) == ["added", "added"]
    assert index.sync(PROJECT, [_rule(DOC_A), _rule(DOC_B)])["events"] == []

    second = index.sync(PROJECT, [_rule(DOC_A, 2, "Orders over 300 need approval.")])
    kinds = {event["doc_id"]: event for event in second["events"]}
    assert kinds[DOC_A]["kind"] == "changed" and kinds[DOC_A]["old_revision"] == 1 and kinds[DOC_A]["new_revision"] == 2
    assert kinds[DOC_B]["kind"] == "removed"
    assert [item["doc_id"] for item in index.documents(PROJECT)] == [DOC_A]
    assert index.document(PROJECT, DOC_B) is None
    assert index.status(PROJECT)["status"] == "synchronized"
    assert index.documents(PROJECT)[0]["url"] == f"/doc/{DOC_A[:8]}"


def test_failed_sync_keeps_the_last_copy(index):
    index.sync(PROJECT, [_rule(DOC_A)])
    index.record_failure(PROJECT, "Outline could not be reached.")
    assert index.status(PROJECT)["status"] == "unavailable"
    assert index.status(PROJECT)["synced_at"] is not None
    assert len(index.documents(PROJECT)) == 1


def test_suggestions_never_overwrite_reviewed_links(index):
    index.sync(PROJECT, [_rule(DOC_A)])
    index.add_suggestions(PROJECT, DOC_A, [{"symbol_id": "App\\Refund::approve", "score": 0.7},
                                           {"symbol_id": "App\\Refund::cancel", "score": 0.6}])
    index.set_link_state(PROJECT, DOC_A, "App\\Refund::approve", "confirmed", "user-1", code_hash="h1")
    index.set_link_state(PROJECT, DOC_A, "App\\Refund::cancel", "rejected", "user-1")
    written = index.add_suggestions(PROJECT, DOC_A, [{"symbol_id": "App\\Refund::approve", "score": 0.9},
                                                     {"symbol_id": "App\\Refund::cancel", "score": 0.9}])
    assert written == 0
    states = {link["symbol_id"]: link["state"] for link in index.links(PROJECT)}
    assert states == {"App\\Refund::approve": "confirmed", "App\\Refund::cancel": "rejected"}


def test_rule_and_code_drift_are_reported_on_reviewed_links(index):
    index.sync(PROJECT, [_rule(DOC_A)])
    index.set_link_state(PROJECT, DOC_A, "route:POST /refunds", "manual", "user-1", code_hash="h1")
    assert index.links(PROJECT)[0]["needs_review"] is False

    index.sync(PROJECT, [_rule(DOC_A, 2, "Orders over 300 need approval.")])
    link = index.links(PROJECT, symbol_ids=["route:POST /refunds"])[0]
    assert link["rule_changed"] is True and link["code_changed"] is False

    index.record_code_hashes(PROJECT, "rev_" + "1" * 32, {"route:POST /refunds": "h2"})
    assert index.links(PROJECT)[0]["code_changed"] is True

    # Re-confirming stamps the current rule revision and code hash.
    index.set_link_state(PROJECT, DOC_A, "route:POST /refunds", "confirmed", "user-1", code_hash="h2")
    assert index.links(PROJECT)[0]["needs_review"] is False


def test_link_validation(index):
    index.sync(PROJECT, [_rule(DOC_A)])
    with pytest.raises(RuleIndexError):
        index.set_link_state(PROJECT, DOC_A, "App\\X::y", "suggested", "user-1")
    with pytest.raises(RuleIndexError):
        index.set_link_state(PROJECT, DOC_B, "App\\X::y", "confirmed", "user-1")
    with pytest.raises(RuleIndexError):
        index.set_link_state(PROJECT, "../etc", "App\\X::y", "confirmed", "user-1")


def test_remote_use_is_off_by_default(index):
    assert index.allow_remote(PROJECT) is False
    assert index.set_allow_remote(PROJECT, True) is True
    assert index.allow_remote(PROJECT) is True
    with pytest.raises(RuleIndexError):
        index.set_allow_remote(PROJECT, "yes")
