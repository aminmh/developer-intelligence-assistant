"""Validated Excimer profile persistence and sampled call-edge rollups."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re

from telemetry.runtime import ClickHouseRuntime, runtime_sampling_policy
from telemetry.span_schema import SPAN_SCOPE
from telemetry.snapshot import validate_project_id, validate_revision_id

_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")
_PROFILE_ID = re.compile(r"^prof_[0-9a-f]{24}$")
_METHOD_ID = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*\\)*[A-Za-z_][A-Za-z0-9_]*::[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
MAX_PROFILE_BYTES = 262_144
MAX_PROFILES_PER_BATCH = 64
# PHP posts each request's profile on its own, so a plain insert makes one MergeTree part per request.
# ClickHouse buffers these server-side and flushes them as one part. The API does not wait for the flush,
# because the PHP sender gives up after 250 ms; profiles are best effort, like the sender.
_ASYNC_INSERT_SETTINGS = {
    "async_insert": "1",
    "wait_for_async_insert": "0",
    "async_insert_use_adaptive_busy_timeout": "0",
    "async_insert_busy_timeout_max_ms": "5000",
}
_ensured_tables: set[tuple[str, str]] = set()


def _profile_table(client: ClickHouseRuntime) -> str:
    table = getattr(client, "profile_table", "otel_profiles")
    if not _SAFE_IDENTIFIER.fullmatch(client.database) or not _SAFE_IDENTIFIER.fullmatch(table):
        raise ValueError("Invalid profile table configuration")
    return f"`{client.database}`.`{table}`"


def validate_profile(profile: object, project_id: str, revision_id: str, commit_sha: str) -> dict:
    if not isinstance(profile, dict):
        raise ValueError("Profile entries must be objects")
    if profile.get("project_id") != project_id or profile.get("revision_id") != revision_id:
        raise ValueError("Profile identity does not match the requested project revision")
    trace_id = str(profile.get("trace_id", "")).lower()
    span_id = str(profile.get("span_id", "")).lower()
    profile_id = str(profile.get("profile_id", ""))
    if (not _TRACE_ID.fullmatch(trace_id) or not _SPAN_ID.fullmatch(span_id)
            or not _PROFILE_ID.fullmatch(profile_id) or set(trace_id) == {"0"} or set(span_id) == {"0"}):
        raise ValueError("Profile trace identity is invalid")
    event_type = profile.get("event_type")
    if event_type not in {"EXCIMER_REAL", "EXCIMER_CPU"}:
        raise ValueError("Profile event type is invalid")
    duration = profile.get("duration_ms")
    period = profile.get("sample_period_ms")
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Profile duration is invalid")
    if type(period) not in (int, float) or not math.isfinite(period) or abs(float(period) - 5.0) > 0.01:
        raise ValueError("Profile sample period must be the Phase 4 5 ms interval")
    collapsed = profile.get("samples_collapsed")
    if not isinstance(collapsed, str) or not collapsed or len(collapsed.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise ValueError("Collapsed profile is missing or too large")
    lines = collapsed.splitlines()
    if len(lines) > 2000:
        raise ValueError("Collapsed profile contains too many stacks")
    frames: set[str] = set()
    total_samples = 0
    for line in lines:
        try:
            stack, count_text = line.rsplit(" ", 1)
            count = int(count_text)
        except (ValueError, TypeError):
            raise ValueError("Collapsed profile format is invalid") from None
        symbols = stack.split(";")
        if not 1 <= len(symbols) <= 128 or not all(_METHOD_ID.fullmatch(symbol) for symbol in symbols):
            raise ValueError("Collapsed profile contains a non-canonical frame")
        if not 1 <= count <= 1_000_000:
            raise ValueError("Collapsed profile sample weight is invalid")
        total_samples += count
        if total_samples > 10_000_000:
            raise ValueError("Collapsed profile sample weight exceeds the limit")
        frames.update(symbols)
    if total_samples == 0:
        raise ValueError("Collapsed profile has no samples")
    resources = profile.get("resource_attributes")
    if not isinstance(resources, dict):
        raise ValueError("Profile resource identity is required")
    allowed_resources = {
        "service.name", "service.version", "indexer.project_id", "indexer.revision_id",
        "indexer.code_dirty", "indexer.schema.version", "indexer.sampling.ratio", "deployment.environment.name",
        "vcs.ref.head.revision", "container.id", "container.image.name",
    }
    if not set(resources).issubset(allowed_resources):
        raise ValueError("Profile resource attributes contain an unsupported field")
    if (resources.get("indexer.project_id") != project_id
            or resources.get("indexer.revision_id") != revision_id
            or resources.get("deployment.environment.name") != profile.get("environment")
            or resources.get("indexer.schema.version") != "indexer-telemetry-v2"
            or resources.get("indexer.code_dirty") is not False):
        raise ValueError("Profile resource attributes do not match its envelope")
    environment = profile.get("environment")
    service = profile.get("service_name")
    if not isinstance(environment, str) or not 1 <= len(environment) <= 64:
        raise ValueError("Profile environment is invalid")
    if not isinstance(service, str) or not 1 <= len(service) <= 128:
        raise ValueError("Profile service name is invalid")
    top_frames = profile.get("top_frames")
    if not isinstance(top_frames, list) or not 1 <= len(top_frames) <= 10 or not all(
        isinstance(frame, str) and _METHOD_ID.fullmatch(frame) and frame in frames for frame in top_frames
    ):
        raise ValueError("Profile top frames are invalid")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    return {
        "profile_id": profile_id,
        "project_id": project_id,
        "revision_id": revision_id,
        "commit_sha": commit_sha,
        "trace_id": trace_id,
        "span_id": span_id,
        "duration_ms": float(duration),
        "event_type": event_type,
        "sample_period_ms": float(period),
        "environment": environment,
        "service_name": service,
        "samples_collapsed": collapsed,
        "top_frames": json.dumps(top_frames, separators=(",", ":")),
        "received_at": now,
    }


def store_profiles(client: ClickHouseRuntime, project_id: str, revision_id: str, commit_sha: str, profiles: list) -> int:
    validate_project_id(project_id)
    validate_revision_id(revision_id)
    if not isinstance(profiles, list) or not 1 <= len(profiles) <= MAX_PROFILES_PER_BATCH:
        raise ValueError("Profile batch must contain between 1 and 64 profiles")
    if not client.configured:
        raise RuntimeError("ClickHouse profile storage is unavailable")
    rows = [validate_profile(profile, project_id, revision_id, commit_sha) for profile in profiles]
    table = _profile_table(client)
    body = "\n".join(json.dumps(row, separators=(",", ":")) for row in rows).encode("utf-8")
    ensured = (getattr(client, "url", ""), table)
    if ensured in _ensured_tables:
        try:
            client.execute(f"INSERT INTO {table} FORMAT JSONEachRow", body, _ASYNC_INSERT_SETTINGS)
            return len(rows)
        except Exception:
            # The table may have been dropped since it was created; recreate it and retry once.
            _ensured_tables.discard(ensured)
    _create_profile_table(client, table)
    _ensured_tables.add(ensured)
    client.execute(f"INSERT INTO {table} FORMAT JSONEachRow", body, _ASYNC_INSERT_SETTINGS)
    return len(rows)


def _create_profile_table(client: ClickHouseRuntime, table: str) -> None:
    retention = runtime_sampling_policy()["aggregation_retention_hours"]
    client.execute(
        f"CREATE TABLE IF NOT EXISTS {table} (profile_id String, project_id String, revision_id String, "
        "commit_sha String, trace_id FixedString(32), span_id FixedString(16), duration_ms Float64, "
        "event_type LowCardinality(String), sample_period_ms Float64, environment LowCardinality(String), "
        "service_name String, samples_collapsed String, top_frames String, received_at DateTime64(6)) "
        "ENGINE = ReplacingMergeTree(received_at) ORDER BY (project_id, revision_id, profile_id) "
        f"TTL received_at + INTERVAL {retention} HOUR"
    )


def profile_edges(client: ClickHouseRuntime, project_id: str, revision_id: str, known_symbol_ids: set[str]) -> dict:
    """Decode canonical folded stacks to weighted runtime edges and frame shares."""
    if not client.configured:
        return {"edges": [], "samples": 0, "profiles": 0, "hot_path_share": {}}
    table = _profile_table(client)
    window = int(getattr(client, "window_hours", 24))
    try:
        rows = client.rows(
            f"SELECT profile_id, samples_collapsed FROM {table} FINAL WHERE project_id = {{project_id:String}} "
            "AND revision_id = {revision_id:String} AND received_at >= now() - INTERVAL " + str(window) + " HOUR "
            "ORDER BY received_at DESC LIMIT 1000",
            project_id,
            revision_id,
        )
    except Exception:
        return {"edges": [], "samples": 0, "profiles": 0}

    weights: dict[tuple[str, str], int] = {}
    frame_profiles: dict[str, int] = {}
    total_samples = 0
    for row in rows:
        collapsed = row.get("samples_collapsed", "")
        if not isinstance(collapsed, str):
            continue
        for line in collapsed.splitlines()[:2000]:
            try:
                stack_text, count_text = line.rsplit(" ", 1)
                sample_count = min(1_000_000, max(1, int(count_text)))
            except (ValueError, TypeError):
                continue
            stack = stack_text.split(";")
            if not any(symbol in known_symbol_ids for symbol in stack):
                continue
            total_samples += sample_count
            for symbol in set(stack):
                if symbol in known_symbol_ids:
                    frame_profiles[symbol] = frame_profiles.get(symbol, 0) + sample_count
            for caller, callee in sorted(set(zip(stack, stack[1:]))):
                if caller in known_symbol_ids and callee in known_symbol_ids and caller != callee:
                    weights[(caller, callee)] = weights.get((caller, callee), 0) + sample_count

    edges = [
        {
            "caller_symbol_id": caller,
            "symbol_id": callee,
            "sample_weight": weight,
            "hot_path_share": round(100 * weight / total_samples, 3) if total_samples else 0.0,
            "source": "excimer",
        }
        for (caller, callee), weight in sorted(weights.items(), key=lambda item: item[1], reverse=True)[:5000]
    ]
    shares = {
        symbol: round(100 * weight / total_samples, 3)
        for symbol, weight in frame_profiles.items()
    }
    return {"edges": edges, "samples": total_samples, "profiles": len(rows), "hot_path_share": shares}


def method_hook_edges(client: ClickHouseRuntime, project_id: str, revision_id: str, known_symbol_ids: set[str]) -> list[dict]:
    """Read bounded root-span counters recorded by targeted method hooks."""
    if not client.configured:
        return []
    table = f"`{client.database}`.`{client.table}`"
    window = int(getattr(client, "window_hours", 24))
    try:
        rows = client.rows(
            "SELECT SpanAttributes['indexer.method_hook_counts'] AS hook_counts "
            f"FROM {table} WHERE {_scope_clause()} "
            "AND SpanAttributes['indexer.method_hook_counts'] != '' ORDER BY Timestamp DESC LIMIT 5000",
            project_id,
            revision_id,
            {"param_window_hours": str(window)},
        )
    except Exception:
        return []
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        raw = row.get("hook_counts", "")
        try:
            entries = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            continue
        if not isinstance(entries, list):
            continue
        for item in entries[:256]:
            if not isinstance(item, dict):
                continue
            caller = item.get("caller_symbol_id")
            callee = item.get("symbol_id")
            calls = item.get("calls")
            if (isinstance(caller, str) and isinstance(callee, str) and caller in known_symbol_ids
                    and callee in known_symbol_ids and _METHOD_ID.fullmatch(caller) and _METHOD_ID.fullmatch(callee)
                    and type(calls) is int and 0 < calls <= 1_000_000):
                key = (caller, callee)
                counts[key] = counts.get(key, 0) + calls
    return [
        {"caller_symbol_id": caller, "symbol_id": callee, "calls": calls, "source": "hook"}
        for (caller, callee), calls in counts.items()
    ]


def _scope_clause() -> str:
    return (
        SPAN_SCOPE + " AND Timestamp >= now() - INTERVAL {window_hours:UInt32} HOUR"
    )


def merge_method_edges(span_edges: list[dict], hook_edges: list[dict], profile_data: dict) -> list[dict]:
    """Merge exact hook counts, parent-child spans and statistical profile weights."""
    merged: dict[tuple[str, str], dict] = {}
    for source, entries in (("span", span_edges), ("hook", hook_edges), ("excimer", profile_data.get("edges", []))):
        for item in entries:
            caller, callee = item.get("caller_symbol_id"), item.get("symbol_id")
            if not isinstance(caller, str) or not isinstance(callee, str):
                continue
            edge = merged.setdefault((caller, callee), {
                "caller_symbol_id": caller, "symbol_id": callee,
                "calls": 0, "hook_calls": 0, "span_edges": 0,
                "sample_weight": 0, "hot_path_share": 0.0, "sources": [],
            })
            if source == "excimer":
                edge["sample_weight"] = int(item.get("sample_weight", 0) or 0)
                edge["hot_path_share"] = float(item.get("hot_path_share", 0) or 0)
            elif source == "hook":
                edge["hook_calls"] = int(item.get("calls", 0) or 0)
                edge["calls"] = max(edge["calls"], edge["hook_calls"])
            else:
                edge["span_edges"] = int(item.get("calls", 0) or 0)
                edge["calls"] = max(edge["calls"], int(item.get("calls", 0) or 0))
            if source not in edge["sources"]:
                edge["sources"].append(source)
    return sorted(merged.values(), key=lambda row: (row["sample_weight"], row["calls"]), reverse=True)[:5000]


def delete_profiles(client: ClickHouseRuntime, project_id: str, revision_id: str, trace_ids: list[str]) -> int:
    """Delete profile payloads alongside explicitly deleted sampled request traces."""
    table = _profile_table(client)
    try:
        found = client.rows(
            "SELECT count() AS tables FROM system.tables WHERE database = {database:String} AND name = {table_name:String}",
            project_id,
            revision_id,
            {"param_database": client.database, "param_table_name": getattr(client, "profile_table", "otel_profiles")},
        )
        if not found or int(found[0].get("tables", 0) or 0) == 0:
            return 0
        placeholders = []
        # Lightweight DELETE masks the rows before returning instead of rewriting whole parts.
        parameters = {"lightweight_deletes_sync": "2", "param_project_id": project_id, "param_revision_id": revision_id}
        for index, trace_id in enumerate(trace_ids):
            name = f"trace_id_{index}"
            placeholders.append(f"{{{name}:String}}")
            parameters[f"param_{name}"] = trace_id
        client.execute(
            f"DELETE FROM {table} WHERE project_id = {{project_id:String}} "
            "AND revision_id = {revision_id:String} "
            f"AND trace_id IN ({', '.join(placeholders)})",
            extra_parameters=parameters,
            timeout=120,
        )
        return len(trace_ids)
    except Exception as exc:
        raise RuntimeError("Stored profiles could not be deleted") from exc
