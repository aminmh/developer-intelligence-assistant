"""Structured schema used by editable drafts, independent of diagram XML."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=r"\S")]


class SchemaPart(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, revalidate_instances="always")

    id: Identifier


class MySQLColumnDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    unsigned: bool
    charset: Identifier | None
    collation: Identifier | None
    comment: str | None
    default_kind: Literal["none", "null", "literal"]
    other_attributes_absent: bool


class Column(SchemaPart):
    name: Identifier
    type: Annotated[str, Field(min_length=1, max_length=65536, pattern=r"\S")]
    nullable: bool
    default: str | int | float | bool | None = None
    default_expression: str | None = Field(default=None, max_length=65536)
    identity: str | None = Field(default=None, max_length=65536)
    auto_increment: bool | None = None
    collation: Identifier | None = None
    mysql_definition: MySQLColumnDefinition | None = None


class Key(SchemaPart):
    name: Identifier | None = None
    kind: Literal["primary", "unique"]
    column_ids: list[Identifier] = Field(min_length=1)


class Index(SchemaPart):
    name: Identifier
    column_ids: list[Identifier] = Field(min_length=1)
    unique: bool = False


class Table(SchemaPart):
    name: Identifier
    namespace: Identifier | None = None
    columns: list[Column]
    keys: list[Key] = Field(default_factory=list)
    indexes: list[Index] = Field(default_factory=list)
    inspection_metadata: list[str] = Field(default_factory=list)


class Relationship(SchemaPart):
    name: Identifier | None = None
    source_table_id: Identifier
    source_column_ids: list[Identifier] = Field(min_length=1)
    target_table_id: Identifier
    target_column_ids: list[Identifier] = Field(min_length=1)
    on_delete: Literal["cascade", "restrict", "set null", "no action", "set default"] | None = None
    on_update: Literal["cascade", "restrict", "set null", "no action", "set default"] | None = None


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, revalidate_instances="always")

    version: Literal[1] = 1
    tables: list[Table]
    relationships: list[Relationship] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> "Schema":
        ids: set[str] = set()
        tables: dict[str, Table] = {}
        names: set[tuple[str | None, str]] = set()

        def add_id(value: str) -> None:
            if value in ids:
                raise ValueError(f"Duplicate schema ID: {value}")
            ids.add(value)

        def check_columns(values: list[str], available: set[str], owner: str) -> None:
            if len(values) != len(set(values)) or not set(values) <= available:
                raise ValueError(f"Invalid column references in {owner}")

        for table in self.tables:
            add_id(table.id)
            name = (table.namespace, table.name)
            if name in names:
                raise ValueError(f"Duplicate table name: {table.name}")
            names.add(name)
            tables[table.id] = table
            if len({column.name for column in table.columns}) != len(table.columns):
                raise ValueError(f"Duplicate column name in table {table.name}")
            column_ids = {column.id for column in table.columns}
            for column in table.columns:
                add_id(column.id)
            if sum(key.kind == "primary" for key in table.keys) > 1:
                raise ValueError(f"Multiple primary keys in table {table.name}")
            for item in [*table.keys, *table.indexes]:
                add_id(item.id)
                check_columns(item.column_ids, column_ids, item.id)

        for relationship in self.relationships:
            add_id(relationship.id)
            source = tables.get(relationship.source_table_id)
            target = tables.get(relationship.target_table_id)
            if source is None or target is None:
                raise ValueError(f"Unknown table in relationship {relationship.id}")
            if len(relationship.source_column_ids) != len(relationship.target_column_ids):
                raise ValueError(f"Mismatched columns in relationship {relationship.id}")
            check_columns(relationship.source_column_ids, {c.id for c in source.columns}, relationship.id)
            check_columns(relationship.target_column_ids, {c.id for c in target.columns}, relationship.id)
        return self
