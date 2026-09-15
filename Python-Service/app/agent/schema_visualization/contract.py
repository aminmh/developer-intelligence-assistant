"""Framework-neutral schema visualization contract and adapter dispatch."""
from __future__ import annotations

from typing import Any


def normalized_schema(result: dict[str, Any]) -> dict[str, Any]:
    """Project the legacy graph response into the column-level neutral contract."""
    tables = []
    for node in result.get("nodes", []):
        data = node.get("data") or {}
        table_name = data.get("table")
        fields = []
        for column in data.get("columns", []) or []:
            name = column.get("name")
            fields.append({
                **column,
                "id": column.get("id") or f"{node.get('id')}:{name}",
                "name": name,
                "references": column.get("references"),
            })
        tables.append({
            "id": node.get("id") or f"table:{table_name}",
            "name": table_name,
            "fields": fields,
            "position": node.get("position"),
            "provenance": (result.get("source") or {}).get("migrationProvenance"),
        })
    refs = []
    for edge in result.get("edges", []):
        for pair in edge.get("columns") or [{}]:
            refs.append({
                "id": edge.get("id"),
                "sourceTable": edge.get("source"),
                "targetTable": edge.get("target"),
                "sourceColumn": pair.get("source"),
                "targetColumn": pair.get("target"),
                "cardinality": edge.get("cardinality"),
                "provenance": edge.get("provenance"),
                "description": edge.get("description"),
                # Temporary aliases keep older consumers source-compatible
                # while the normalized contract becomes canonical.
                "source": edge.get("source"),
                "target": edge.get("target"),
            })
    return {
        "version": "1.0",
        "tables": tables,
        "refs": refs,
        "warnings": result.get("warnings", []),
        "source": result.get("source", {}),
    }


def with_normalized_contract(result: dict[str, Any]) -> dict[str, Any]:
    result = dict(result)
    result["normalizedSchema"] = normalized_schema(result)
    return result


def build_schema_visualization(profile: dict[str, Any] | None, analysis_id: str, *, generated_at: str | None = None) -> dict[str, Any]:
    """Dispatch to the registered framework adapter without framework logic in the route."""
    profile = profile or {}
    framework = str(profile.get("framework") or "laravel").lower()
    if framework in {"symfony", "doctrine"}:
        from agent.plugins.frameworks.symfony.schema_visualization import build_schema_visualization as build
    else:
        from agent.plugins.frameworks.laravel.schema_visualization import build_schema_visualization as build
    return with_normalized_contract(build(profile, analysis_id, generated_at=generated_at))


__all__ = ["build_schema_visualization", "normalized_schema", "with_normalized_contract"]
