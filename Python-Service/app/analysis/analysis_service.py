"""Analysis persistence service — bridges durable job bundles, the knowledge
store, Qdrant/Neo4j and the HTTP layer.

Flow:
  * orchestrator finishes a job → ``write_bundle`` stores the FULL result
    (profile, doc context, sandbox runtime probes, output) under
    ANALYSES_DIR/jobs/<job_id>/bundle.json — survives restarts (orchestrator
    status files are ephemeral and never re-read).
  * developer clicks "Save analysis" → ``persist_analysis(job_id, label)``
    rehydrates the bundle, builds deterministic knowledge chunks from the
    canonical profile (+ runtime probes) via ``project_context``, ingests them
    into the SIBLING collection ``proj_knowledge_<stem>`` (never the wiped-on-
    reindex chunk_code_* collection), best-effort links them into the Neo4j
    graph, and records the session row + artifacts in ``analysis_store``.

Everything is best-effort-safe: no import in this module touches Docker, Qdrant
or Neo4j at import time.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from analysis.analysis_store import ANALYSIS_DIR, analysis_store
from infra.exception_logger import get_exception_logger

try:
    from index.llamaindex_layer import (
        _CODE_PREFIX,
        _KNOWLEDGE_PREFIX,
        knowledge_collection_for,
    )
except Exception:  # pragma: no cover — heavy import kept lazy below anyway
    knowledge_collection_for = None  # type: ignore[assignment]
    _CODE_PREFIX = "chunk_code_"
    _KNOWLEDGE_PREFIX = "proj_knowledge_"


def analyses_dir() -> Path:
    """Durable analyses root (bundles + store artifacts live here)."""
    override = os.environ.get("ANALYSES_DIR")
    if override:
        return Path(override)
    return Path(ANALYSIS_DIR)


def bundle_dir() -> Path:
    return analyses_dir() / "jobs"


def bundle_path(job_id: str) -> Path:
    return bundle_dir() / str(job_id) / "bundle.json"


def write_bundle(job_id: str, data: dict[str, Any]) -> Path:
    """Atomically write a durable per-job result bundle."""
    target = bundle_path(job_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    tmp.replace(target)
    return target


def load_bundle(job_id: str) -> dict[str, Any] | None:
    path = bundle_path(job_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        get_exception_logger().warning("Bundle %s unreadable: %s", job_id, exc)
        return None


def project_stem(filename: str) -> str:
    """Sanitized project stem mirroring project_collection_name's naming."""
    stem = Path(filename).stem if filename else ""
    name = re.sub(r"[^a-zA-Z0-9_]", "_", stem)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "unnamed"


def knowledge_collection(filename: str) -> str:
    if knowledge_collection_for is not None:
        return knowledge_collection_for(project_stem(filename))
    return f"proj_knowledge_{project_stem(filename)}"


def build_counts(profile: dict[str, Any] | None, runtime: dict[str, Any] | None) -> dict[str, Any]:
    """Counts for the saved-analysis list/summary rendering."""
    profile = profile or {}
    wiring = profile.get("wiring") or {}
    packages = profile.get("packages") or {}
    probes: list[dict[str, Any]] = []
    if isinstance(runtime, dict):
        probes = runtime.get("probes") or []
    return {
        "routes": len(profile.get("routes") or []),
        "tables": len(profile.get("schema") or []),
        "models": len(profile.get("models") or []),
        "providers": wiring.get("provider_count") or len(wiring.get("providers") or []),
        "bindings": len(wiring.get("bindings") or []),
        "views": len((profile.get("frontend") or {}).get("views") or []),
        "probes_done": sum(1 for p in probes if p.get("status") == "done"),
        "probes_failed": sum(1 for p in probes if p.get("status") not in ("done", "skipped")),
        "payment_gateways": [g.get("gateway") for g in (packages.get("payment_gateways") or [])],
    }


