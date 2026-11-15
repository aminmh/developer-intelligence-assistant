from agent.schema_visualization import build_schema_visualization


def test_normalized_schema_exposes_tables_and_column_level_refs():
    result = build_schema_visualization(
        {
            "framework": "laravel",
            "schema": [
                {"table": "users", "columns": [{"name": "id", "type": "int", "primary": True}]},
                {
                    "table": "posts",
                    "columns": [
                        {"name": "id", "type": "int", "primary": True},
                        {"name": "user_id", "type": "int", "foreign": {"table": "users", "column": "id"}},
                    ],
                },
            ],
        },
        "analysis-contract",
        generated_at="2026-09-12T00:00:00+00:00",
    )

    normalized = result["normalizedSchema"]
    assert normalized["version"] == "1.0"
    assert {table["name"] for table in normalized["tables"]} == {"users", "posts"}
    assert any(
        ref["source"] == "users"
        and ref["target"] == "posts"
        and ref["sourceColumn"] == "id"
        and ref["targetColumn"] == "user_id"
        for ref in normalized["refs"]
    )


def test_symfony_adapter_reports_missing_doctrine_metadata_honestly():
    result = build_schema_visualization(
        {
            "framework": "symfony",
            "schema": [{"table": "users", "columns": [{"name": "id", "primary": True}]}],
        },
        "analysis-symfony",
        generated_at="2026-09-12T00:00:00+00:00",
    )

    assert result["source"]["adapter"] == "symfony-doctrine"
    assert any(warning["code"] == "missing-doctrine-metadata" for warning in result["warnings"])
    assert not any(warning["code"] == "unsupported-framework" for warning in result["warnings"])


def test_symfony_entity_metadata_is_merged_into_schema():
    result = build_schema_visualization(
        {
            "framework": "symfony",
            "doctrine_entities": [
                {
                    "class": "App\\Entity\\Post",
                    "table": "posts",
                    "fields": [{"name": "id", "type": "integer", "id": True}],
                }
            ],
        },
        "analysis-symfony-entity",
        generated_at="2026-09-12T00:00:00+00:00",
    )

    assert [table["name"] for table in result["normalizedSchema"]["tables"]] == ["posts"]
    assert result["normalizedSchema"]["tables"][0]["fields"][0]["primaryKey"] is True
