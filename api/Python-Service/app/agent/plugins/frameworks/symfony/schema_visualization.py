"""Doctrine/Symfony schema adapter.

The adapter consumes saved Doctrine metadata when available and merges it with
the migration-derived ``schema`` slice. It never guesses across conflicting
sources; unresolved metadata is surfaced as warnings by the shared graph
builder.
"""
from __future__ import annotations

from typing import Any


def _doctrine_to_profile(profile: dict[str, Any]) -> dict[str, Any]:
    entities = profile.get("doctrine_entities") or profile.get("entities") or []
    migration_sources = profile.get("doctrine_migrations") or profile.get("migrations") or []
    models: list[dict[str, Any]] = []
    schema = list(profile.get("schema") or [])
    source_warnings: list[dict[str, Any]] = []
    for migration in migration_sources:
        if not isinstance(migration, dict):
            continue
        migration_tables = migration.get("schema") if isinstance(migration.get("schema"), list) else [migration]
        for raw_table in migration_tables:
            if not isinstance(raw_table, dict):
                continue
            table = str(raw_table.get("table") or raw_table.get("table_name") or "").strip()
            if not table:
                continue
            columns = raw_table.get("columns") or raw_table.get("fields") or []
            existing = next((item for item in schema if isinstance(item, dict) and str(item.get("table")) == table), None)
            if existing is None:
                schema.append({"table": table, "columns": columns})
            elif columns and existing.get("columns") and existing.get("columns") != columns:
                source_warnings.append({"code": "schema-source-conflict", "message": f"Doctrine migration sources disagree for table {table}; retained the first migration definition.", "table": table})
    known = {str(item.get("table")) for item in schema if isinstance(item, dict)}
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        table = str(entity.get("table") or entity.get("table_name") or "").strip()
        name = str(entity.get("name") or entity.get("class") or "").split("\\")[-1]
        if not table:
            continue
        fields = entity.get("fields") or entity.get("columns") or []
        columns = []
        for field in fields:
            if not isinstance(field, dict) or not field.get("name"):
                continue
            columns.append({
                "name": field["name"],
                "type": field.get("type") or field.get("columnDefinition") or "unknown",
                "primary": bool(field.get("id") or field.get("primary")),
                "nullable": bool(field.get("nullable", False)),
                "unique": bool(field.get("unique", False)),
                "foreign": field.get("foreign") or field.get("references"),
            })
        if table not in known:
            schema.append({"table": table, "columns": columns})
            known.add(table)
        else:
            existing = next((item for item in schema if isinstance(item, dict) and str(item.get("table")) == table), None)
            existing_columns = {str(item.get("name")): item for item in (existing or {}).get("columns", []) if isinstance(item, dict)}
            for field in columns:
                prior = existing_columns.get(str(field.get("name")))
                if prior is not None and str(prior.get("type") or "").lower() != str(field.get("type") or "").lower():
                    source_warnings.append({"code": "schema-source-conflict", "message": f"Doctrine entity and migration disagree for {table}.{field.get('name')}; retained the migration definition.", "table": table, "column": field.get("name")})
        relations = entity.get("relations") or entity.get("associations") or []
        models.append({"name": name or table, "table": table, "relations": relations})
    merged = dict(profile)
    merged["framework"] = "symfony"
    merged["schema"] = schema
    merged["models"] = models
    merged["_schema_warnings"] = source_warnings
    return merged


def build_schema_visualization(profile: dict[str, Any] | None, analysis_id: str, *, generated_at: str | None = None) -> dict[str, Any]:
    from agent.plugins.frameworks.laravel.schema_visualization import build_schema_visualization as build

    source = _doctrine_to_profile(profile or {})
    result = build(source, analysis_id, generated_at=generated_at)
    result["source"] = dict(result.get("source") or {})
    result["source"].update({
        "adapter": "symfony-doctrine",
        "metadataProvenance": "project_profile.doctrine_entities",
        "migrationProvenance": "project_profile.schema",
    })
    warnings = [w for w in (result.get("warnings") or []) if w.get("code") != "unsupported-framework"]
    warnings.extend(source.get("_schema_warnings") or [])
    if not (profile or {}).get("doctrine_entities") and not (profile or {}).get("entities"):
        warnings.append({"code": "missing-doctrine-metadata", "message": "No Doctrine entity metadata was found; migration tables only are available."})
    result["warnings"] = warnings
    return result


__all__ = ["build_schema_visualization"]