def link_knowledge_to_graph(
    knowledge_chunks: list[dict[str, Any]], collection_name: str
) -> dict[str, Any]:
    """Best-effort pass linking persisted knowledge nodes into the graph.

    Knowledge chunk nodes already exist as CodeSymbols (ingest). This pass adds
    ``HAS_KNOWLEDGE`` edges from a code symbol to the knowledge node when they
    share the same file path (controller/model/… facts about that file). Runs
    only when the graph store is reachable; never raises.
    """
    edges = 0
    try:
        from index.llamaindex_layer import CodePropertyGraph

        store = CodePropertyGraph()._get_graph_store()
        if store is None:
            return {"linked": 0, "reason": "graph store unavailable"}
        from llama_index.graph_stores.neo4j import Neo4jPropertyGraphStore

        if not isinstance(store, Neo4jPropertyGraphStore):
            return {"linked": 0, "reason": "graph store not Neo4j"}
        # Code symbols live in the *code* collection (chunk_code_<stem>);
        # knowledge nodes in this run's collection (proj_knowledge_<stem>).
        # File paths may be relative either to the workspace or to its
        # wrapped top dir — match exact or by suffix.
        code_collection = collection_name
        if collection_name.startswith(_KNOWLEDGE_PREFIX):
            code_collection = _CODE_PREFIX + collection_name[len(_KNOWLEDGE_PREFIX):]
        cypher = (
            "MATCH (c:CodeSymbol {collection: $code_collection}) "
            "MATCH (k:CodeSymbol {collection: $collection_name}) "
            "WHERE k.file <> '' AND c <> k "
            "AND (c.file = k.file OR c.file ENDS WITH k.file) "
            "MERGE (c)-[:HAS_KNOWLEDGE]->(k)"
        )
        # property-graph stores expose the driver under `.driver` (llama-index
        # keeps the live handle private as `._driver`)
        driver = getattr(store, "driver", None) or getattr(store, "_driver", None)
        if driver is None:
            return {"linked": 0, "reason": "no neo4j driver handle"}
        with driver.session() as session:
            result = session.run(
                cypher,
                collection_name=collection_name,
                code_collection=code_collection,
            )
            summary = result.consume()
            edges = int(getattr(summary.counters, "relationships_created", 0) or 0)
        return {"linked": edges}
    except Exception as exc:  # noqa: BLE001 — linking is enhancement-only
        get_exception_logger().warning("Knowledge graph link pass skipped: %s", exc)
        return {"linked": 0, "reason": str(exc)[:200]}


def persist_analysis(job_id: str, label: str = "") -> dict[str, Any] | None:
    """Save a finished orchestration run as a persisted analysis.

    Returns the stored row (dict) or None when no durable bundle exists.
    Idempotent per job_id (upsert preserves the original created_at).
    """
    bundle = load_bundle(job_id)
    if not bundle:
        return None

    profile = bundle.get("project_profile") or {}
    runtime = bundle.get("sandbox") or {}
    filename = bundle.get("filename") or "unknown.zip"
    collection = knowledge_collection(filename)

    # Knowledge chunks (LLM-valuable facts) → sibling Qdrant collection.
    ingest: dict[str, Any] = {"ingested": 0, "skipped": "no knowledge chunks"}
    link: dict[str, Any] = {"linked": 0}
    chunks: list[dict[str, Any]] = []
    try:
        if profile:
            from projects.project_context import (
                build_grounding_md,
                build_knowledge_chunks,
            )

            chunks = build_knowledge_chunks(profile, project_stem(filename), runtime=runtime)
            if chunks:
                from index.llamaindex_layer import CodePropertyGraph

                ingest = CodePropertyGraph().ingest(chunks, collection) or {}
                link = link_knowledge_to_graph(chunks, collection)
            grounding_md = build_grounding_md(
                profile, runtime=runtime, filename=filename
            )
        else:
            grounding_md = ""
    except Exception as exc:  # noqa: BLE001 — persistence must not hard-fail the run
        get_exception_logger().error(
            "Knowledge ingest failed for analysis %s: %s", job_id, exc
        )
        ingest = {"ingested": 0, "error": str(exc)[:300]}

    counts = build_counts(profile, runtime)
    from projects.project_context import analysis_summary

    summary = (
        (bundle.get("output") or "")
        if not profile
        else analysis_summary(profile)
    )
    if isinstance(summary, str):
        summary = summary[:500]

    # job_id is UNIQUE: re-saving a job must update its ORIGINAL row (same id,
    # preserved created_at, artifacts overwritten) — never insert a second one.
    existing = analysis_store.get_by_job(job_id)
    session_id = (existing or {}).get("id") or str(uuid.uuid4())
    analysis_store.save(
        session_id=session_id,
        job_id=job_id,
        filename=filename,
        collection_name=collection,
        label=(label or "").strip()[:120],
        summary=str(summary),
        profile=profile,
        grounding_md=str(grounding_md),
        runtime=runtime,
        counts=counts,
    )
    row = analysis_store.get(session_id) or {}
    row["ingest"] = ingest
    row["link"] = link
    row["knowledge_chunks"] = len(chunks)
    return row


