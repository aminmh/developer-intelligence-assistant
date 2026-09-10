from agent.plugins.frameworks.laravel.schema import collapse_migrations, parse_migration
from agent.plugins.frameworks.laravel.schema_visualization import (
    build_schema_visualization,
)


def _schema():
    users = """
    Schema::create('users', function (Blueprint $table) {
        $table->id();
    });
    """
    posts = """
    Schema::create('posts', function (Blueprint $table) {
        $table->id();
        $table->foreignId('user_id')->constrained('users');
        $table->string('slug')->unique()->index();
        $table->string('title')->nullable()->default('draft');
    });
    """
    tags = """
    Schema::create('tags', function (Blueprint $table) { $table->id(); });
    Schema::create('post_tag', function (Blueprint $table) {
        $table->foreignId('post_id')->constrained('posts');
        $table->foreignId('tag_id')->constrained('tags');
    });
    """
    return collapse_migrations(
        [
            parse_migration(users, "001_users.php"),
            parse_migration(posts, "002_posts.php"),
            parse_migration(tags, "003_tags.php"),
        ]
    )


def test_normalizer_marks_columns_and_relationships():
    result = build_schema_visualization(
        {
            "framework": "laravel",
            "schema": _schema(),
            "models": [
                {
                    "name": "Post",
                    "table": "posts",
                    "primary_key": "id",
                    "relations": [
                        {
                            "method": "tags",
                            "type": "belongsToMany",
                            "target": "App\\Models\\Tag",
                        },
                    ],
                },
                {"name": "Tag", "table": "tags", "relations": []},
            ],
        },
        "analysis-1",
        generated_at="2026-09-10T00:00:00+00:00",
    )
    nodes = {node["data"]["table"]: node for node in result["nodes"]}
    posts = {column["name"]: column for column in nodes["posts"]["data"]["columns"]}
    assert posts["id"]["primaryKey"] is True
    assert posts["user_id"]["foreignKey"] is True
    assert posts["user_id"]["references"] == {"table": "users", "column": "id"}
    assert posts["slug"]["unique"] is True and posts["slug"]["indexed"] is True
    assert posts["title"]["nullable"] is True and posts["title"]["default"] == "draft"
    assert any(
        edge["cardinality"] == "1:N" and edge["source"] == "users"
        for edge in result["edges"]
    )
    assert any(
        edge["cardinality"] == "N:M"
        and edge["source"] == "posts"
        and edge["target"] == "tags"
        for edge in result["edges"]
    )
    assert all(
        edge["source"] in nodes and edge["target"] in nodes for edge in result["edges"]
    )


def test_explicit_on_target_and_unresolved_fk_are_reported():
    migration = """
    Schema::create('orders', function (Blueprint $table) {
        $table->id();
        $table->unsignedBigInteger('account_id')->references('uuid')->on('accounts');
        $table->foreign('account_id')->references('uuid')->on('accounts');
    });
    """
    schema = collapse_migrations([parse_migration(migration, "orders.php")])
    result = build_schema_visualization(
        {"framework": "laravel", "schema": schema, "models": []},
        "analysis-2",
        generated_at="now",
    )
    account = next(
        column
        for column in result["nodes"][0]["data"]["columns"]
        if column["name"] == "account_id"
    )
    assert account["references"] == {"table": "accounts", "column": "uuid"}
    assert any(
        warning["code"] == "unresolved-foreign-key" for warning in result["warnings"]
    )
    assert result["edges"] == []


def test_cycle_layout_is_stable_and_warns():
    profile = {
        "framework": "laravel",
        "schema": [
            {
                "table": "a",
                "columns": [
                    {
                        "name": "b_id",
                        "type": "int",
                        "foreign": {"table": "b", "column": "id"},
                    }
                ],
            },
            {
                "table": "b",
                "columns": [
                    {
                        "name": "a_id",
                        "type": "int",
                        "foreign": {"table": "a", "column": "id"},
                    }
                ],
            },
        ],
        "models": [],
    }
    first = build_schema_visualization(profile, "analysis-3", generated_at="now")
    second = build_schema_visualization(profile, "analysis-3", generated_at="now")
    assert {node["id"]: node["position"] for node in first["nodes"]} == {
        node["id"]: node["position"] for node in second["nodes"]
    }
    assert any(warning["code"] == "layout-fallback" for warning in first["warnings"])
