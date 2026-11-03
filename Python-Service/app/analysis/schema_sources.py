"""Convert saved, migration-derived profile facts into editable schema drafts."""

from __future__ import annotations

import json
import uuid
from typing import Any

from analysis.schema_model import Schema


def _id(analysis_id: str, *parts: str) -> str:
    value = json.dumps([analysis_id, *parts], ensure_ascii=False, separators=(",", ":"))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


def schema_from_analysis(profile: dict[str, Any], analysis_id: str) -> Schema:
    """Use migration-derived profile tables only, never inferred graph edges.

    Profiles do not describe all composite constraints or index names. The
    resulting baseline is therefore incomplete and must not imply otherwise.
    """
    raw_tables = profile.get("schema")
    if not isinstance(raw_tables, list) or not raw_tables:
        raise ValueError("Saved analysis has no migration-derived schema tables")
    tables: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    raw_columns: dict[str, list[dict[str, Any]]] = {}
    for raw in raw_tables:
        if not isinstance(raw, dict) or not isinstance(raw.get("table"), str) or not raw["table"].strip():
            raise ValueError("Saved schema has a table without a valid name")
        name = raw["table"]
        columns = raw.get("columns")
        if name in by_name or not isinstance(columns, list):
            raise ValueError(f"Saved schema has duplicate or invalid table {name}")
        table = {"id": _id(analysis_id, "table", name), "name": name,
                 "columns": [], "keys": [], "indexes": []}
        for column in columns:
            if (not isinstance(column, dict) or not isinstance(column.get("name"), str)
                    or not column["name"].strip() or not isinstance(column.get("type"), str)
                    or not column["type"].strip()):
                raise ValueError(f"Saved schema has an invalid column in {name}")
            col_name = column["name"]
            if not isinstance(column.get("nullable"), bool):
                raise ValueError(f"Saved schema has unknown nullability for {name}.{col_name}")
            table["columns"].append({"id": _id(analysis_id, "column", name, col_name),
                                     "name": col_name, "type": column["type"],
                                     "nullable": column["nullable"], "default": column.get("default"),
                                     "collation": column.get("collation"),
                                     "mysql_definition": column.get("mysql_definition")})
        primary = [item["id"] for item, raw_col in zip(table["columns"], columns) if raw_col.get("primary") is True]
        if primary:
            table["keys"].append({"id": _id(analysis_id, "primary", name), "kind": "primary", "column_ids": primary})
        for item, raw_col in zip(table["columns"], columns):
            if raw_col.get("unique") is True:
                table["keys"].append({"id": _id(analysis_id, "unique", name, item["name"]),
                                      "kind": "unique", "column_ids": [item["id"]]})
        tables.append(table)
        by_name[name] = table
        raw_columns[name] = columns

    relationships = []
    for name, columns in raw_columns.items():
        for raw_col in columns:
            ref = raw_col.get("foreign")
            if ref is None or (isinstance(ref, dict) and not ref.get("table")):
                continue
            if not isinstance(ref, dict) or not isinstance(ref.get("table"), str) or not isinstance(ref.get("column"), str):
                raise ValueError(f"Saved schema has an unresolved foreign key in {name}.{raw_col['name']}")
            target = by_name.get(ref["table"])
            target_col = next((c for c in target["columns"] if c["name"] == ref["column"]), None) if target else None
            if target_col is None:
                raise ValueError(f"Saved schema has an unresolved foreign key in {name}.{raw_col['name']}")
            relationships.append({"id": _id(analysis_id, "foreign", name, raw_col["name"], ref["table"], ref["column"]),
                                  "source_table_id": by_name[name]["id"],
                                  "source_column_ids": [_id(analysis_id, "column", name, raw_col["name"])],
                                  "target_table_id": target["id"], "target_column_ids": [target_col["id"]]})
    return Schema.model_validate({"tables": tables, "relationships": relationships})