# --- structure + code-index persistence (P3) ---------------------------------


def code_collection(filename: str) -> str:
    """``chunk_code_<stem>`` — the sibling code collection a Save re-creates."""
    return f"{_CODE_PREFIX}{project_stem(filename)}"


def _fact_chunk(stem: str, name: str, text: str) -> dict[str, Any]:
    """One deterministic structure-fact chunk (project_context._chunk shape)."""
    fqn = f"kn:structure:{stem}"
    return {
        "class_fqn": fqn,
        "class": fqn,
        "name": (name or "unnamed")[:120],
        "method_name": None,
        "file": "structure",
        "type": "structure_fact",
        "code": text[:3000],
        "has_docblock": False,
        "calls": [],
    }


def _structure_facts(profile: dict[str, Any]) -> list[tuple[str, str]]:
    """Deterministic ``(slug, human_text)`` facts for a structure profile.

    Pure + total: only the canonical structure keys are read; absent values
    simply produce fewer facts (never a raise). Fact count lands in the
    ~10-20 range for a normal profile.
    """
    facts: list[tuple[str, str]] = []

    def _list_fact(name: str, label: str, items: list[str], cap: int) -> None:
        cleaned = [str(item) for item in items[:cap]]
        if cleaned:
            facts.append((name, f"{label}: {', '.join(cleaned)}"))

    framework = profile.get("framework")
    version = profile.get("version")
    if framework:
        slug = f"framework:{framework}" + (f":{version}" if version else "")
        facts.append((slug, f"Framework: {framework}" + (f" {version}" if version else "")))

    primary = profile.get("language")
    if primary:
        facts.append((f"language:{primary}", f"Primary language: {primary}"))

    secondary = [
        lang
        for lang in profile.get("languages") or []
        if isinstance(lang, dict)
        and lang.get("language")
        and lang.get("language") != primary
    ][:6]
    for lang in secondary:
        facts.append(
            (
                f"language:{lang['language']}",
                f"{lang['language']}: {lang.get('files') or 0} file(s)",
            )
        )

    database = profile.get("database")
    if database and str(database).lower() != "not determined":
        confidence = profile.get("database_confidence") or "low"
        facts.append(
            (f"database:{database}", f"Database: {database} (confidence: {confidence})")
        )

    total = profile.get("total_files")
    if isinstance(total, int) and total > 0:
        facts.append(("project:total_files", f"Total files: {total}"))

    _list_fact("architecture:top_level_dirs", "Top-level dirs",
               profile.get("top_level_dirs") or [], 8)
    _list_fact("architecture:entrypoints", "Entrypoints",
               profile.get("entrypoints") or [], 8)
    _list_fact("architecture:manifests", "Manifests",
               profile.get("manifests") or [], 8)

    dep_count = profile.get("dependency_count")
    if isinstance(dep_count, int) and dep_count > 0:
        facts.append(("architecture:dependencies", f"Dependency count: {dep_count}"))

    summary = str(profile.get("summary") or "").strip()
    if summary:
        facts.append(("summary", summary[:400]))
    return facts


def _structure_chunks(
    profile: dict[str, Any], stem: str
) -> list[dict[str, Any]]:
    """Structure-fact chunk dicts for one stem (deterministic)."""
    return [
        _fact_chunk(stem, name, text)
        for name, text in _structure_facts(profile)
    ]


def _structure_grounding(facts: list[tuple[str, str]]) -> str:
    """Human-readable structure-fact block appended to the grounding artifact."""
    lines = ["## Structure facts", ""]
    for name, text in facts:
        lines.append(f"- **{name}**: {text}")
    return "\n".join(lines)


