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
