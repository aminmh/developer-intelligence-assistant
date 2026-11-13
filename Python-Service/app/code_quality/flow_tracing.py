"""Bounded PHP call-reachability traces for revision-scoped assessment evidence.

This adapter follows indexed symbol calls from request-input methods to
sensitive-operation methods. It does not claim variable-level taint/data-flow.
"""
from __future__ import annotations

from collections import deque
from hashlib import sha256
import re
from pathlib import PurePosixPath
from typing import Any

from telemetry.snapshot import Snapshot, source_filter, validate_project_id, validate_revision_id

TRACER_VERSION = "php-call-reachability-2"
MAX_GRAPH_NODES = 20_000
MAX_GRAPH_EDGES = 60_000
MAX_TRACES = 3
MAX_PATH_NODES = 8
MAX_TRAVERSAL_STEPS = 50_000
MAX_EXCERPT_CHARS = 900
MAX_UNRESOLVED_DETAILS = 100

# A request object: $request / $req, $this->request, or Laravel's request() helper.
_REQUEST_OBJECT = r"(?:\$(?:request|req)\b|\$this\s*->\s*request\b|\brequest\s*\(\s*\))"
_REQUEST_READERS = (r"(?:input|get|post|query|only|all|except|cookie|header|file|json|string|integer|float|"
                    r"boolean|date|enum|collect|validated|safe|route|getContent)")
# Source: values that come from the HTTP request. Method names alone (for example Eloquent's
# ->get() that runs a query) are not request input; the receiver must be a request object.
_SOURCE = re.compile(
    r"\$_(?:GET|POST|REQUEST|COOKIE|FILES|SERVER)\b"
    rf"|{_REQUEST_OBJECT}\s*->\s*{_REQUEST_READERS}\s*\("
    # Symfony: $request->query->get(...), $request->request->all(), ...
    rf"|{_REQUEST_OBJECT}\s*->\s*(?:query|request|attributes|cookies|files|headers)\s*->\s*\w+\s*\("
    r"|\brequest\s*\(\s*['\"]"
    r"|\bRequest::(?:input|get|post|query|only|all|except|cookie|header|file|json)\s*\("
    r"|php://input",
    re.IGNORECASE,
)
# Sink: operations that are dangerous when they receive request data. Query-builder calls with
# bound values (Model::query(), ->where(), ->select('column')) are not sinks; raw SQL is.
_SINK = re.compile(
    # Global PHP functions only: not ->exec() / ::system() methods or "function exec(" declarations.
    r"(?<![\w>:$])(?<!function )(?:eval|exec|shell_exec|system|passthru|proc_open|popen|pcntl_exec|"
    r"unserialize|assert|create_function)\s*\("
    r"|\bDB::(?:select|selectOne|selectResultSets|cursor|insert|update|delete|statement|unprepared|"
    r"affectingStatement|raw)\s*\("
    r"|\bDB::connection\s*\([^)]*\)\s*->\s*(?:select|selectOne|cursor|insert|update|delete|statement|"
    r"unprepared|affectingStatement|raw)\s*\("
    r"|->\s*(?:whereRaw|orWhereRaw|havingRaw|orHavingRaw|orderByRaw|groupByRaw|selectRaw|fromRaw|joinRaw)\s*\("
    # PDO / mysqli handles; $this, $request and query-builder variables are excluded.
    r"|\$(?!this\b|request\b|req\b|query\b|builder\b)\w+\s*->\s*(?:query|exec|prepare|multi_query|real_query)\s*\("
    r"|\b(?:mysqli_query|mysqli_multi_query|mysqli_real_query|pg_query|pg_send_query)\s*\("
    # Doctrine DBAL / ORM statements built from strings.
    r"|->\s*(?:executeQuery|executeStatement|executeUpdate|createNativeQuery|createQuery)\s*\(",
    re.IGNORECASE,
)
_FLOW_TYPES = {
    "id": "php.request_input_to_sensitive_sink",
    "source": _SOURCE,
    "sink": _SINK,
}
_CALLABLE_TYPES = {"method", "function", "class_method", "classmethod"}


def _unavailable(revision_id: str, reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "revision_id": revision_id,
        "tracer_version": TRACER_VERSION,
        "languages": ["php"],
        "flow_types": [_FLOW_TYPES["id"]],
        "edge_types": ["resolved_static_call"],
        "node_count": 0,
        "edge_count": 0,
        "trace_count": 0,
        "unresolved_edge_count": 0,
        "limits": _limits(),
        "reason": reason,
    }


def _limits() -> dict[str, int]:
    return {
        "nodes": MAX_GRAPH_NODES,
        "edges": MAX_GRAPH_EDGES,
        "traces": MAX_TRACES,
        "path_nodes": MAX_PATH_NODES,
        "traversal_steps": MAX_TRAVERSAL_STEPS,
        "excerpt_chars_per_node": MAX_EXCERPT_CHARS,
    }