def _retained_workspace(filename: str) -> str | None:
    """Newest registered workspace pinned to ``filename`` that still exists."""
    try:
        from projects.workspace_registry import list_workspaces

        for entry in list_workspaces():
            if entry.get("filename") != filename:
                continue
            path = entry.get("path")
            if path and os.path.isdir(path):
                return path
    except Exception:  # noqa: BLE001 — retention lookup is best-effort
        return None
    return None


def _prepare_workspace_http(filename: str) -> str | None:
    """HTTP workspace prepare (mirrors orchestrator._prepare_workspace)."""
    try:
        import requests

        base = os.environ.get("PYTHON_API_URL", "http://python-api:9002")
        response = requests.post(
            f"{base.rstrip('/')}/api/workspace/prepare",
            json={"filename": filename},
            timeout=120,
        )
        if response.status_code >= 400:
            return None
        payload = response.json()
        return payload.get("extracted_path") if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001 — prepare is best-effort
        return None


def _index_sym_graph(workspace_path: str, stem: str) -> dict[str, Any]:
    """Best-effort PHP symbol graph (sym_<stem>); graph down → skipped dict."""
    try:
        from inspection.service import inspection_service

        return inspection_service().index(workspace_path, stem)
    except Exception as exc:  # noqa: BLE001 — sym graph is enhancement-only
        get_exception_logger().warning("Sym graph index skipped: %s", exc)
        return {"indexed": False, "error": str(exc)[:200]}


def persist_structure(job_id: str, label: str = "") -> dict[str, Any] | None:
    """Save a structure_analyze run: structure facts → proj_knowledge_<stem>.

    Deterministic fact chunks (``kn:structure:<stem>`` namespace) ride the same
    CodePropertyGraph ingest + Neo4j link pass as a normal knowledge save.
    Returns the stored row or None when no durable bundle exists. Idempotent
    per job_id.
    """
    bundle = load_bundle(job_id)
    if not bundle:
        return None
    profile = bundle.get("structure_profile") or bundle.get("project_profile") or {}
    filename = bundle.get("filename") or "unknown.zip"
    collection = knowledge_collection(filename)
    stem = project_stem(filename)

    ingest: dict[str, Any] = {"ingested": 0, "skipped": "no structure profile"}
    link: dict[str, Any] = {"linked": 0}
    chunks: list[dict[str, Any]] = []
    try:
        if profile:
            from projects.project_context import build_grounding_md

            facts = _structure_facts(profile)
            chunks = _structure_chunks(profile, stem)
            if chunks:
                from index.llamaindex_layer import CodePropertyGraph

                ingest = CodePropertyGraph().ingest(chunks, collection) or {}
                link = link_knowledge_to_graph(chunks, collection)
            grounding_md = build_grounding_md(profile, runtime=None, filename=filename)
            if facts:
                grounding_md = f"{grounding_md}\n\n{_structure_grounding(facts)}"
        else:
            grounding_md = ""
    except Exception as exc:  # noqa: BLE001 — persistence must not hard-fail the run
        get_exception_logger().error(
            "Structure ingest failed for analysis %s: %s", job_id, exc
        )
        ingest = {"ingested": 0, "error": str(exc)[:300]}

    counts = build_counts(profile, None)
    counts["structure_facts"] = len(chunks)
    if not profile:
        summary = str(bundle.get("output") or "")[:500]
    else:
        summary = str(profile.get("summary") or "")[:500]

    existing = analysis_store.get_by_job(job_id)
    session_id = (existing or {}).get("id") or str(uuid.uuid4())
    analysis_store.save(
        session_id=session_id,
        job_id=job_id,
        filename=filename,
        collection_name=collection,
        label=(label or "").strip()[:120],
        summary=summary,
        profile=profile or None,
        grounding_md=str(grounding_md),
        runtime=None,
        counts=counts,
        kind="structure",
    )
    row = analysis_store.get(session_id) or {}
    row["ingest"] = ingest
    row["link"] = link
    row["knowledge_chunks"] = len(chunks)
    return row


