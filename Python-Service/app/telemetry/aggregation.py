"""Project-scoped telemetry rollups and durable graph/vector projections."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import re
from uuid import NAMESPACE_URL, uuid5

from telemetry.registry import default_registry
from telemetry.runtime import ClickHouseRuntime, runtime_sampling_policy, snapshot_definitions
from telemetry.snapshot import storage_id

logger = logging.getLogger(__name__)
_SAFE_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,190}$")


def _table_identity(value: str) -> tuple[str, str]:
    value = value.strip()
    if value.startswith("table:"):
        identity = value
        name = value.removeprefix("table:")
    else:
        name = value
        identity = f"table:default.{value}"
    if not _SAFE_TABLE.fullmatch(name):
        return f"table:runtime.{hashlib.sha256(value.encode()).hexdigest()[:24]}", value[:190]
    return identity, name


def _graph_facts(project_id: str, revision_id: str, commit_sha: str, collection: str, definitions: list[dict], telemetry: dict) -> dict[str, list[dict]]:
    window = int(telemetry.get("aggregation_window_hours", 24))
    known = {item.get("symbol_id") for item in definitions if item.get("symbol_id")}
    symbols = {row.get("symbol_id"): row for row in telemetry.get("symbols", []) if row.get("symbol_id") in known}
    facts: dict[str, list[dict]] = {"symbols": [], "calls": [], "routes": [], "tables": []}
    for symbol_id in sorted(known):
        row = symbols.get(symbol_id, {})
        facts["symbols"].append({
            "symbol_id": symbol_id,
            "count": int(row.get("runs", 0) or 0),
            "p95_ms": float(row.get("p95_ms", 0) or 0),
            "error_rate": float(row.get("error_rate", 0) or 0),
            "last_seen": row.get("last_seen"),
            "window_hours": window,
        })
    for row in telemetry.get("callers", []):
        if row.get("symbol_id") in known and row.get("caller_symbol_id") in known:
            facts["calls"].append({
                "symbol_id": row["symbol_id"],
                "caller_symbol_id": row["caller_symbol_id"],
                "calls": int(row.get("calls", 0) or 0),
                "last_seen": telemetry.get("last_seen"),
                "window_hours": window,
            })

    route_rows = telemetry.get("routes", [])
    routes_by_path: dict[str, dict] = {}
    routes_by_id: dict[str, dict] = {}
    for route in route_rows:
        path = str(route.get("route", ""))
        method = str(route.get("method", "GET")).upper()
        route_id = str(route.get("route_id") or f"route:{method} {path}")
        routes_by_path[path] = {**route, "route_id": route_id}
        routes_by_id[route_id] = {**route, "route_id": route_id}
    for row in telemetry.get("symbol_routes", []):
        symbol_id = row.get("symbol_id")
        route_ref = str(row.get("route_id") or row.get("route", ""))
        route = routes_by_id.get(route_ref) or routes_by_path.get(route_ref)
        path = str((route or {}).get("route", route_ref))
        if symbol_id not in known or not route_ref:
            continue
        route_id = str((route or {}).get("route_id") or route_ref)
        facts["routes"].append({
            "id": storage_id(project_id, commit_sha, route_id),
            "symbol_id": symbol_id,
            "calls": int(row.get("calls", 0) or 0),
            "p95_ms": float((route or {}).get("p95_ms", 0) or 0),
            "error_rate": float((route or {}).get("error_rate", 0) or 0),
            "last_seen": (route or {}).get("last_seen") or telemetry.get("last_seen"),
            "window_hours": window,
            "properties": {
                "project_id": project_id, "revision_id": revision_id, "commit_sha": commit_sha,
                "collection": collection, "route_id": route_id,
                "method": (route or {}).get("method", ""),
                "route": (route or {}).get("route", path),
            },
        })

    table_rows: dict[str, dict] = {}
    for row in telemetry.get("tables", []):
        table_ref = str(row.get("table_id") or row.get("table_name", ""))
        current = table_rows.setdefault(table_ref, {"operations": set(), "last_seen": None})
        operation = str(row.get("operation", "")).strip()
        if operation:
            current["operations"].add(operation)
        if str(row.get("last_seen", "")) > str(current.get("last_seen") or ""):
            current["last_seen"] = row.get("last_seen")
    for current in table_rows.values():
        current["operation"] = ", ".join(sorted(current.pop("operations"))) or "other"
    for row in telemetry.get("symbol_tables", []):
        symbol_id = row.get("symbol_id")
        table_name = str(row.get("table_name", ""))
        table_ref = str(row.get("table_id") or table_name)
        if symbol_id not in known or not table_ref:
            continue
        table_id, canonical_name = _table_identity(table_ref)
        table = table_rows.get(table_ref, {})
        facts["tables"].append({
            "id": storage_id(project_id, commit_sha, table_id),
            "symbol_id": symbol_id,
            "operations": int(row.get("calls", 0) or 0),
            "operation": str(table.get("operation", "other")),
            "last_seen": table.get("last_seen") or telemetry.get("last_seen"),
            "window_hours": window,
            "properties": {
                "project_id": project_id, "revision_id": revision_id, "commit_sha": commit_sha,
                "collection": collection, "table_id": table_id,
                "table_name": table_name or canonical_name,
            },
        })
    return facts


def _store_exception_vectors(
    client: ClickHouseRuntime,
    project_id: str,
    revision_id: str,
    commit_sha: str,
    window_hours: int,
) -> int:
    try:
        rows = client.rows(
            f"SELECT TraceId, SpanId, Timestamp, "
            "SpanAttributes['indexer.exception.type'] AS exception_type, "
            "SpanAttributes['indexer.exception.normalized_message'] AS normalized_message, "
            "SpanAttributes['indexer.exception.top_frames'] AS top_frames, "
            "SpanAttributes['indexer.symbol_id'] AS symbol_id "
            f"FROM `{client.database}`.`{client.table}` WHERE "
            "SpanAttributes['indexer.exception.normalized_message'] != '' "
            f"AND Timestamp >= now() - INTERVAL {window_hours} HOUR "
            "ORDER BY Timestamp DESC LIMIT 10000",
            project_id, revision_id,
        )
        if not rows:
            return 0
        from qdrant_client import QdrantClient, models
        from infra.settings import settings
        from index.embeddings import embed_texts

        groups: dict[str, dict] = {}
        for row in rows:
            message = str(row.get("normalized_message", "")).strip()
            exception_type = str(row.get("exception_type", "Unknown"))[:255]
            if not message:
                continue
            raw_frames = row.get("top_frames", [])
            if isinstance(raw_frames, str):
                try:
                    raw_frames = json.loads(raw_frames)
                except ValueError:
                    raw_frames = []
            frames = [str(frame)[:512] for frame in raw_frames if isinstance(frame, str)][:5] if isinstance(raw_frames, list) else []
            signature = json.dumps([project_id, revision_id, exception_type, message, frames], separators=(",", ":"))
            item = groups.setdefault(signature, {
                "id": str(uuid5(NAMESPACE_URL, "indexr-runtime-exception:" + signature)),
                "message": message, "exception_type": exception_type, "frames": frames,
                "symbol_id": str(row.get("symbol_id", "")), "count": 0,
                "last_seen": str(row.get("Timestamp", "")),
                "trace_ids": [],
            })
            item["count"] += 1
            trace_id = str(row.get("TraceId", ""))
            if trace_id and trace_id not in item["trace_ids"] and len(item["trace_ids"]) < 20:
                item["trace_ids"].append(trace_id)
            if str(row.get("Timestamp", "")) > item["last_seen"]:
                item["last_seen"] = str(row.get("Timestamp", ""))

        qdrant = QdrantClient(url=settings.qdrant_url)
        # Keep Cohere signatures in their own vector space; the legacy
        # collection may contain vectors from a different dimension/model.
        collection = (
            "runtime_exceptions_cohere_v5"
            if settings.embedding_provider == "cohere"
            else "runtime_exceptions"
        )
        grouped_items = list(groups.values())
        existing: set[str] = set()
        if qdrant.collection_exists(collection):
            ids = [item["id"] for item in grouped_items]
            for offset in range(0, len(ids), 256):
                points = qdrant.retrieve(
                    collection_name=collection,
                    ids=ids[offset:offset + 256],
                    with_payload=False,
                    with_vectors=False,
                )
                existing.update(str(point.id) for point in points)
            for item in grouped_items:
                if item["id"] in existing:
                    qdrant.set_payload(
                        collection_name=collection,
                        payload={
                            "observations": item["count"],
                            "last_seen": item["last_seen"],
                            "symbol_id": item["symbol_id"],
                            "trace_ids": item["trace_ids"],
                        },
                        points=[item["id"]],
                        wait=False,
                    )
        new_items = [item for item in grouped_items if item["id"] not in existing]
        if new_items:
            texts = [item["exception_type"] + " " + item["message"] + " " + " ".join(item["frames"]) for item in new_items]
            vectors = embed_texts(texts)
            if not qdrant.collection_exists(collection):
                qdrant.create_collection(collection_name=collection, vectors_config=models.VectorParams(size=len(vectors[0]), distance=models.Distance.COSINE))
            points = []
            for item, vector in zip(new_items, vectors):
                points.append(models.PointStruct(
                    id=item["id"], vector=vector,
                    payload={
                        "project_id": project_id, "revision_id": revision_id, "commit_sha": commit_sha,
                        "symbol_id": item["symbol_id"], "exception_type": item["exception_type"],
                        "normalized_message": item["message"], "top_frames": item["frames"],
                        "observations": item["count"], "last_seen": item["last_seen"],
                        "trace_ids": item["trace_ids"],
                    },
                ))
            qdrant.upsert(collection_name=collection, points=points, wait=False)
        return len(new_items)
    except Exception as exc:  # noqa: BLE001 — unavailable vector services must not block telemetry
        logger.warning("Runtime exception indexing skipped: %s", exc)
        return 0


def aggregate_runtime() -> dict:
    """Refresh graph facts and searchable normalized exception signatures."""
    registry = default_registry()
    clickhouse = ClickHouseRuntime()
    if not clickhouse.configured:
        return {"processed": 0, "reason": "ClickHouse is not configured."}
    from inspection.graph import InspectionGraph

    graph = InspectionGraph()
    seen: set[tuple[str, str]] = set()
    processed = 0
    unresolved_rows: list[dict] = []
    for project in registry.runtime_projects():
        project_id = project.get("project_id")
        commit_sha = project.get("commit_sha")
        revision_id = project.get("revision_id")
        if project.get("state") != "ready" or not project_id or not commit_sha or not revision_id:
            continue
        pair = (project_id, revision_id)
        if pair in seen:
            continue
        seen.add(pair)
        try:
            definitions = snapshot_definitions(project_id, commit_sha)
            known = {item.get("symbol_id") for item in definitions if item.get("symbol_id")}
            telemetry = clickhouse.summary(project_id, revision_id, known)
            if not telemetry.get("available"):
                continue
            collection = "sym_snapshot_" + storage_id(project_id, commit_sha, "")
            facts = _graph_facts(project_id, revision_id, commit_sha, collection, definitions, telemetry)
            graph.upsert_runtime_facts(collection, facts)
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            for row in telemetry.get("symbols", []):
                symbol_id = row.get("symbol_id")
                if symbol_id:
                    unresolved_rows.append({
                        "project_id": project_id, "revision_id": revision_id,
                        "commit_sha": commit_sha,
                        "symbol_id": symbol_id, "observed_spans": int(row.get("runs", 0) or 0),
                        "last_seen": row.get("last_seen") or now,
                        "snapshot_updated_at": now,
                        "is_unresolved": int(symbol_id not in known),
                    })
            _store_exception_vectors(
                clickhouse, project_id, revision_id, commit_sha,
                int(telemetry.get("aggregation_window_hours", 24)),
            )
            processed += 1
        except Exception as exc:  # noqa: BLE001 — retry this scope on the next aggregation tick
            logger.warning("Runtime aggregation failed for project %s: %s", project_id, exc)
    try:
        clickhouse.store_unresolved_symbols(unresolved_rows)
    except Exception as exc:  # noqa: BLE001 — table availability is visible in pipeline health
        logger.warning("Unresolved-symbol persistence failed: %s", exc)
    return {"processed": processed, "unmatched_records": len(unresolved_rows)}
