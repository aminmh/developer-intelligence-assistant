"""Background Cohere rebuilds for immutable project snapshot vectors."""

from __future__ import annotations

import json
import logging
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

from infra.settings import settings
from infra.php_tools import extract_zip_safely, run_code_parse

logger = logging.getLogger(__name__)
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _status_path(job_id: str) -> Path:
    return Path(settings.download_dir) / f"embedding-{job_id}.status.json"


def write_status(job_id: str, *, status: str, stage: str, total: int = 0,
                 indexed: int = 0, error: str | None = None,
                 collection_name: str = "") -> None:
    path = _status_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": job_id, "status": status, "stage": stage,
        "total": total, "indexed": indexed,
        "collection_name": collection_name, "error": error,
    }
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload), encoding="utf-8")
    temp.replace(path)


def read_status(job_id: str) -> dict[str, Any] | None:
    try:
        return json.loads(_status_path(job_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def activate_generation(snapshot_collection: str, generation: str, *,
                        domain_project_id: str | None = None,
                        domain_generation: str | None = None) -> None:
    """Atomically publish a complete generation and retain the prior one."""
    from telemetry.registry import default_registry

    def publish(current: dict | None) -> dict:
        state = current or {"active": {}, "retained": {}}
        active = dict(state.get("active") or {})
        retained = {key: list(value) for key, value in (state.get("retained") or {}).items()}
        previous = active.get(snapshot_collection)
        if previous and previous != generation:
            retained[snapshot_collection] = [previous, *retained.get(snapshot_collection, [])]
        active[snapshot_collection] = generation
        domains = dict(state.get("domains") or {})
        if domain_project_id and domain_generation:
            previous_domain = domains.get(domain_project_id)
            domain_key = f"domain:{domain_project_id}"
            if previous_domain and previous_domain != domain_generation:
                retained[domain_key] = [previous_domain, *retained.get(domain_key, [])]
            domains[domain_project_id] = domain_generation
        return {"active": active, "domains": domains, "retained": retained}

    # One locked read-modify-write: frequent revisions must not overwrite each other's update.
    default_registry().update_runtime_setting("cohere_embedding_generations", publish)


def active_generation(snapshot_collection: str) -> str | None:
    from telemetry.registry import default_registry

    saved = default_registry().get_runtime_setting("cohere_embedding_generations") or {}
    active = (saved.get("value") or {}).get("active", {})
    return active.get(snapshot_collection) if isinstance(active, dict) else None


def active_vector_collection(snapshot_collection: str) -> str | None:
    """Resolve published vectors without falling back to retained generations."""
    if settings.embedding_provider != "cohere":
        return snapshot_collection
    generation = active_generation(snapshot_collection)
    if not isinstance(generation, str) or not generation.startswith(snapshot_collection + "_cohere_"):
        return None
    return generation


def start_rebuild(job_id: str, filename: str, archive: bytes, identity: dict[str, str],
                  snapshot_collection: str) -> None:
    """Queue one revision rebuild; active generation changes only on success."""
    with _locks_guard:
        lock = _locks.setdefault(snapshot_collection, threading.Lock())
    if not lock.acquire(blocking=False):
        raise RuntimeError("A Cohere rebuild is already running for this revision")

    thread = threading.Thread(
        target=_run_rebuild,
        args=(lock, job_id, filename, archive, identity, snapshot_collection),
        daemon=True,
    )
    thread.start()


def _run_rebuild(lock: threading.Lock, job_id: str, filename: str, archive: bytes,
                 identity: dict[str, str], snapshot_collection: str) -> None:
    from telemetry.registry import default_registry
    from telemetry.snapshot import Snapshot

    snapshot = Snapshot.from_row(identity["project_id"], identity)  # keeps a working tree's dirty flag
    registry = default_registry()
    try:
        with registry.lock(snapshot.project_id, snapshot.reference):
            current = registry.get(snapshot.project_id, snapshot.reference)
            if current.get("state") != "ready" or current.get("revision_id") != identity.get("revision_id"):
                raise RuntimeError("The selected revision is no longer available for rebuilding")
            _run_rebuild_locked(lock, job_id, filename, archive, identity, snapshot_collection)
    except Exception as exc:
        logger.exception("Embedding rebuild could not start for %s", snapshot_collection)
        write_status(job_id, status="error", stage="error", error=str(exc)[:300], collection_name=snapshot_collection)
        lock.release()


def _run_rebuild_locked(lock: threading.Lock, job_id: str, filename: str, archive: bytes,
                        identity: dict[str, str], snapshot_collection: str) -> None:
    generation = f"{snapshot_collection}_cohere_{uuid.uuid4().hex}"
    workdir = Path(settings.download_dir) / f"embedding-work-{job_id}"
    try:
        write_status(job_id, status="running", stage="extracting", collection_name=generation)
        extract_zip_safely(archive, str(workdir))
        chunks = run_code_parse(str(workdir))
        if not chunks:
            raise RuntimeError("The selected revision contains no indexable code")

        from inspection.cli import inspect_index
        from index.llamaindex_layer import CodePropertyGraph
        from telemetry.registry import default_registry
        from telemetry.snapshot import Snapshot

        exported = inspect_index(str(workdir))
        snapshot = Snapshot.from_row(identity["project_id"], identity)
        if snapshot.collection != snapshot_collection:
            raise RuntimeError("Snapshot identity changed while the rebuild was queued")

        def report(stage: str, completed: int, total: int) -> None:
            write_status(
                job_id, status="running", stage=stage, total=total,
                indexed=completed, collection_name=generation,
            )

        write_status(
            job_id, status="running", stage="embedding", total=len(chunks),
            collection_name=generation,
        )
        result = CodePropertyGraph().ingest(
            chunks,
            snapshot_collection,
            symbol_definitions=exported.get("definitions", []),
            symbol_references=exported.get("references", []),
            snapshot=snapshot,
            progress_callback=report,
            vector_collection_name=generation,
            reuse=False,  # a rebuild exists to re-embed every chunk
        )
        if (result.get("ingested") != result.get("chunk_count")
                or result.get("embed_fails", 0)):
            raise RuntimeError("Cohere did not index every source chunk")

        from projects.domain.indexer import ensure_collection, index_project_concepts
        from projects.domain.store import concept_store

        project_id = identity["project_id"]
        concepts = concept_store.list_concepts(project_id, limit=10000)
        domain_generation = f"domain_concepts_cohere_{uuid.uuid4().hex}"
        if concepts:
            def report_domain(completed: int, total: int) -> None:
                report("domain_embedding", completed, total)

            domain_result = index_project_concepts(
                project_id,
                concepts,
                collection=domain_generation,
                progress_callback=report_domain,
                force_reembed=True,
            )
            if not domain_result.get("ok") or domain_result.get("failed", 0) or domain_result.get("pending", 0):
                raise RuntimeError(
                    "Cohere did not index every domain concept: "
                    + str(domain_result.get("error") or "one or more concepts failed")
                )
        else:
            empty_collection = ensure_collection(domain_generation)
            if not empty_collection.get("ok"):
                raise RuntimeError(
                    "Could not create an empty Cohere domain generation: "
                    + str(empty_collection.get("error") or "collection unavailable")
                )

        write_status(
            job_id, status="running", stage="activating",
            total=result.get("chunk_count", 0), indexed=result.get("ingested", 0),
            collection_name=generation,
        )
        activate_generation(
            snapshot_collection,
            generation,
            domain_project_id=project_id,
            domain_generation=domain_generation,
        )
        write_status(
            job_id, status="done", stage="complete", total=result.get("chunk_count", 0),
            indexed=result.get("ingested", 0), collection_name=generation,
        )
    except Exception as exc:  # noqa: BLE001 — failure leaves the previous generation active
        logger.exception("Cohere embedding rebuild failed for %s", snapshot_collection)
        write_status(
            job_id, status="error", stage="error", error=str(exc)[:300],
            collection_name=generation,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        lock.release()
