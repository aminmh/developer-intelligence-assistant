from copy import deepcopy

import pytest

from analysis.schema_sources import schema_from_analysis


def test_saved_profile_conversion_uses_only_explicit_foreign_keys():
    profile = {"schema": [
        {"table": "users", "columns": [
            {"name": "id", "type": "bigint", "nullable": False, "primary": True},
        ]},
        {"table": "posts", "columns": [
            {"name": "user_id", "type": "bigint", "nullable": False,
             "foreign": {"table": "users", "column": "id"}},
        ]},
    ], "models": [{"name": "Post", "relations": [{"target": "Other"}]}]}
    result = schema_from_analysis(profile, "analysis-1")
    assert len(result.relationships) == 1
    assert result.relationships[0].source_table_id == result.tables[1].id
    assert result.relationships[0].target_table_id == result.tables[0].id
    assert result.tables[0].keys[0].kind == "primary"
    assert result == schema_from_analysis(deepcopy(profile), "analysis-1")
    assert result.tables[0].id != schema_from_analysis(profile, "analysis-2").tables[0].id


def test_unresolved_foreign_key_and_missing_schema_are_rejected():
    profile = {"schema": [{"table": "posts", "columns": [
        {"name": "user_id", "type": "int", "nullable": False,
         "foreign": {"table": "users", "column": "id"}},
    ]}]}
    with pytest.raises(ValueError, match="unresolved foreign key"):
        schema_from_analysis(profile, "analysis-1")
    with pytest.raises(ValueError, match="no migration-derived schema"):
        schema_from_analysis({}, "analysis-1")


def test_explicit_collation_is_retained_and_missing_collation_is_unknown():
    profile = {"schema": [{"table": "items", "columns": [
        {"name": "label", "type": "text", "nullable": False, "collation": "pg_catalog.C"},
        {"name": "other", "type": "text", "nullable": True},
    ]}]}
    columns = schema_from_analysis(profile, "analysis-1").tables[0].columns
    assert columns[0].collation == "pg_catalog.C"
    assert columns[1].collation is None


def test_saved_profile_preserves_explicit_auto_increment():
    profile = {"schema": [{"table": "items", "columns": [
        {"name": "id", "type": "integer", "nullable": False, "auto_increment": True},
        {"name": "other", "type": "integer", "nullable": True},
        {"name": "explicit", "type": "integer", "nullable": True, "auto_increment": False},
    ]}]}
    columns = schema_from_analysis(profile, "analysis-1").tables[0].columns
    assert columns[0].auto_increment is True
    assert columns[1].auto_increment is None
    assert columns[2].auto_increment is False


def test_saved_profile_rejects_invalid_auto_increment():
    profile = {"schema": [{"table": "items", "columns": [
        {"name": "id", "type": "integer", "nullable": False, "auto_increment": "yes"},
    ]}]}
    with pytest.raises(ValueError):
        schema_from_analysis(profile, "analysis-1")
