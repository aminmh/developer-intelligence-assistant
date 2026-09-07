"""D9 regression tests for the Laravel migration/schema parser.

Fixtures model the real-world shapes observed in the armaiti project:
up()/down() splits, rename + re-declare (->change()) migrations, phantom
tables created only in down(), duplicate columns across update migrations,
constrained() FKs, and quoted numeric defaults.
"""

import pytest

from agent.plugins.frameworks.laravel.schema import (
    collapse_migrations,
    parse_migration,
)

_CREATE_USERS = """<?php
return new class extends Migration {
    public function up(): void
    {
        Schema::create('users', function (Blueprint $table) {
            $table->id();
            $table->string('email')->unique();
            $table->unsignedBigInteger('avatar')->nullable();
            $table->foreign('avatar')->references('id')->on('media');
            $table->enum('active', ['1', '2'])->default(2);
            $table->integer('experience_amount')->default(0);
            $table->timestamps();
        });
    }
    public function down(): void
    {
        Schema::dropIfExists('users');
    }
};
"""

_RENAME_ACTIVE = """<?php
return new class extends Migration {
    public function up(): void
    {
        Schema::table('users', function (Blueprint $table): void {
            $table->renameColumn('active', 'status');
        });
        Schema::table('users', function (Blueprint $table) {
            $table->enum('status', ['activated', 'pending', 'deactivated'])
                ->default('pending')
                ->change();
        });
    }
    public function down(): void
    {
        Schema::table('users', function (Blueprint $table) {
            $table->enum('status', ['1', '2'])->default('2')->change();
            $table->renameColumn('status', 'active');
        });
    }
};
"""

_DOWN_ONLY_PHANTOM = """<?php
return new class extends Migration {
    public function up(): void
    {
        Schema::table('users', function (Blueprint $table) {
            $table->renameColumn('avatar', 'media_id');
            $table->foreign('media_id')->references('id')->on('media');
        });
    }
    public function down(): void
    {
        Schema::table('posts', function (Blueprint $table) {
            $table->dropForeign(['media_id']);
            $table->renameColumn('media_id', 'file');
        });
    }
};
"""

_CREATE_POSTS = """<?php
return new class extends Migration {
    public function up(): void
    {
        Schema::create('posts', function (Blueprint $table) {
            $table->id();
            $table->foreignId('user_id')->constrained();
            $table->string('title');
        });
    }
};
"""

_DROP_STATUS = """<?php
return new class extends Migration {
    public function up(): void
    {
        Schema::table('users', function (Blueprint $table) {
            $table->dropColumn('status');
        });
    }
};
"""


def _cols(schema: list[dict], table: str) -> dict[str, dict]:
    for t in schema:
        if t["table"] == table:
            return {c["name"]: c for c in t["columns"]}
    return {}


class TestParseScoping:
    def test_up_only_state_down_ignored(self):
        parsed = parse_migration(_DOWN_ONLY_PHANTOM, "x")
        assert "posts" not in parsed["created"]
        assert parsed["renames"] == [("users", "avatar", "media_id")]
        assert parsed["drops"] == []

    def test_rename_and_redeclare_in_up(self):
        parsed = parse_migration(_RENAME_ACTIVE, "x")
        assert ("users", "active", "status") in parsed["renames"]
        assert "users" in parsed["created"]
        names = {c["name"] for c in parsed["created"]["users"]["columns"]}
        assert names == {"status"}  # down() enum ignored

    def test_drop_column_reported(self):
        parsed = parse_migration(_DROP_STATUS, "x")
        assert ("users", "status") in parsed["drop_columns"]

    def test_legacy_flat_migration_no_up(self):
        flat = "Schema::create('x', function (Blueprint $table) { $table->id(); });"
        assert "x" in parse_migration(flat)["created"]


class TestCollapseSemantics:
    def test_sequence_rename_dedupe_last_wins(self):
        schema = collapse_migrations(
            [
                parse_migration(_CREATE_USERS, "1"),
                parse_migration(_RENAME_ACTIVE, "2"),
            ]
        )
        users = _cols(schema, "users")
        assert "active" not in users  # renamed away, never duplicated
        assert "status" in users
        # re-declared enum with ->change() overrides the original active col
        assert users["status"]["default"] == "pending"
        assert users["status"]["type"] == "enum"

    def test_down_phantom_table_absent(self):
        schema = collapse_migrations(
            [
                parse_migration(_CREATE_USERS, "1"),
                parse_migration(_DOWN_ONLY_PHANTOM, "2"),
            ]
        )
        tables = {t["table"] for t in schema}
        assert "posts" not in tables  # was only ever touched in down()

    def test_drop_column_removes_state(self):
        schema = collapse_migrations(
            [
                parse_migration(_CREATE_USERS, "1"),
                parse_migration(_RENAME_ACTIVE, "2"),
                parse_migration(_DROP_STATUS, "3"),
            ]
        )
        assert "status" not in _cols(schema, "users")

    def test_constrained_fk_defaults_to_id(self):
        schema = collapse_migrations([parse_migration(_CREATE_POSTS, "1")])
        user_id = _cols(schema, "posts")["user_id"]
        assert user_id["type"] == "bigint unsigned"
        assert user_id["foreign"] == {"table": "users", "column": "id"}

    def test_numeric_default_coercion(self):
        schema = collapse_migrations([parse_migration(_CREATE_USERS, "1")])
        users = _cols(schema, "users")
        assert users["experience_amount"]["default"] == 0  # int, not "0"
        assert isinstance(users["experience_amount"]["default"], int)

    def test_no_duplicate_columns_across_updates(self):
        # Two update migrations re-adding the same column must not duplicate.
        second = """<?php
return new class extends Migration {
    public function up(): void {
        Schema::table('users', function (Blueprint $table) {
            $table->boolean('is_verified')->default('0');
        });
    }
};
"""
        schema = collapse_migrations(
            [parse_migration(_CREATE_USERS, "1"), parse_migration(second, "2")]
        )
        users = _cols(schema, "users")
        assert users["is_verified"]["default"] is False  # boolean coercion
        assert [c["name"] for c in schema[0]["columns"]].count("is_verified") == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