def persist_code_index(job_id: str, label: str = "") -> dict[str, Any] | None:
    """Save an index_code run: parsed chunks → chunk_code_<stem> (destructive).

    The parse is re-produced here from the retained workspace (or a scratch
    HTTP extract that is deleted afterwards) so the durable record always
    matches the current source. The Qdrant collection is recreated on every
    save (CodePropertyGraph.ingest wipes chunk_code_*), so re-saving the same
    job is idempotent per job_id but reflects the newest source.
    Returns the stored row or None when no durable bundle exists.
    """
    bundle = load_bundle(job_id)
    if not bundle:
        return None
    filename = bundle.get("filename") or "unknown.zip"
    stem = project_stem(filename)
    collection = code_collection(filename)

    workspace_path = _retained_workspace(filename)
    scratch = workspace_path is None
    if scratch:
        workspace_path = _prepare_workspace_http(filename)

    result: dict[str, Any] = {}
    ingest: dict[str, Any] = {"ingested": 0, "skipped": "no chunks"}
    sym: dict[str, Any] | None = None
    try:
        if not workspace_path:
            result = {
                "language": None,
                "chunks": [],
                "files": 0,
                "producer": None,
                "error": "workspace unavailable",
            }
        else:
            from projects.lang_index import produce_chunks

            result = produce_chunks(workspace_path)
            chunks = result.get("chunks") or []
            if chunks and not result.get("error"):
                from index.llamaindex_layer import CodePropertyGraph

                ingest = CodePropertyGraph().ingest(chunks, collection) or {}
                if result.get("producer") == "code-parse":
                    sym = _index_sym_graph(workspace_path, stem)
    except Exception as exc:  # noqa: BLE001 — persistence must not hard-fail the run
        get_exception_logger().error(
            "Code index ingest failed for analysis %s: %s", job_id, exc
        )
        result = result or {
            "language": None,
            "chunks": [],
            "files": 0,
            "producer": None,
        }
        ingest = {"ingested": 0, "error": str(exc)[:300]}
    finally:
        if scratch and workspace_path:
            from infra.php_tools import safe_rmtree

            safe_rmtree(workspace_path)

    chunks = result.get("chunks") or []
    language = result.get("language")
    producer = result.get("producer")
    error = result.get("error")

    counts: dict[str, Any] = {
        "chunks": len(chunks),
        "files": int(result.get("files") or 0),
        "language": language,
        "producer": producer,
    }
    for key in ("ingested", "graph_nodes", "graph_relations", "embed_fails"):
        counts[key] = ingest.get(key, 0)
    if sym:
        counts["sym_nodes"] = int(sym.get("nodes") or 0)
    if error:
        counts["error"] = str(error)[:200]

    if error:
        summary = f"code index failed: {error}"[:500]
    else:
        summary = f"{len(chunks)} {language or ''} chunk(s) via {producer or '?'}".strip()[:500]
    grounding_md = (
        f"Code index: {len(chunks)} {language or ''} chunk(s) via {producer or '?'}.\n"
        f"Collection: {collection}"
    )
    if sym and sym.get("collection"):
        grounding_md += f"\nSymbol graph: {sym.get('collection')}"

    profile = bundle.get("structure_profile") or bundle.get("project_profile")
    existing = analysis_store.get_by_job(job_id)
    session_id = (existing or {}).get("id") or str(uuid.uuid4())
    analysis_store.save(
        session_id=session_id,
        job_id=job_id,
        filename=filename,
        collection_name=collection,
        label=(label or "").strip()[:120],
        summary=summary,
        profile=profile,
        grounding_md=grounding_md,
        runtime=None,
        counts=counts,
        kind="code_index",
    )
    row = analysis_store.get(session_id) or {}
    row["ingest"] = ingest
    if sym is not None:
        row["sym"] = sym
    return row


def persist_for_bundle(job_id: str, label: str = "") -> dict[str, Any] | None:
    """Route-aware persist dispatcher (POST /analysis single entry point).

    Chooses the persist strategy from the durable bundle's manifest route:
    ``structure_analyze`` → structure facts, ``index_code`` → code index, every
    other route → the legacy knowledge analysis (kind defaults to 'analysis').
    """
    bundle = load_bundle(job_id)
    if not bundle:
        return None
    manifest = bundle.get("manifest") or {}
    route = manifest.get("route") or bundle.get("route_name") or ""
    if route == "structure_analyze":
        return persist_structure(job_id, label)
    if route == "index_code":
        return persist_code_index(job_id, label)
    return persist_analysis(job_id, label)