def _location(payload: dict[str, Any], business_files: set[str]) -> tuple[str, int, int] | None:
    file = payload.get("file_path") or payload.get("file")
    if not isinstance(file, str) or not file or len(file) > 300:
        return None
    path = PurePosixPath(file)
    if path.is_absolute() or ".." in path.parts or "\\" in file or path.as_posix() not in business_files:
        return None
    start = payload.get("start_line")
    end = payload.get("end_line")
    if type(start) is not int or start < 1 or type(end) is not int or end < start:
        return None
    return path.as_posix(), start, end


def _symbol(payload: dict[str, Any]) -> str | None:
    value = payload.get("fq_symbol_name") or payload.get("symbol_identity") or payload.get("symbol_name")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lstrip("\\")[:300]


def _call_target(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lstrip("\\")[:300]
    return normalized or None


def build_call_traces(
    project_id: str,
    revision_id: str,
    snapshot: dict[str, Any],
    chunks: list[dict[str, Any]],
    business_files: list[str],
    *,
    truncated: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build deterministic request-to-sensitive-sink call paths from AST chunks."""
    validate_project_id(project_id)
    validate_revision_id(revision_id)
    commit_sha = snapshot.get("commit_sha")
    archive_hash = snapshot.get("source_archive_sha256")
    try:
        expected_collection = Snapshot(project_id, commit_sha, archive_hash).collection
    except (TypeError, ValueError):
        return _unavailable(revision_id, "The selected revision has invalid source identity metadata."), []
    if (snapshot.get("state") != "ready" or snapshot.get("project_id") != project_id
            or snapshot.get("revision_id") != revision_id
            or snapshot.get("collection_name") != expected_collection):
        return _unavailable(revision_id, "The selected revision does not have a ready matching index."), []
    if not isinstance(business_files, list) or not business_files:
        return _unavailable(revision_id, "No inventoried business source files were available to trace."), []
    if not any(isinstance(path, str) and path.lower().endswith(".php") for path in business_files):
        return _unavailable(
            revision_id,
            "The current semantic flow analyzer supports PHP only; this revision has no inventoried PHP source.",
        ), []

    allowed_files = {item for item in business_files if isinstance(item, str)}
    nodes: dict[str, dict[str, Any]] = {}
    symbol_to_id: dict[str, str] = {}
    ambiguous_symbols: set[str] = set()
    unresolved_count = 0
    unresolved_details: list[dict[str, Any]] = []
    edge_count = 0
    limit_reasons: list[str] = ["node_limit"] if truncated else []

    for payload in chunks:
        if len(nodes) >= MAX_GRAPH_NODES:
            limit_reasons.append("node_limit")
            break
        if not isinstance(payload, dict):
            continue
        if (payload.get("project_id") != project_id
                or payload.get("revision_id") != revision_id
                or any(payload.get(key) != value for key, value in source_filter(snapshot).items())
                or payload.get("source_archive_sha256") != archive_hash
                or payload.get("dirty") is not False
                or payload.get("collection") != expected_collection):
            continue
        if str(payload.get("node_type") or payload.get("type") or "").lower() not in _CALLABLE_TYPES:
            continue
        location = _location(payload, allowed_files)
        symbol = _symbol(payload)
        code = payload.get("code")
        if location is None or symbol is None or not isinstance(code, str) or not code:
            continue
        key = symbol.casefold()
        # Ambiguous names cannot safely become call targets.
        if key in ambiguous_symbols:
            continue
        node_id = sha256(f"{project_id}:{revision_id}:{location[0]}:{location[1]}:{key}".encode()).hexdigest()
        if key in symbol_to_id and symbol_to_id[key] != node_id:
            symbol_to_id.pop(key, None)
            ambiguous_symbols.add(key)
            continue
        symbol_to_id[key] = node_id
        nodes[node_id] = {
            "node_id": node_id,
            "symbol": symbol,
            "file": location[0],
            "start_line": location[1],
            "end_line": location[2],
            "code": code[:MAX_EXCERPT_CHARS],
            "calls": sorted({target.casefold() for item in (payload.get("called_symbols") or [])
                              if (target := _call_target(item))}),
        }
        try:
            unresolved_references = max(0, int(payload.get("unresolved_reference_count") or 0))
            unresolved_count += unresolved_references
            if unresolved_references and len(unresolved_details) < MAX_UNRESOLVED_DETAILS:
                unresolved_details.append({
                    "source_symbol": symbol, "file": location[0], "line": location[1],
                    "target_symbol": None, "reason": "exporter_unresolved_references",
                    "count": unresolved_references,
                })
        except (TypeError, ValueError):
            unresolved_count += 1

    # Remove ambiguous symbols that were seen more than once.
    present_ids = set(nodes)
    symbol_to_id = {key: node_id for key, node_id in symbol_to_id.items() if node_id in present_ids}
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for node_id, node in nodes.items():
        for target_name in node["calls"]:
            target_id = symbol_to_id.get(target_name)
            if target_id is None:
                # Calls outside this PHP/business-source graph are explicit unknowns.
                unresolved_count += 1
                if len(unresolved_details) < MAX_UNRESOLVED_DETAILS:
                    unresolved_details.append({
                        "source_symbol": node["symbol"], "file": node["file"],
                        "line": node["start_line"], "target_symbol": target_name,
                        "reason": "ambiguous_target" if target_name in ambiguous_symbols else "target_not_indexed",
                        "count": 1,
                    })
                continue
            adjacency[node_id].append(target_id)
            edge_count += 1
            if edge_count >= MAX_GRAPH_EDGES:
                limit_reasons.append("edge_limit")
                break
        adjacency[node_id].sort(key=lambda value: (nodes[value]["file"], nodes[value]["start_line"], nodes[value]["symbol"].casefold()))
        if edge_count >= MAX_GRAPH_EDGES:
            break

    source_ids = sorted(
        (node_id for node_id, node in nodes.items() if _FLOW_TYPES["source"].search(node["code"])),
        key=lambda value: (nodes[value]["file"], nodes[value]["start_line"], nodes[value]["symbol"].casefold()),
    )
    sink_ids = {node_id for node_id, node in nodes.items() if _FLOW_TYPES["sink"].search(node["code"])}
    traces: list[dict[str, Any]] = []
    traversal_steps = 0
    for source_id in source_ids:
        if len(traces) >= MAX_TRACES or traversal_steps >= MAX_TRAVERSAL_STEPS:
            break
        queue = deque([(source_id, [source_id])])
        seen = {source_id}
        found_sink = False
        while queue:
            current, path = queue.popleft()
            traversal_steps += 1
            if current in sink_ids:
                found_sink = True
                traces.append(_trace(project_id, revision_id, nodes, path))
                if len(traces) >= MAX_TRACES:
                    break
            if len(path) >= MAX_PATH_NODES:
                continue
            for neighbor in adjacency.get(current, []):
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                queue.append((neighbor, [*path, neighbor]))
                if traversal_steps >= MAX_TRAVERSAL_STEPS:
                    break
            if traversal_steps >= MAX_TRAVERSAL_STEPS:
                break
        if found_sink and len(traces) >= MAX_TRACES:
            break
    if traversal_steps >= MAX_TRAVERSAL_STEPS:
        limit_reasons.append("traversal_step_limit")
    if len(traces) >= MAX_TRACES and source_ids:
        limit_reasons.append("trace_limit")

    status = "partial" if truncated or unresolved_count or limit_reasons else "complete"
    reason = (
        "Call reachability is shown; variable-level data flow is not proven. "
        "Some graph edges are unresolved or outside the indexed PHP business-source scope."
        if status == "partial" else
        "Call reachability is complete only for recognized request-input and sensitive-sink patterns in the indexed PHP business-source scope; variable-level data flow is not proven."
    )
    summary = {
        "status": status,
        "revision_id": revision_id,
        "tracer_version": TRACER_VERSION,
        "languages": ["php"],
        "flow_types": [_FLOW_TYPES["id"]],
        "edge_types": ["resolved_static_call"],
        "node_count": len(nodes),
        "edge_count": edge_count,
        "trace_count": len(traces),
        "unresolved_edge_count": unresolved_count,
        "unresolved_edges": sorted(
            unresolved_details,
            key=lambda item: (item["file"], item["line"], item["source_symbol"].casefold(),
                              item["target_symbol"] or "", item["reason"]),
        )[:MAX_UNRESOLVED_DETAILS],
        "limits": _limits(),
        "limits_reached": sorted(set(limit_reasons)),
        "reason": reason,
    }
    evidence = [{
        "evidence_id": item["evidence_id"],
        "kind": "semantic_trace",
        "category": "security",
        "rule": TRACER_VERSION,
        "file": item["nodes"][0]["file"],
        "line": item["nodes"][0]["start_line"],
        "message": (
            f"PHP request-input to sensitive-operation call path ({len(item['nodes'])} symbols); "
            "call reachability only, variable-level data flow unproven."
        ),
        "context_snippet": "\n".join(
            f"{node['symbol']} at {node['file']}:{node['start_line']}-{node['end_line']}\n{node['code']}"
            for node in item["source_code"]
        )[:MAX_EXCERPT_CHARS * MAX_PATH_NODES],
        "trace": {"nodes": item["nodes"], "edges": item["edges"]},
    } for item in traces]
    return summary, evidence


def _trace(project_id: str, revision_id: str, nodes: dict[str, dict[str, Any]], path: list[str]) -> dict[str, Any]:
    evidence_id = "ev_" + sha256(
        f"{project_id}:{revision_id}:{TRACER_VERSION}:{'|'.join(path)}".encode()
    ).hexdigest()[:16]
    return {
        "evidence_id": evidence_id,
        "nodes": [{key: nodes[node_id][key] for key in ("symbol", "file", "start_line", "end_line")}
                  for node_id in path],
        "source_code": [{key: nodes[node_id][key] for key in ("symbol", "file", "start_line", "end_line", "code")}
                        for node_id in path],
        "edges": [{"type": "resolved_static_call", "from": nodes[left]["symbol"],
                   "to": nodes[right]["symbol"], "file": nodes[left]["file"],
                   "line": nodes[left]["start_line"]}
                  for left, right in zip(path, path[1:])],
    }


def retrieve_revision_call_traces(
    project_id: str,
    revision_id: str,
    snapshot: dict[str, Any],
    business_files: list[str],
    *, include_design_signals: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read only the selected ready snapshot's Qdrant payloads and trace calls."""
    validate_project_id(project_id)
    validate_revision_id(revision_id)
    if snapshot.get("state") != "ready" or snapshot.get("project_id") != project_id or snapshot.get("revision_id") != revision_id:
        return _unavailable(revision_id, "The selected revision does not have a ready matching index."), []
    if ".php" not in {PurePosixPath(path).suffix.lower() for path in business_files if isinstance(path, str)}:
        return _unavailable(revision_id, "The initial semantic flow adapter supports PHP business source files."), []
    try:
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        from index.llamaindex_layer import CodePropertyGraph

        commit_sha = snapshot.get("commit_sha")
        archive_hash = snapshot.get("source_archive_sha256")
        expected_collection = Snapshot(project_id, commit_sha, archive_hash).collection
        if snapshot.get("collection_name") != expected_collection:
            return _unavailable(revision_id, "The selected revision index identity does not match its source snapshot."), []
        from index.embedding_rebuild import active_vector_collection
        vector_collection = active_vector_collection(expected_collection)
        if vector_collection is None:
            return _unavailable(revision_id, "Rebuild Cohere embeddings for this revision in Knowledge Base before assessment."), []
        qdrant = CodePropertyGraph()._get_qdrant()
        scroll_filter = Filter(must=[
            FieldCondition(key="project_id", match=MatchValue(value=project_id)),
            FieldCondition(key="revision_id", match=MatchValue(value=revision_id)),
            *[FieldCondition(key=key, match=MatchValue(value=value))
              for key, value in source_filter(snapshot).items()],
            FieldCondition(key="source_archive_sha256", match=MatchValue(value=archive_hash)),
            FieldCondition(key="dirty", match=MatchValue(value=False)),
            FieldCondition(key="collection", match=MatchValue(value=vector_collection)),
        ])
        rows: list[dict[str, Any]] = []
        offset = None
        truncated = False
        while True:
            records, next_offset = qdrant.scroll(
                collection_name=vector_collection,
                scroll_filter=scroll_filter,
                limit=min(256, MAX_GRAPH_NODES + 1 - len(rows)),
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for record in records:
                payload = dict(record.payload or {})
                if payload.get("collection") == vector_collection:
                    # Pure trace builders consume the logical snapshot identity.
                    rows.append({**payload, "collection": expected_collection})
            if len(rows) > MAX_GRAPH_NODES:
                rows = rows[:MAX_GRAPH_NODES]
                truncated = True
                break
            if next_offset is None:
                break
            offset = next_offset
        flow_summary, flow_evidence = build_call_traces(
            project_id, revision_id, snapshot, rows, business_files, truncated=truncated,
        )
        if not include_design_signals:
            return flow_summary, flow_evidence
        try:
            from code_quality.php_design import build_php_design_signals

            design_summary, design_evidence = build_php_design_signals(
                project_id, revision_id, snapshot, rows, business_files, truncated=truncated,
            )
        except Exception:
            design_summary, design_evidence = ({
                "status": "unavailable", "revision_id": revision_id,
                "analyzer_version": "php-solid-design-signals-1", "languages": ["php"],
                "principles_with_static_signals": [],
                "principles_requiring_semantic_evidence": [],
                "chunk_count": 0, "class_count": 0, "method_count": 0,
                "signal_count": 0, "signals_truncated": False,
                "reason": "The selected revision's PHP design index could not be analyzed.",
            }, [])
        flow_summary["php_design_signals"] = design_summary
        return flow_summary, [*flow_evidence, *design_evidence]
    except Exception:
        return _unavailable(revision_id, "The selected revision's PHP call graph could not be read."), []
