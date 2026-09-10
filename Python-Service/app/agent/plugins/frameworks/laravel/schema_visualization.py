"""Deterministic schema-graph normalization for saved Laravel analyses.

The Laravel plugin already produces the canonical migration-derived ``schema``
slice and a lightweight Eloquent ``models`` slice.  This module deliberately
does not inspect a live database or call an LLM: it turns those saved facts
into a stable graph contract for the developer UI.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any


def _slug(value: Any) -> str:
    return str(value or "").strip()


def _snake_case(value: str) -> str:
    value = value.split("\\")[-1]
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()


def _plural_table(model_name: str) -> str:
    """Small Eloquent-style fallback for profiles without an explicit table."""
    value = _snake_case(model_name)
    if value.endswith(("s", "x", "z", "ch", "sh")):
        return value + "es"
    if value.endswith("y") and len(value) > 1 and value[-2] not in "aeiou":
        return value[:-1] + "ies"
    return value + "s"


def _model_table(model: dict[str, Any]) -> str | None:
    return (
        _slug(model.get("table")) or _plural_table(_slug(model.get("name")))
        if _slug(model.get("name"))
        else None
    )


def _relation_target_name(target: Any) -> str:
    return _slug(target).split("\\")[-1].split("::")[0]


def _edge_id(
    source: str, target: str, cardinality: str, columns: list[dict[str, str]]
) -> str:
    pairs = ",".join(f"{p.get('source','')}->{p.get('target','')}" for p in columns)
    return f"relation:{source}:{target}:{cardinality}:{pairs}"


def _column_ref(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    table = _slug(value.get("table"))
    column = _slug(value.get("column"))
    return {"table": table, "column": column or "id"} if table else None


def _normalise_column(column: dict[str, Any], primary_keys: set[str]) -> dict[str, Any]:
    name = _slug(column.get("name"))
    reference = _column_ref(column.get("foreign"))
    return {
        "name": name,
        "type": _slug(column.get("type")) or "unknown",
        "primaryKey": bool(
            column.get("primary") or name in primary_keys or name == "id"
        ),
        "foreignKey": reference is not None,
        "nullable": bool(column.get("nullable", False)),
        "default": column.get("default"),
        "unique": bool(column.get("unique", False)),
        "indexed": bool(column.get("indexed", False)),
        "references": reference,
    }


def _relationship_description(rel_type: str, model_name: str, method: str) -> str:
    labels = {
        "belongsTo": "belongs to",
        "hasMany": "has many",
        "hasOne": "has one",
        "belongsToMany": "belongs to many",
    }
    return f"{model_name} {labels.get(rel_type, rel_type)} ({method})"


def _layout(
    table_names: list[str], relationships: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, float]], bool]:
    """Layer parent tables before children; use a stable order for cycles."""
    names = sorted(set(table_names))
    outgoing: dict[str, set[str]] = {name: set() for name in names}
    indegree: dict[str, int] = {name: 0 for name in names}
    for edge in relationships:
        source, target = edge["source"], edge["target"]
        if source == target or source not in outgoing or target not in outgoing:
            continue
        if target not in outgoing[source]:
            outgoing[source].add(target)
            indegree[target] += 1

    layers: dict[str, int] = {name: 0 for name in names}
    queue = deque(sorted(name for name in names if indegree[name] == 0))
    visited: list[str] = []
    while queue:
        current = queue.popleft()
        visited.append(current)
        for target in sorted(outgoing[current]):
            layers[target] = max(layers[target], layers[current] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)

    cycle_fallback = len(visited) != len(names)
    if cycle_fallback:
        # Preserve already-computed layers and deterministically append cyclic
        # nodes after the acyclic portion instead of dropping or fabricating
        # positions.
        next_layer = max(layers.values(), default=0) + 1
        for name in names:
            if name not in visited:
                layers[name] = next_layer
                next_layer += 1

    grouped: dict[int, list[str]] = defaultdict(list)
    for name in names:
        grouped[layers[name]].append(name)
    positions: dict[str, dict[str, float]] = {}
    for layer in sorted(grouped):
        for row, name in enumerate(sorted(grouped[layer])):
            positions[name] = {"x": float(layer * 360), "y": float(row * 190)}
    return positions, cycle_fallback


def build_schema_visualization(
    profile: dict[str, Any] | None,
    analysis_id: str,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build the public schema visualization contract from a saved profile."""
    profile = profile or {}
    framework = _slug(profile.get("framework")) or None
    warnings: list[dict[str, Any]] = []
    schema = profile.get("schema") if isinstance(profile.get("schema"), list) else []
    models = profile.get("models") if isinstance(profile.get("models"), list) else []

    tables: dict[str, dict[str, Any]] = {}
    model_table_by_name: dict[str, str] = {}
    model_meta_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for model in models:
        if not isinstance(model, dict):
            continue
        table = _model_table(model)
        name = _slug(model.get("name"))
        if table and name:
            model_table_by_name[name] = table
            model_table_by_name[name.split("\\")[-1]] = table
            model_meta_by_table[table].append(model)

    for raw_table in schema:
        if not isinstance(raw_table, dict):
            continue
        table_name = _slug(raw_table.get("table"))
        if not table_name:
            continue
        primary_keys = {
            _slug(model.get("primary_key"))
            for model in model_meta_by_table.get(table_name, [])
            if _slug(model.get("primary_key"))
        }
        columns = [
            _normalise_column(column, primary_keys)
            for column in (raw_table.get("columns") or [])
            if isinstance(column, dict) and _slug(column.get("name"))
        ]
        tables[table_name] = {"name": table_name, "columns": columns}

    if framework and framework.lower() != "laravel":
        warnings.append(
            {
                "code": "unsupported-framework",
                "message": "Schema visualization currently supports Laravel analyses only.",
            }
        )
    if not schema:
        warnings.append(
            {
                "code": "no-migrations",
                "message": "No migration-derived tables were found in this analysis.",
            }
        )

    relationships: list[dict[str, Any]] = []
    relationship_keys: set[tuple[str, str, str, str]] = set()
    unresolved_targets: set[tuple[str, str, str]] = set()

    def add_edge(
        source: str,
        target: str,
        cardinality: str,
        columns: list[dict[str, str]],
        description: str,
        *,
        provenance: str,
    ) -> None:
        if source not in tables or target not in tables:
            return
        for existing in relationships:
            if (
                existing["source"] != source
                or existing["target"] != target
                or existing["cardinality"] != cardinality
            ):
                continue
            existing_columns = existing.get("columns") or []
            # An Eloquent relation enriches the migration edge instead of
            # creating a visually duplicate line. Distinct migration FKs
            # retain separate edges because both carry real column pairs.
            if not columns or not existing_columns or existing_columns == columns:
                existing["description"] = f"{existing['description']}; {description}"
                existing["provenance"] = (
                    existing.get("provenance")
                    if existing.get("provenance") == provenance
                    else f"{existing.get('provenance', 'static')}+{provenance}"
                )
                if columns and not existing_columns:
                    existing["columns"] = columns
                return
        key = (
            source,
            target,
            cardinality,
            ",".join(f"{p.get('source','')}->{p.get('target','')}" for p in columns),
        )
        if key in relationship_keys:
            return
        relationship_keys.add(key)
        relationships.append(
            {
                "id": _edge_id(source, target, cardinality, columns),
                "source": source,
                "target": target,
                "columns": columns,
                "cardinality": cardinality,
                "description": description,
                "provenance": provenance,
            }
        )

    # Migration foreign keys are authoritative and always produce a graph edge.
    for table_name in sorted(tables):
        for column in tables[table_name]["columns"]:
            ref = column.get("references")
            if not ref:
                continue
            ref_table = ref["table"]
            if ref_table not in tables:
                unresolved_targets.add((table_name, column["name"], ref_table))
                continue
            cardinality = "1:1" if column.get("unique") else "1:N"
            add_edge(
                ref_table,
                table_name,
                cardinality,
                [{"source": ref["column"], "target": column["name"]}],
                f"{table_name}.{column['name']} references {ref_table}.{ref['column']}",
                provenance="migration",
            )

    for child, column, target in sorted(unresolved_targets):
        warnings.append(
            {
                "code": "unresolved-foreign-key",
                "message": f"Foreign key {child}.{column} references missing table {target}.",
                "table": child,
                "column": column,
                "target": target,
            }
        )

    # Model relations enrich descriptions/cardinality and can add an edge only
    # when both endpoint tables are already migration-derived.
    for table_name in sorted(model_meta_by_table):
        for model in model_meta_by_table[table_name]:
            model_name = _slug(model.get("name")) or table_name
            for relation in model.get("relations") or []:
                if not isinstance(relation, dict):
                    continue
                rel_type = _slug(relation.get("type"))
                if rel_type not in {"belongsTo", "hasMany", "hasOne", "belongsToMany"}:
                    continue
                target_name = _relation_target_name(relation.get("target"))
                target_table = model_table_by_name.get(target_name)
                method = _slug(relation.get("method")) or rel_type
                if (
                    not target_table
                    or target_table not in tables
                    or table_name not in tables
                ):
                    unresolved = (table_name, method, target_name)
                    if unresolved not in unresolved_targets:
                        warnings.append(
                            {
                                "code": "unresolved-model-relationship",
                                "message": f"Model relationship {model_name}.{method} could not resolve {target_name} to a schema table.",
                                "table": table_name,
                                "method": method,
                                "target": target_name,
                            }
                        )
                        unresolved_targets.add(unresolved)
                    continue
                source, target = table_name, target_table
                if rel_type == "belongsTo":
                    source, target = target_table, table_name
                cardinality = {
                    "hasOne": "1:1",
                    "belongsTo": "1:N",
                    "hasMany": "1:N",
                    "belongsToMany": "N:M",
                }[rel_type]
                add_edge(
                    source,
                    target,
                    cardinality,
                    [],
                    _relationship_description(rel_type, model_name, method),
                    provenance="eloquent",
                )

    # A pair of foreign keys is the migration-level pivot signal.  Emit one
    # direct N:M edge while retaining the two authoritative FK edges to the
    # pivot table itself.
    pivot_pairs: dict[tuple[str, str], list[tuple[str, str, str]]] = defaultdict(list)
    for pivot, info in tables.items():
        refs = [
            (
                column["name"],
                column["references"]["table"],
                column["references"]["column"],
            )
            for column in info["columns"]
            if column.get("references") and column["references"]["table"] in tables
        ]
        if len(refs) < 2:
            continue
        for left in range(len(refs)):
            for right in range(left + 1, len(refs)):
                first, second = sorted(
                    (refs[left], refs[right]), key=lambda item: item[1]
                )
                pair = (first[1], second[1])
                pivot_pairs[pair].append((pivot, first[0], second[0]))
    for (left_table, right_table), pivots in sorted(pivot_pairs.items()):
        for pivot, left_column, right_column in sorted(pivots):
            add_edge(
                left_table,
                right_table,
                "N:M",
                [{"source": left_column, "target": right_column}],
                f"{left_table} and {right_table} are related through pivot table {pivot}",
                provenance="migration-pivot",
            )

    # Multiple independent relations between the same pair are useful but can
    # be ambiguous in a compact graph; surface that fact explicitly.
    pair_counts: dict[tuple[str, str], int] = defaultdict(int)
    for edge in relationships:
        pair_counts[(edge["source"], edge["target"])] += 1
    for (source, target), count in sorted(pair_counts.items()):
        if count > 1:
            warnings.append(
                {
                    "code": "ambiguous-relationship",
                    "message": f"{source} and {target} have {count} relationship paths; inspect edge details.",
                    "source": source,
                    "target": target,
                }
            )

    positions, cycle_fallback = _layout(list(tables), relationships)
    if cycle_fallback:
        warnings.append(
            {
                "code": "layout-fallback",
                "message": "Cyclic relationships detected; deterministic fallback positions were used.",
            }
        )

    nodes = [
        {
            "id": f"table:{table_name}",
            "type": "databaseTable",
            "position": positions[table_name],
            "data": {
                "table": table_name,
                "columns": tables[table_name]["columns"],
                "searchText": " ".join(
                    [table_name]
                    + [column["name"] for column in tables[table_name]["columns"]]
                ),
            },
        }
        for table_name in sorted(tables)
    ]
    return {
        "nodes": nodes,
        "edges": sorted(relationships, key=lambda edge: edge["id"]),
        "warnings": warnings,
        "source": {
            "framework": framework,
            "analysisId": analysis_id,
            "migrationProvenance": "project_profile.schema",
            "modelProvenance": "project_profile.models",
            "migrationTableCount": len(schema),
            "modelCount": len(models),
            "generatedAt": generated_at or datetime.now(timezone.utc).isoformat(),
        },
    }


__all__ = ["build_schema_visualization"]
