"""Deterministic domain entity model for saved analyses.

The entity model is intentionally built from the persisted profile only.  It
does not inspect a live database and does not require an LLM.  The optional
``enrich_domain_model`` helper is an explicit, session-scoped overlay for
descriptions, behaviours, and otherwise ambiguous relation labels.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SUPPORTED = {"laravel", "symfony", "doctrine"}
_REL_CARDINALITY = {
    "belongsto": "1:N",
    "hasmany": "1:N",
    "hasone": "1:1",
    "belongstomany": "M:N",
    "morphmany": "polymorphic",
    "morphto": "polymorphic",
    "morphtomany": "polymorphic",
    "morphone": "polymorphic",
    "hasmanythrough": "1:N",
    "hasonethrough": "1:1",
    "manytoone": "1:N",
    "onetomany": "1:N",
    "onetoone": "1:1",
    "manytomany": "M:N",
}


def profile_for_saved_analysis(row: dict[str, Any], analysis_store: Any) -> tuple[dict[str, Any], str | None]:
    """Return the saved profile or a same-filename profile-backed sibling."""
    profile = row.get("profile") if isinstance(row.get("profile"), dict) else None
    if profile:
        return profile, None
    filename = _text(row.get("filename"), 500)
    if not filename or not hasattr(analysis_store, "list_by_filename"):
        return {}, None
    try:
        siblings = analysis_store.list_by_filename(filename, limit=25)
    except Exception:
        return {}, None
    for sibling in siblings:
        if str(sibling.get("id") or "") == str(row.get("id") or ""):
            continue
        candidate = sibling.get("profile") if isinstance(sibling.get("profile"), dict) else None
        if candidate:
            return candidate, str(sibling.get("id") or "") or None
    return {}, None


def complete(*args: Any, **kwargs: Any) -> str:
    """Lazy LLM import keeps deterministic model reads side-effect free."""
    from runtime.llm import complete as _complete

    return _complete(*args, **kwargs)


def _text(value: Any, cap: int = 400) -> str:
    return str(value or "").strip()[:cap]


def _framework(profile: dict[str, Any]) -> str:
    value = profile.get("framework")
    if isinstance(value, dict):
        value = value.get("name") or value.get("id")
    return _text(value, 80).lower()


def _short_name(value: Any) -> str:
    return _text(value, 180).split("\\")[-1].split("::")[0]


def _table_for_model(model: dict[str, Any]) -> str:
    table = _text(model.get("table"), 180)
    if table:
        return table
    name = _short_name(model.get("name") or model.get("class"))
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()
    if snake.endswith(("s", "x", "z", "ch", "sh")):
        return snake + "es"
    if snake.endswith("y") and len(snake) > 1 and snake[-2] not in "aeiou":
        return snake[:-1] + "ies"
    return snake + "s" if snake else ""


def _entity_key(name: str, table: str) -> str:
    return table or name


def _type_name(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("type")
    return _text(value, 100) or "unknown"


def _reference(value: Any) -> dict[str, str] | None:
    if isinstance(value, dict):
        table = _text(value.get("table") or value.get("target_table"), 180)
        column = _text(value.get("column") or value.get("target_column"), 120) or "id"
        return {"table": table, "column": column} if table else None
    return None


def _normal_property(raw: dict[str, Any], casts: dict[str, Any] | list[Any] | None = None) -> dict[str, Any] | None:
    name = _text(raw.get("name") or raw.get("property") or raw.get("field"), 160)
    if not name:
        return None
    ref = _reference(raw.get("foreign") or raw.get("references"))
    cast = raw.get("cast_or_enum") or raw.get("cast") or raw.get("enum")
    if cast is None and isinstance(casts, dict):
        cast = casts.get(name)
    elif cast is None and isinstance(casts, list):
        for item in casts:
            if isinstance(item, (list, tuple)) and len(item) >= 2 and str(item[0]) == name:
                cast = item[1]
    primary = bool(raw.get("is_primary", raw.get("primary", raw.get("id", False)))) or name == "id"
    nullable = bool(raw.get("is_nullable", raw.get("nullable", False)))
    foreign = bool(raw.get("is_foreign", raw.get("foreign_key", False))) or ref is not None
    result = {
        "name": name,
        "type": _type_name(raw.get("type") or raw.get("native_type") or raw.get("columnDefinition")),
        "is_primary": primary,
        "is_foreign": foreign,
        "is_nullable": nullable,
        "cast_or_enum": _text(cast, 180) or None,
    }
    # Keep migration facts available to the UI without making them required
    # by the neutral entity contract.
    if ref:
        result["references"] = ref
    for key in ("default", "unique", "indexed"):
        if key in raw:
            result[key] = raw[key]
    return result


def _relationship(raw: dict[str, Any], *, default_target: str = "") -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = _text(raw.get("name") or raw.get("method") or raw.get("property"), 160)
    kind = _text(raw.get("type") or raw.get("relation") or raw.get("association"), 80)
    target = _short_name(raw.get("target_entity") or raw.get("target") or raw.get("targetEntity") or default_target)
    if not target:
        return None
    key = kind.replace("_", "").replace("-", "").lower()
    cardinality = _text(raw.get("cardinality"), 40) or _REL_CARDINALITY.get(key, "unknown")
    result = {
        "name": name or kind or "relation",
        "type": kind or "relationship",
        "target_entity": target,
        "cardinality": cardinality,
        "foreign_key": _text(raw.get("foreign_key") or raw.get("foreignKey"), 120) or None,
    }
    if raw.get("description"):
        result["description"] = _text(raw["description"], 300)
    return result


def _visibility(value: Any) -> str:
    value = _text(value, 20).lower()
    return value if value in {"public", "protected", "private", "internal"} else "public"


def _normal_method(raw: Any, *, owner: str | None = None) -> dict[str, Any] | None:
    if isinstance(raw, str):
        text = _text(raw, 220)
        if not text:
            return None
        return {
            "name": text.split("(", 1)[0].lstrip("+-#~ "),
            "visibility": "public",
            "return_type": "void",
            "parameters": "",
            "owner": owner,
        }
    if not isinstance(raw, dict):
        return None
    name = _text(raw.get("name") or raw.get("method"), 120)
    if not name:
        return None
    params = raw.get("parameters") or raw.get("params") or ""
    if isinstance(params, list):
        params = ", ".join(_text(item, 100) for item in params)
    result = {
        "name": name,
        "visibility": _visibility(raw.get("visibility")),
        "return_type": _type_name(raw.get("return_type") or raw.get("returnType") or "void"),
        "parameters": _text(params, 220),
        "is_static": bool(raw.get("is_static") or raw.get("static")),
    }
    owner_value = _text(raw.get("owner") or owner, 240)
    if owner_value:
        result["owner"] = owner_value
    source_file = _text(raw.get("source_file") or raw.get("file"), 500)
    definition_line = raw.get("definition_line")
    if definition_line is None:
        definition_line = raw.get("line") or raw.get("start_line")
    if source_file:
        result["source_file"] = source_file
    if isinstance(definition_line, int) and definition_line > 0:
        result["definition_line"] = definition_line
    if raw.get("lookup_key"):
        result["lookup_key"] = _text(raw.get("lookup_key"), 300)
    return result


def _normal_class_property(raw: Any, casts: Any = None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    prop = _normal_property(raw, casts)
    if not prop:
        return None
    prop["visibility"] = _visibility(raw.get("visibility"))
    prop["is_static"] = bool(raw.get("is_static") or raw.get("static"))
    return prop


def _uml_visibility(value: Any) -> str:
    return {"public": "+", "protected": "#", "private": "-", "internal": "~"}.get(_visibility(value), "+")


def _method_signature(method: Any) -> str:
    normalized = _normal_method(method)
    if not normalized:
        return _text(method, 220)
    static = "{static} " if normalized.get("is_static") else ""
    params = normalized.get("parameters") or ""
    return f"{_uml_visibility(normalized.get('visibility'))}{static}{normalized['name']}({params}): {normalized.get('return_type') or 'void'}"


def _behavior_metadata(method: dict[str, Any], owner: str) -> dict[str, Any]:
    """Attach stable source identity without changing the legacy method shape."""
    normalized = _normal_method(method, owner=owner) or {}
    name = str(normalized.get("name") or "method")
    params = str(normalized.get("parameters") or "")
    lookup_key = _text(normalized.get("lookup_key"), 300) or (
        f"{owner}::{name}" if normalized.get("source_file") or normalized.get("definition_line") else None
    )
    behavior_key = f"{owner}::{name}({params})"
    source_file = _text(normalized.get("source_file"), 500) or None
    if source_file and (source_file.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", source_file)):
        parts = [part for part in source_file.replace("\\", "/").split("/") if part]
        markers = {"app", "src", "routes", "database", "config", "resources", "public", "bin", "tests", "lib", "modules", "packages"}
        source_file = "/".join(parts[next((i for i, part in enumerate(parts) if part.casefold() in markers), max(0, len(parts) - 1)):]) if parts else None
    line = normalized.get("definition_line")
    has_identity = bool(lookup_key and source_file and isinstance(line, int) and line > 0)
    return {
        **normalized,
        "signature": _method_signature(normalized),
        "owner": owner,
        "source_file": source_file,
        "definition_line": line if isinstance(line, int) and line > 0 else None,
        "lookup_key": lookup_key,
        "behavior_key": behavior_key,
        "has_source_identity": has_identity,
        "usage_available": has_identity,
    }


def _layout(names: list[str], edges: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    names = sorted(set(names))
    out = {name: set() for name in names}
    indegree = {name: 0 for name in names}
    for edge in edges:
        source, target = edge.get("sourceEntity"), edge.get("targetEntity")
        if source in out and target in out and source != target and target not in out[source]:
            out[source].add(target)
            indegree[target] += 1
    layer = {name: 0 for name in names}
    queue = deque(sorted(name for name in names if indegree[name] == 0))
    seen: set[str] = set()
    while queue:
        source = queue.popleft()
        seen.add(source)
        for target in sorted(out[source]):
            layer[target] = max(layer[target], layer[source] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    # Cycles remain stable by appending them in name order.
    next_layer = max(layer.values(), default=0) + 1
    for name in names:
        if name not in seen:
            layer[name] = next_layer
            next_layer += 1
    grouped: dict[int, list[str]] = defaultdict(list)
    for name in names:
        grouped[layer[name]].append(name)
    return {
        name: {"x": layer_no * 320, "y": row * 220}
        for layer_no, group in sorted(grouped.items())
        for row, name in enumerate(sorted(group))
    }


def _node_id(name: str) -> str:
    return "node-" + re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_").lower()


def _plantuml_identifier(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "Entity"
    if value[0].isdigit():
        value = "E_" + value
    return value


def _plantuml(model: dict[str, Any]) -> str:
    aliases = {entity["name"]: _plantuml_identifier(entity["name"]) for entity in model["entities"]}
    lines = ["@startuml", "!theme plain", "skinparam linetype ortho", ""]
    for entity in model["entities"]:
        name = entity["name"]
        display = _text(name, 180).replace('"', '\\"')
        keyword = "interface" if str(entity.get("stereotype", "")).lower() == "interface" else "class"
        stereotype = entity.get("stereotype") or "Entity"
        lines.append(f'{keyword} "{display}" as {aliases[name]} <<{stereotype}>> {{')
        for prop in entity.get("properties", []):
            nullable = " = null" if prop.get("is_nullable") else ""
            lines.append(f"  {_uml_visibility(prop.get('visibility'))}{prop.get('type') or 'unknown'}: {prop['name']}{nullable}")
        for method in entity.get("methods", []):
            lines.append(f"  {_method_signature(method)}")
        lines.append("}")
        lines.append("")
    for entity in model["entities"]:
        source = aliases.get(entity["name"])
        for parent in entity.get("extends", []) or []:
            target = aliases.get(_short_name(parent))
            if source and target:
                lines.append(f"{source} --|> {target}")
        for contract in entity.get("implements", []) or []:
            target = aliases.get(_short_name(contract))
            if source and target:
                lines.append(f"{source} ..|> {target}")
    for edge in model.get("relationships", []):
        if edge.get("type") in {"inheritance", "realization"}:
            continue
        source = aliases.get(edge.get("sourceEntity"))
        target = aliases.get(edge.get("targetEntity"))
        if not source or not target:
            continue
        cardinality = edge.get("cardinality") or "unknown"
        left, right = {"1:1": ('"1"', '"1"'), "1:N": ('"1"', '"0..*"'), "M:N": ('"*"', '"*"'), "N:M": ('"*"', '"*"'), "polymorphic": ('"*"', '"*"')}.get(cardinality, ('"?"', '"?"'))
        label = _text(edge.get("description") or edge.get("name") or edge.get("type"), 120).replace('"', '\\"')
        lines.append(f'{source} {left} --> {right} {target} : {label}')
    lines.extend(["", "@enduml"])
    return "\n".join(lines)


def build_domain_model(profile: dict[str, Any] | None, analysis_id: str, *, project_id: str | None = None, generated_at: str | None = None, include_domain_concepts: bool = True) -> dict[str, Any]:
    """Build the stable framework-neutral entity model response."""
    profile = profile or {}
    framework = _framework(profile)
    framework_public = "symfony" if framework == "doctrine" else framework
    warnings: list[dict[str, Any]] = []
    if framework_public not in {"laravel", "symfony"}:
        warnings.append({"code": "unsupported-framework", "message": f"Entity model extraction is not available for framework {framework or 'unknown'!r}."})

    entities: dict[str, dict[str, Any]] = {}
    by_table: dict[str, str] = {}
    by_name: dict[str, str] = {}

    def ensure(name: str, table: str = "", namespace: str = "", provenance: str = "project_profile") -> dict[str, Any]:
        name = _short_name(name) or _short_name(table) or "Entity"
        key = _entity_key(name, table)
        if key not in entities:
            entities[key] = {
                "name": name,
                "namespace": namespace,
                "table": table or name,
                "framework": framework_public or "unknown",
                "stereotype": "Entity",
                "properties": [],
                "relationships": [],
                "methods": [],
                "scopes_or_behaviors": [],
                "extends": [],
                "implements": [],
                "provenance": [provenance],
            }
        item = entities[key]
        if table:
            by_table.setdefault(table, item["name"])
        by_name.setdefault(name, item["name"])
        return item

    models = [item for item in (profile.get("models") or []) if isinstance(item, dict)]
    doctrine = [item for item in (profile.get("doctrine_entities") or profile.get("entities") or []) if isinstance(item, dict)]
    for raw in models + doctrine:
        table = _text(raw.get("table") or raw.get("table_name"), 180) or _table_for_model(raw)
        fqn = _text(raw.get("name") or raw.get("class") or raw.get("entity"), 220)
        namespace = _text(raw.get("namespace"), 180) or (_text(fqn.rsplit("\\", 1)[0], 180) if "\\" in fqn else "")
        item = ensure(fqn or table, table, namespace, "model_metadata")
        casts = raw.get("casts")
        fields = raw.get("properties") or raw.get("fields") or raw.get("columns") or []
        for field in fields:
            if isinstance(field, dict):
                prop = _normal_property(field, casts)
                if prop:
                    _merge_property(item, prop)
        primary_key = _text(raw.get("primary_key") or raw.get("primaryKey"), 120)
        if primary_key:
            for prop in item["properties"]:
                if prop["name"] == primary_key:
                    prop["is_primary"] = True
        for field in (raw.get("fillable") or []):
            prop = _normal_property({"name": field, "type": "unknown"}, casts)
            if prop:
                _merge_property(item, prop)
        for relation in raw.get("relations") or raw.get("associations") or []:
            rel = _relationship(relation)
            if rel:
                item["relationships"].append(rel)
                if not any((_normal_method(method) or {}).get("name") == rel["name"] for method in item["methods"]):
                    item["methods"].append(
                        {
                            "name": rel["name"],
                            "visibility": "public",
                            "return_type": _short_name(rel.get("target_entity")),
                            "parameters": "",
                            "synthetic": True,
                        }
                    )
        for prop_raw in raw.get("properties") or []:
            prop = _normal_class_property(prop_raw, casts)
            if prop:
                _merge_property(item, prop)
        for method_raw in raw.get("methods") or []:
            method = _normal_method(method_raw)
            if method:
                item["methods"].append(method)
        item["extends"].extend(_short_name(value) for value in (raw.get("extends") or []) if _short_name(value))
        item["implements"].extend(_short_name(value) for value in (raw.get("implements") or []) if _short_name(value))
        behavior_values = raw.get("scopes_or_behaviors") or raw.get("scopes") or raw.get("behaviors") or []
        item["scopes_or_behaviors"].extend(_text(value, 160) for value in behavior_values if _text(value, 160))
        item["methods"].extend(
            {"name": f"scope{_text(value, 120)}", "visibility": "public", "return_type": "void", "parameters": "...", "synthetic": True}
            for value in behavior_values
            if _text(value, 120)
        )

    # Materialize parent classes and interfaces as diagram nodes so inheritance
    # and realization arrows always have visible endpoints.
    for item in list(entities.values()):
        for parent in item.get("extends", []) or []:
            ensure(parent, "", "", "class_relationship")
        for contract in item.get("implements", []) or []:
            interface = ensure(contract, "", "", "interface_relationship")
            interface["stereotype"] = "interface"

    for raw_table in (profile.get("schema") or []):
        if not isinstance(raw_table, dict):
            continue
        table = _text(raw_table.get("table") or raw_table.get("table_name"), 180)
        if not table:
            continue
        model_name = by_table.get(table)
        if not model_name:
            warnings.append({
                "code": "schema-without-entity",
                "message": f"Migration table {table} has no matching model metadata; omitted from the class diagram.",
                "table": table,
            })
            continue
        item = next(
            (candidate for candidate in entities.values() if candidate.get("name") == model_name),
            None,
        )
        if item is None:
            continue
        item["provenance"].append("schema_metadata")
        for raw_col in raw_table.get("columns") or raw_table.get("fields") or []:
            if isinstance(raw_col, dict):
                prop = _normal_property(raw_col)
                if prop:
                    _merge_property(item, prop)

    # Normalize model relation targets after all schema/model entities exist.
    for item in entities.values():
        normalized: list[dict[str, Any]] = []
        for relation in item["relationships"]:
            target_ref = _short_name(relation.get("target_entity"))
            target = (
                by_name.get(target_ref)
                or by_table.get(_text(relation.get("target_entity"), 180))
                or by_table.get(_table_for_model({"name": target_ref}))
            )
            if not target:
                warnings.append({"code": "unresolved-relationship", "message": f"{item['name']}.{relation.get('name')} references unknown entity {relation.get('target_entity')}."})
                continue
            relation["target_entity"] = target
            _add_relation(item, relation, normalized)
        item["relationships"] = normalized
        item["relationships"] = sorted(
            item["relationships"],
            key=lambda value: (
                _text(value.get("target_entity"), 180),
                _text(value.get("name"), 160),
                _text(value.get("type"), 80),
            ),
        )
        item["scopes_or_behaviors"] = sorted(set(item["scopes_or_behaviors"]))
        unique_methods: dict[str, dict[str, Any]] = {}
        for method in item.get("methods", []):
            normalized = _normal_method(method, owner=item["name"])
            if normalized:
                name = normalized["name"]
                prior = unique_methods.get(name)
                # Prefer the parser's concrete declaration over a synthetic
                # relationship method with a void return type.
                if prior is None or (prior.get("return_type") == "void" and normalized.get("return_type") != "void"):
                    unique_methods[name] = normalized
        owner_fqn = f"{item.get('namespace')}\\{item['name']}" if item.get("namespace") else item["name"]
        item["methods"] = [
            _behavior_metadata(unique_methods[key], owner_fqn)
            for key in sorted(unique_methods)
        ]
        item["extends"] = sorted(set(item.get("extends", [])))
        item["implements"] = sorted(set(item.get("implements", [])))
        item["properties"] = sorted(item["properties"], key=lambda value: value["name"])
        item["provenance"] = sorted(set(item["provenance"]))

    if framework_public == "symfony" and not doctrine and not (profile.get("schema") or []):
        warnings.append({"code": "missing-doctrine-metadata", "message": "No Doctrine entity or migration metadata was found; entity model is empty."})
    if framework_public == "laravel" and not models and not (profile.get("schema") or []):
        warnings.append({"code": "missing-laravel-metadata", "message": "No Laravel model or migration metadata was found; entity model is empty."})

    entity_list = sorted(entities.values(), key=lambda value: (value["table"], value["name"]))
    edges: list[dict[str, Any]] = []
    edge_by_pair: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in entity_list:
        for relation in item["relationships"]:
            source_name, target_name = item["name"], relation["target_entity"]
            source_port = target_port = None
            key = (source_name, target_name, relation.get("cardinality", "unknown"))
            existing = edge_by_pair.get(key)
            if existing:
                relation_name = _text(relation.get("name"), 100)
                names = [part.strip() for part in str(existing.get("name") or "").split(",") if part.strip()]
                if relation_name and relation_name not in names:
                    existing["name"] = ", ".join(sorted(names + [relation_name]))
                continue
            edge = {"id": f"relation:{_node_id(source_name)}:{_node_id(target_name)}:{_node_id(relation.get('name') or 'relation')}", "sourceEntity": source_name, "targetEntity": target_name, "name": relation.get("name"), "type": relation.get("type"), "cardinality": relation.get("cardinality"), "foreign_key": relation.get("foreign_key"), "sourcePort": source_port, "targetPort": target_port, "provenance": relation.get("provenance", "deterministic")}
            edge_by_pair[key] = edge
            edges.append(edge)
    positions = _layout([item["name"] for item in entity_list], edges)
    edges = sorted(edges, key=lambda value: value["id"])
    nodes = []
    for item in entity_list:
        pos = positions.get(item["name"], {"x": 0, "y": 0})
        fields = [{"name": prop["name"], "type": prop["type"], "visibility": prop.get("visibility", "public"), "isPk": prop["is_primary"], "isFk": prop["is_foreign"], "isNullable": prop["is_nullable"]} for prop in item["properties"]]
        methods = list(item.get("methods", []))
        node = {"id": _node_id(item["name"]), "shape": "custom-vue-entity", "x": pos["x"], "y": pos["y"], "width": 280, "height": max(150, 92 + 24 * (len(fields) + len(methods))), "ports": {"items": []}, "data": {"name": item["name"], "table": item["table"], "framework": item["framework"], "stereotype": item.get("stereotype", "Entity"), "fields": fields, "methods": methods, "namespace": item.get("namespace", ""), "extends": item.get("extends", []), "implements": item.get("implements", [])}, "position": pos}
        nodes.append(node)
    x6_edges = []
    for item in entity_list:
        for parent in item.get("extends", []) or []:
            if parent in by_name:
                edges.append({"id": f"extends:{_node_id(item['name'])}:{_node_id(parent)}", "sourceEntity": item["name"], "targetEntity": by_name[parent], "name": "extends", "type": "inheritance", "cardinality": "", "provenance": "class_metadata"})
        for contract in item.get("implements", []) or []:
            if contract in by_name:
                edges.append({"id": f"implements:{_node_id(item['name'])}:{_node_id(contract)}", "sourceEntity": item["name"], "targetEntity": by_name[contract], "name": "implements", "type": "realization", "cardinality": "", "provenance": "class_metadata"})
    for edge in sorted(edges, key=lambda value: value["id"]):
        label = ": ".join(filter(None, [_text(edge.get("cardinality"), 40), _text(edge.get("name"), 100)]))
        marker = "block" if edge.get("type") == "inheritance" else ("open" if edge.get("type") == "realization" else "classic")
        x6_edges.append({"id": edge["id"], "source": _node_id(edge["sourceEntity"]), "target": _node_id(edge["targetEntity"]), "sourcePort": edge.get("sourcePort"), "targetPort": edge.get("targetPort"), "data": edge, "labels": ([{"attrs": {"text": {"text": label, "fill": "#bac2de", "fontSize": 11}, "rect": {"fill": "#181825", "rx": 3, "ry": 3}}, "position": 0.5}] if label else []), "router": {"name": "manhattan"}, "connector": {"name": "rounded", "args": {"radius": 8}}, "attrs": {"line": {"stroke": "#a6e3a1" if edge.get("type") == "inheritance" else "#89b4fa", "strokeWidth": 1.5, "strokeDasharray": "6 4" if edge.get("type") == "realization" else "", "targetMarker": marker}}})

    result = {"version": "1.0", "status": "deterministic", "entities": entity_list, "relationships": edges, "plantuml": "", "antv_x6": {"nodes": nodes, "edges": x6_edges}, "source": {"analysis_id": analysis_id, "framework": framework_public or "unknown", "project_id": project_id, "provenance": ["project_profile.schema", "project_profile.models"], "generated_at": generated_at or datetime.now(timezone.utc).isoformat()}, "warnings": warnings}
    if include_domain_concepts and project_id:
        try:
            from projects.domain.store import concept_store
            concepts = concept_store.list_concepts(project_id, limit=500)
            result["source"]["domain_concept_count"] = len(concepts)
            result["source"]["provenance"].append("domain_concept_store")
            result["domain_concepts"] = [{"identity_key": _text(item.get("identity_key"), 200), "canonical_name": _text(item.get("canonical_name"), 200), "description": _text(item.get("description"), 500) or None} for item in concepts if isinstance(item, dict)]
        except Exception as exc:  # domain data is enhancement-only
            result["warnings"].append({"code": "domain-concepts-unavailable", "message": str(exc)[:200]})
    result["plantuml"] = _plantuml(result)
    return result


def _merge_property(entity: dict[str, Any], prop: dict[str, Any]) -> None:
    for prior in entity["properties"]:
        if prior["name"] == prop["name"]:
            for key, value in prop.items():
                if key not in prior or prior[key] in (None, "unknown", False):
                    prior[key] = value
            return
    entity["properties"].append(prop)


def _add_relation(entity: dict[str, Any], relation: dict[str, Any], target: list[dict[str, Any]] | None = None) -> None:
    target = target if target is not None else entity["relationships"]
    identity = (relation.get("name"), relation.get("target_entity"), relation.get("type"), relation.get("cardinality"))
    if not any((prior.get("name"), prior.get("target_entity"), prior.get("type"), prior.get("cardinality")) == identity for prior in target):
        target.append(relation)


def _extract_json(text: str) -> Any:
    clean = (text or "").strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.I | re.S).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        start, end = clean.find("{"), clean.rfind("}")
        if start >= 0 and end > start:
            return json.loads(clean[start : end + 1])
    raise ValueError("LLM returned malformed JSON")


def enrich_domain_model(model: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Explicitly enrich a deterministic model; authoritative facts survive."""
    profile = profile or {}
    try:
        prompt_path = Path(__file__).resolve().parents[3] / "prompts" / "plantuml.MD"
        template = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else "Return the requested entity model JSON."
    except OSError:
        template = "Return the requested entity model JSON."
    context = json.dumps({"entities": model.get("entities", []), "relationships": model.get("relationships", []), "profile": profile}, ensure_ascii=False, sort_keys=True, default=str)[:28000]
    prompt = f"{template[:10000]}\n\nAnalyze this bounded saved-analysis context. Return JSON only with optional entities (name, description, scopes_or_behaviors, relationships with name/source_entity/target_entity/label).\nCONTEXT:\n{context}"
    raw = complete(prompt, system="You enrich a deterministic domain model. Never change tables, properties, keys, or authoritative relationships.", max_tokens=5000)
    parsed = _extract_json(raw)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("entities", []), list):
        raise ValueError("LLM response does not contain an entities array")
    enriched = json.loads(json.dumps(model, ensure_ascii=False, default=str))
    by_name = {entity["name"].casefold(): entity for entity in enriched.get("entities", [])}
    for raw_entity in parsed.get("entities", []):
        if not isinstance(raw_entity, dict):
            continue
        entity = by_name.get(_short_name(raw_entity.get("name")).casefold())
        if entity is None:
            continue
        if not entity.get("description") and _text(raw_entity.get("description"), 500):
            entity["description"] = _text(raw_entity["description"], 500)
        values = raw_entity.get("scopes_or_behaviors") or raw_entity.get("behaviors") or []
        entity["scopes_or_behaviors"] = sorted(set(entity.get("scopes_or_behaviors", []) + [_text(value, 160) for value in values if _text(value, 160)]))
        for raw_rel in raw_entity.get("relationships") or []:
            if not isinstance(raw_rel, dict):
                continue
            source = _short_name(raw_rel.get("source_entity") or raw_entity.get("name"))
            target = _short_name(raw_rel.get("target_entity") or raw_rel.get("target"))
            for relation in enriched.get("relationships", []):
                same_direction = relation.get("sourceEntity") == source and relation.get("targetEntity") == target
                reverse_direction = relation.get("sourceEntity") == target and relation.get("targetEntity") == source
                if (same_direction or reverse_direction) and not relation.get("description"):
                    label = _text(raw_rel.get("label") or raw_rel.get("description"), 300)
                    if label:
                        relation["description"] = label
    enriched["status"] = "enriched"
    enriched.setdefault("source", {}).setdefault("provenance", []).append("llm_enrichment")
    enriched["warnings"] = list(enriched.get("warnings") or [])
    enriched["plantuml"] = _plantuml(enriched)
    return enriched


__all__ = ["build_domain_model", "enrich_domain_model"]
