"""Revision-scoped, deterministic Clean Code design measurements."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import yaml

from telemetry.snapshot import Snapshot, validate_project_id, validate_revision_id

RUBRIC_PATH = Path(__file__).parent / "rubrics" / "clean-code-rubric-1.yml"


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load_rubric(path: Path = RUBRIC_PATH) -> tuple[dict, str]:
    try:
        data = path.read_bytes()
        rubric = yaml.safe_load(data)
        if not isinstance(rubric, dict) or rubric.get("version") != "clean-code-rubric-1" or rubric.get("metric_schema") != "code-metrics-1" or rubric.get("score_method") != "design-metrics-1":
            raise ValueError("invalid rubric identity")
        limits = rubric["limits"]
        for key in ("candidates_per_principle", "candidates_total", "body_chars", "reason_chars", "max_chunks", "completion_tokens", "request_token_limit_fallback"):
            if type(limits[key]) is not int or limits[key] <= 0:
                raise ValueError("invalid rubric limit")
        if not isinstance(rubric["rules"], list) or not rubric["rules"]:
            raise ValueError("missing rules")
        ids = set()
        metrics = {"lcom4", "fan_out", "method_count", "public_interface_size", "type_switch",
                   "concrete_dependencies", "unsupported_overrides", "infrastructure_instantiations",
                   "max_cyclomatic", "max_nesting", "max_params", "max_method_loc", "loc"}
        for rule in rubric["rules"]:
            if (not isinstance(rule, dict) or not isinstance(rule.get("id"), str) or not rule["id"] or rule["id"] in ids
                    or not isinstance(rule.get("principle"), str) or not rule["principle"] or rule.get("metric") not in metrics
                    or type(rule.get("threshold")) not in (int, float) or not math.isfinite(rule["threshold"]) or rule["threshold"] < 0
                    or type(rule.get("penalty")) not in (int, float) or not math.isfinite(rule["penalty"]) or not 0 <= rule["penalty"] <= 100
                    or rule.get("judge") not in ("model", "none") or not isinstance(rule.get("applies_to"), list)
                    or not rule["applies_to"] or not set(rule["applies_to"]) <= {"class", "trait", "enum", "interface"}
                    or type(rule.get("min_method_count", 0)) is not int or rule.get("min_method_count", 0) < 0
                    or (rule["judge"] == "model" and (not isinstance(rule.get("question"), str) or not rule["question"].strip()))):
                raise ValueError("invalid rule")
            ids.add(rule["id"])
        if (not isinstance(rubric["unsupported_exceptions"], list) or not all(isinstance(x, str) for x in rubric["unsupported_exceptions"])
                or not isinstance(rubric["infrastructure"], dict)
                or any(not isinstance(rubric["infrastructure"].get(k), list)
                       or not all(isinstance(x, str) for x in rubric["infrastructure"][k]) for k in ("names", "prefixes"))):
            raise ValueError("invalid rubric configuration")
        return rubric, hashlib.sha256(data).hexdigest()
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        raise ValueError("The Clean Code rubric is missing or invalid; restore it and retry the assessment.") from exc


def _name(value: Any) -> str:
    return value.strip().lstrip("\\").casefold() if isinstance(value, str) else ""


def read_revision_chunks(project_id: str, revision_id: str, snapshot: dict, files: list[str], limit: int) -> tuple[list[dict], bool, str | None]:
    validate_project_id(project_id)
    validate_revision_id(revision_id)
    try:
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        from index.llamaindex_layer import CodePropertyGraph
        collection = Snapshot.from_row(project_id, snapshot).collection
        if (snapshot.get("state") != "ready" or snapshot.get("project_id") != project_id
                or snapshot.get("revision_id") != revision_id or snapshot.get("collection_name") != collection):
            return [], False, "The selected revision does not have a ready matching index."
        from telemetry.snapshot import source_filter
        from index.embedding_rebuild import active_vector_collection
        vector_collection = active_vector_collection(collection)
        if vector_collection is None:
            return [], False, "Rebuild Cohere embeddings for this revision in Knowledge Base before assessment."
        identity = {"project_id": project_id, "revision_id": revision_id, **source_filter(snapshot),
                    "source_archive_sha256": snapshot["source_archive_sha256"], "dirty": False, "collection": vector_collection}
        conditions = Filter(must=[FieldCondition(key=k, match=MatchValue(value=v)) for k, v in identity.items()])
        qdrant = CodePropertyGraph()._get_qdrant()
        rows, offset = [], None
        while True:
            records, offset = qdrant.scroll(collection_name=vector_collection, scroll_filter=conditions,
                                            limit=min(256, limit + 1 - len(rows)), offset=offset,
                                            with_payload=True, with_vectors=False)
            rows.extend(dict(record.payload or {}) for record in records)
            if len(rows) > limit or offset is None:
                break
        partial = len(rows) > limit
        allowed = set(files)
        rows = [row for row in rows[:limit] if all(row.get(k) == v for k, v in identity.items())
                and (row.get("file_path") or row.get("file")) in allowed]
        return rows, partial, None
    except Exception:
        return [], False, "The selected revision's indexed chunks could not be read."


def _components(methods: list[dict], owner: str) -> int:
    methods = [m for m in methods if _name(m.get("method_name") or m.get("name")) != "__construct"]
    parent = list(range(len(methods)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    names = {_name(m.get("method_name") or m.get("name")): i for i, m in enumerate(methods)}
    properties = [set(m.get("reads_properties") or []) | set(m.get("writes_properties") or []) for m in methods]
    for i, method in enumerate(methods):
        for j in range(i):
            if properties[i] & properties[j]:
                parent[find(i)] = find(j)
        for call in method.get("called_symbols") or []:
            receiver, sep, name = str(call).rpartition("::")
            if sep and _name(receiver) in {"$this", "this", "self", "static", _name(owner)} and _name(name) in names:
                parent[find(i)] = find(names[_name(name)])
    return len({find(i) for i in range(len(methods))})


def build_units(chunks: list[dict], rubric: dict) -> list[dict]:
    schema = rubric["metric_schema"]
    declarations = { _name(c.get("fq_symbol_name")): c for c in chunks
                    if c.get("metric_schema") == schema and c.get("node_type") in {"class", "trait", "enum", "interface"} and _name(c.get("fq_symbol_name")) }
    methods: dict[str, list[dict]] = {}
    properties: dict[str, list[str]] = {}
    for c in chunks:
        if c.get("metric_schema") == schema and c.get("node_type") == "method":
            methods.setdefault(_name(c.get("parent_symbol")), []).append(c)
        if c.get("node_type") == "property_group" and c.get("parent_symbol"):
            properties.setdefault(_name(c["parent_symbol"]), []).append(str(c.get("signature") or c.get("symbol_name") or ""))
    units = []
    for owner, declaration in declarations.items():
        ms = sorted(methods.get(owner, []), key=lambda m: (m.get("start_line", 0), _name(m.get("method_name"))))
        dependencies = declaration.get("constructor_dependencies") or []
        related_names = list(declaration.get("implements") or []) + list(declaration.get("extends") or []) + [d.get("type") for d in dependencies if isinstance(d, dict)]
        def resolve(name):
            key = _name(name)
            if key in declarations:
                return key
            imports = declaration.get("imports") or []
            imported = next((_name(item) for item in imports if _name(item).rsplit("\\", 1)[-1] == key), None)
            namespaced = _name(str(declaration.get("namespace") or "") + "\\" + str(name or ""))
            return imported if imported in declarations else namespaced
        related = [declarations[resolved] for name in related_names if (resolved := resolve(name)) in declarations]
        concrete = [d for d in dependencies if isinstance(d, dict) and (target := declarations.get(resolve(d.get("type"))))
                    and target.get("node_type") == "class" and "abstract" not in (target.get("modifiers") or [])]
        contracts = {_name(m.get("method_name")) for item in related for m in methods.get(_name(item.get("fq_symbol_name")), [])}
        unsupported = {_name(x).rsplit("\\", 1)[-1] for x in rubric["unsupported_exceptions"]}
        override = [m for m in ms if _name(m.get("method_name")) in contracts and any(_name(x).rsplit("\\", 1)[-1] in unsupported for x in m.get("throws") or [])]
        infrastructure = rubric["infrastructure"]
        infra = [m for m in ms if any(_name(x) in infrastructure["names"] or any(_name(x).startswith(_name(p)) for p in infrastructure["prefixes"]) for x in m.get("instantiates") or [])]
        classes = {_name(d.get("type")) for d in dependencies if isinstance(d, dict)}
        for m in ms:
            classes.update(_name(x) for x in m.get("instantiates") or [])
            classes.update(_name(str(x).rpartition("::")[0]) for x in m.get("called_symbols") or [] if "::" in str(x))
        classes -= {"", owner, "self", "static", "$this", "this", "__unknown", "__function", "__global__"}
        maximum = lambda key: max((int(m.get(key) or 0) for m in ms), default=0)
        metrics = {"lcom4": _components(ms, owner), "fan_out": len(classes), "method_count": len(ms),
                   "public_interface_size": sum(m.get("visibility") in (None, "public") for m in ms),
                   "type_switch": maximum("type_checks"), "concrete_dependencies": len(concrete),
                   "unsupported_overrides": len(override), "infrastructure_instantiations": len(infra),
                   "max_cyclomatic": maximum("cyclomatic"), "max_nesting": maximum("max_nesting"),
                   "max_params": maximum("param_count"), "max_method_loc": maximum("loc"), "loc": int(declaration.get("loc") or 1)}
        units.append({"declaration": declaration, "methods": ms, "related": related, "metrics": metrics,
                      "properties": sorted(properties.get(owner, [])),
                      "flagged": {"type_switch": max(ms, key=lambda m: m.get("type_checks") or 0, default=None),
                                  "unsupported_overrides": override[0] if override else None,
                                  "infrastructure_instantiations": infra[0] if infra else None}})
    return sorted(units, key=lambda u: (u["declaration"].get("file_path", ""), u["declaration"].get("start_line", 0), u["declaration"].get("fq_symbol_name", "")))


def select_candidates(project_id: str, revision_id: str, units: list[dict], rubric: dict) -> tuple[list[dict], dict]:
    candidates, counts = [], {}
    for rule in rubric["rules"]:
        if rule["judge"] != "model":
            continue
        principle = rule["principle"]
        counts.setdefault(principle, {"exceeding": 0, "judged": 0, "violations": 0, "acceptable": 0, "uncertain": 0, "unjudged": 0})
        exceeding = [u for u in units if u["declaration"]["node_type"] in rule["applies_to"] and u["metrics"].get(rule["metric"], 0) > rule["threshold"] and u["metrics"]["method_count"] >= rule.get("min_method_count", 0)]
        exceeding.sort(key=lambda u: (-(u["metrics"][rule["metric"]] / rule["threshold"] if rule["threshold"] else float("inf")), u["declaration"].get("file_path", ""), u["declaration"].get("start_line", 0), u["declaration"].get("fq_symbol_name", "")))
        counts[principle]["exceeding"] += len(exceeding)
        for unit in exceeding:
            if sum(c["principle"] == principle for c in candidates) >= rubric["limits"]["candidates_per_principle"] or len(candidates) >= rubric["limits"]["candidates_total"]:
                counts[principle]["unjudged"] += 1
                continue
            dec = unit["declaration"]
            symbol = dec["fq_symbol_name"]
            flagged = unit["flagged"].get(rule["metric"])
            body = (flagged or {}).get("code", "")
            declaration_line = str(dec.get("code") or dec.get("signature") or "").splitlines()
            card = {"symbol": symbol, "declaration": declaration_line[0] if declaration_line else "",
                    "methods": [m.get("signature", "") for m in unit["methods"]],
                    "properties": unit["properties"],
                    "constructor_dependencies": dec.get("constructor_dependencies") or [],
                    "metric": rule["metric"], "value": unit["metrics"][rule["metric"]], "threshold": rule["threshold"],
                    "flagged_method": (flagged or {}).get("signature"), "body": body[:rubric["limits"]["body_chars"]],
                    "body_truncated": len(body) > rubric["limits"]["body_chars"],
                    "related": [{"symbol": item.get("fq_symbol_name"), "signature": item.get("signature"),
                                 "content_hash": item.get("content_hash"),
                                 "methods": [m.get("signature", "") for m in units_by_name(units, item).get("methods", [])]} for item in unit["related"]],
                    "content_hash": dec.get("content_hash"), "method_hashes": [m.get("content_hash") for m in unit["methods"]]}
            evidence_id = "ev_" + digest([project_id, revision_id, rubric["version"], rule["id"], symbol])[:24]
            candidates.append({"evidence_id": evidence_id, "principle": principle, "rule_id": rule["id"], "symbol": symbol,
                               "file": dec.get("file_path"), "line": (flagged or dec).get("start_line"),
                               "card": card, "card_hash": digest(card), "question": rule["question"]})
    return candidates, counts


def units_by_name(units: list[dict], item: dict) -> dict:
    return next((u for u in units if u["declaration"] is item), {})


def score_design(units: list[dict], candidates: list[dict], verdicts: dict, counts: dict, rubric: dict, rubric_hash: str, partial: bool) -> tuple[dict, dict, list[dict]]:
    by_unit_rule = {(c["symbol"], c["rule_id"]): verdicts[c["evidence_id"]] for c in candidates}
    unit_scores = []
    for unit in units:
        declaration, metrics = unit["declaration"], unit["metrics"]
        score = 100
        for rule in rubric["rules"]:
            if (declaration["node_type"] not in rule["applies_to"] or metrics.get(rule["metric"], 0) <= rule["threshold"]
                    or metrics["method_count"] < rule.get("min_method_count", 0)):
                continue
            verdict = by_unit_rule.get((declaration["fq_symbol_name"], rule["id"]))
            if rule["judge"] == "none" or (verdict and verdict["verdict"] == "violation"):
                score -= rule["penalty"]
        unit_scores.append({"symbol": declaration["fq_symbol_name"], "file": declaration.get("file_path"),
                            "line": declaration.get("start_line"), "loc": max(1, metrics["loc"]),
                            "score": max(0, score), "metrics": metrics})
    total_loc = sum(u["loc"] for u in unit_scores)
    score = int(sum(u["score"] * u["loc"] for u in unit_scores) / total_loc + 0.5)
    observations = []
    persisted_candidates = []
    for candidate in candidates:
        verdict = verdicts[candidate["evidence_id"]]
        principle = counts[candidate["principle"]]
        principle["judged"] += 1
        principle[{"violation": "violations", "acceptable": "acceptable", "uncertain": "uncertain"}[verdict["verdict"]]] += 1
        persisted = {k: v for k, v in candidate.items() if k not in {"card", "question"}}
        persisted_candidates.append(persisted | verdict)
        if verdict["verdict"] == "violation":
            observations.append({"evidence_id": candidate["evidence_id"], "kind": "finding", "category": "clean_code",
                                 "rule": candidate["rule_id"], "file": candidate["file"], "line": candidate["line"],
                                 "message": verdict["reason"], "severity": "INFO", "tool": "design-metrics-1",
                                 "tool_version": rubric["version"], "design_principle": candidate["principle"]})
    uncertain = partial or any(c["uncertain"] or c["unjudged"] for c in counts.values())
    clean_hash = digest({"metric_schema": rubric["metric_schema"], "rubric_version": rubric["version"],
                         "rubric_hash": rubric_hash, "candidates": [{k: v for k, v in c.items() if k != "cache_hit"} for c in persisted_candidates],
                         "unit_scores": unit_scores, "score": score})
    provenance = {"status": "partial" if partial else "complete", "metric_schema": rubric["metric_schema"],
                  "rubric_version": rubric["version"], "rubric_hash": rubric_hash,
                  "rules": [{k: r[k] for k in ("id", "principle", "metric", "threshold", "penalty", "judge")} for r in rubric["rules"]],
                  "limits": rubric["limits"], "candidates": persisted_candidates, "principles": counts,
                  "unit_count": len(unit_scores), "total_loc": total_loc, "unit_scores": unit_scores,
                  "clean_code_hash": clean_hash}
    category = {"assessed": True, "score": score, "score_method": rubric["score_method"],
                "reason": "Calculated from revision-scoped design metrics and bounded candidate verdicts.",
                "evidence_ids": [row["evidence_id"] for row in observations],
                "confidence": "low" if uncertain else "high", "assessment_status": "provisional" if uncertain else "scored",
                "confidence_reasons": ["Some candidates or indexed chunks could not be fully assessed."] if uncertain else [],
                "metric_schema": rubric["metric_schema"], "rubric_version": rubric["version"]}
    return category, provenance, observations
