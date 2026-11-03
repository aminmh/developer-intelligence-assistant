from copy import deepcopy
import sqlite3

import pytest
from pydantic import ValidationError

from analysis.analysis_store import AnalysisStore
from analysis.schema_drafts import DraftConflict, DraftNotFound, MAX_LAYOUT_BYTES, SchemaDraftStore


@pytest.fixture
def stores(tmp_path):
    db = str(tmp_path / "settings.db")
    analyses = AnalysisStore(db_path=db, analysis_dir=str(tmp_path / "analyses"))
    analyses.save("analysis-1", "job-1", "project.zip", profile={"schema": ["original"]})
    return analyses, SchemaDraftStore(db)


@pytest.fixture
def schema():
    return {
        "version": 1,
        "tables": [
            {"id": "table-users", "name": "users", "columns": [
                {"id": "column-user-id", "name": "id", "type": "bigint", "nullable": False},
                {"id": "column-email", "name": "email", "type": "text", "nullable": False},
            ], "keys": [{"id": "key-users", "kind": "primary", "column_ids": ["column-user-id"]}],
             "indexes": [{"id": "index-email", "name": "users_email", "column_ids": ["column-email"], "unique": True}]},
            {"id": "table-posts", "name": "posts", "columns": [
                {"id": "column-post-user", "name": "user_id", "type": "bigint", "nullable": False},
            ]},
        ],
        "relationships": [{"id": "relation-user", "source_table_id": "table-posts",
                           "source_column_ids": ["column-post-user"], "target_table_id": "table-users",
                           "target_column_ids": ["column-user-id"]}],
    }


def test_draft_round_trip_preserves_baseline_and_analysis(stores, schema):
    analyses, drafts = stores
    created = drafts.create(schema, analysis_id="analysis-1", layout="<mxfile/>")
    changed = deepcopy(schema)
    changed["tables"][0]["name"] = "accounts"
    saved = drafts.save(created["id"], changed, "<mxfile>layout</mxfile>", 1)
    restored = SchemaDraftStore(drafts.db_path).get(created["id"])
    assert saved == restored
    assert restored["revision"] == 2
    assert restored["baseline"]["tables"][0]["name"] == "users"
    assert restored["schema"]["tables"][0]["id"] == "table-users"
    assert restored["schema"]["tables"][0]["name"] == "accounts"
    assert restored["layout"] == "<mxfile>layout</mxfile>"
    assert analyses.get("analysis-1")["profile"] == {"schema": ["original"]}
    assert drafts.list()[0]["analysis_id"] == "analysis-1"


def test_invalid_save_and_stale_revision_leave_last_valid_draft(stores, schema):
    _, drafts = stores
    draft = drafts.create(schema)
    invalid = deepcopy(schema)
    invalid["relationships"][0]["target_column_ids"] = ["missing"]
    with pytest.raises(ValidationError, match="Invalid column references"):
        drafts.save(draft["id"], invalid, "invalid", 1)
    with pytest.raises(ValueError, match="size limit"):
        drafts.save(draft["id"], schema, "é" * MAX_LAYOUT_BYTES, 1)
    assert drafts.get(draft["id"]) == draft
    drafts.save(draft["id"], schema, "new", 1)
    with pytest.raises(DraftConflict):
        drafts.save(draft["id"], schema, "stale", 1)
    assert drafts.get(draft["id"])["layout"] == "new"


def test_ids_and_composite_references_are_validated(stores, schema):
    _, drafts = stores
    duplicate = deepcopy(schema)
    duplicate["tables"][1]["columns"][0]["id"] = "column-user-id"
    with pytest.raises(ValidationError, match="Duplicate schema ID"):
        drafts.create(duplicate)
    mismatched = deepcopy(schema)
    mismatched["relationships"][0]["target_column_ids"].append("column-email")
    with pytest.raises(ValidationError, match="Mismatched columns"):
        drafts.create(mismatched)


def test_analysis_reference_and_deletion(stores, schema):
    analyses, drafts = stores
    with pytest.raises(sqlite3.IntegrityError):
        drafts.create(schema, analysis_id="missing")
    draft = drafts.create(schema, analysis_id="analysis-1")
    with pytest.raises(sqlite3.IntegrityError):
        drafts.create(schema, analysis_id="analysis-1")
    imported = drafts.create(schema)
    assert analyses.delete("analysis-1")
    with pytest.raises(DraftNotFound):
        drafts.get(draft["id"])
    assert drafts.get(imported["id"])["source_kind"] == "import"


def test_restore_rejects_corrupted_schema(stores, schema):
    _, drafts = stores
    draft = drafts.create(schema)
    with sqlite3.connect(drafts.db_path) as conn:
        conn.execute("UPDATE schema_drafts SET schema_json = ? WHERE id = ?", ('{"tables":[] , "unknown":1}', draft["id"]))
    with pytest.raises(ValidationError):
        drafts.get(draft["id"])
