"""CI archive ingestion: immutable source and fail-closed ready publication."""
from __future__ import annotations

import hashlib
import fcntl
import io
import logging
from pathlib import Path
import stat
import tempfile
from typing import Any, Callable
import zipfile

from infra.php_tools import extract_zip_safely
from telemetry.registry import SnapshotRegistry, default_registry
from telemetry.snapshot import Snapshot
from telemetry.symbols import relative_path

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20_000


def validate_archive(data: bytes) -> None:
    if not data or len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("ZIP archive exceeds the source upload limit or is empty")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES or sum(i.file_size for i in entries) > MAX_EXPANDED_BYTES:
                raise ValueError("Expanded source archive exceeds limits")
            seen = set()
            for entry in entries:
                path = relative_path(entry.filename.rstrip("/"))
                if path in seen or path != entry.filename.rstrip("/"):
                    raise ValueError("Archive contains ambiguous paths")
                seen.add(path)
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or entry.flag_bits & 1:
                    raise ValueError("Symlinks and encrypted ZIP entries are unsupported")
            if archive.testzip() is not None:
                raise ValueError("Corrupt source archive")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValueError("Invalid source ZIP archive") from exc


def _normalize_exported_paths(exported: dict[str, list[dict[str, Any]]], workspace: str) -> None:
    root = Path(workspace).resolve()
    for records in (exported.get("definitions", []), exported.get("references", [])):
        for record in records:
            for field in ("file", "file_path"):
                value = record.get(field)
                if not isinstance(value, str) or not value:
                    continue
                path = Path(value)
                if path.is_absolute():
                    try:
                        value = path.resolve().relative_to(root).as_posix()
                    except ValueError as exc:
                        raise ValueError("Source index emitted a path outside the snapshot root") from exc
                record[field] = relative_path(value)


def index_source(workspace: str, snapshot: Snapshot) -> dict:
    if not any(path.is_file() and path.suffix.lower() == ".php" for path in Path(workspace).rglob("*")):
        # Static assessment supports more languages than the current call graph
        # exporter. Publish the source snapshot while marking semantic indexing
        # unavailable; callers must not interpret this as an empty clean graph.
        return {"chunk_count": 0, "ingested": 0, "graph_nodes": 0, "graph_relations": 0,
                "embed_fails": 0, "semantic_index_status": "unavailable",
                "semantic_index_reason": "The current symbol and call graph exporter supports PHP only."}

    from infra.php_cli import PhpCli
    from index.llamaindex_layer import CodePropertyGraph
    from inspection.service import InspectionService

    cli = PhpCli()
    # Legacy parser accepts exit 2 (partial parse). Exact snapshots must not.
    result = cli.run("code-parse", ["--path", workspace, "--output", "json"], timeout=600)
    if result.returncode != 0:
        raise ValueError("Source parsing failed or was incomplete")
    import json
    chunks = json.loads(result.stdout)
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("Source archive contains no indexable PHP")
    exported = cli.inspect_index(workspace)
    _normalize_exported_paths(exported, workspace)
    result = CodePropertyGraph().ingest(
        chunks, snapshot.collection, snapshot=snapshot,
        symbol_definitions=exported["definitions"], symbol_references=exported["references"],
    )
    InspectionService().index(workspace, snapshot.key, exported=exported, snapshot=snapshot)
    return result


class SourceService:
    def __init__(self, registry: SnapshotRegistry, storage: Any,
                 indexer: Callable[[str, Snapshot], dict] = index_source):
        self.registry, self.storage, self.indexer = registry, storage, indexer

    def upload(self, project_id: str, commit_sha: str | None, data: bytes) -> dict:
        snapshot = Snapshot(project_id, commit_sha, hashlib.sha256(data).hexdigest())
        validate_archive(data)
        source_ref = snapshot.reference
        with self.registry.lock(project_id, source_ref):
            self.registry.register(snapshot)
            current = self.registry.get(project_id, source_ref)
            if current["state"] == "ready":
                return current
            if not self.registry.archive_key(project_id, source_ref):
                self.registry.transition(snapshot, "storing")
                key = self.storage.put_snapshot_archive(snapshot.key, snapshot.source_archive_sha256, data)
                self.registry.transition(snapshot, "awaiting_deployment", archive_key=key)
            if commit_sha:
                self.registry.queue_if_deployable(project_id, source_ref, retry_failed=True)
            return self.registry.get(project_id, source_ref)

    def upload_git_snapshot(self, project_id: str, commit_sha: str, data: bytes) -> dict:
        """Stage a fetched Git revision and queue indexing independently of deployment state."""
        snapshot = Snapshot(project_id, commit_sha, hashlib.sha256(data).hexdigest())
        validate_archive(data)
        with self.registry.lock(project_id, commit_sha):
            self.registry.register(snapshot)
            current = self.registry.get(project_id, commit_sha)
            if current["state"] == "ready":
                return current
            if not self.registry.archive_key(project_id, commit_sha):
                self.registry.transition(snapshot, "storing")
                key = self.storage.put_snapshot_archive(snapshot.key, snapshot.source_archive_sha256, data)
                self.registry.transition(snapshot, "indexing", archive_key=key)
            self.registry.queue_source_index(project_id, commit_sha)
            return self.registry.get(project_id, commit_sha)

    def process_one(self) -> bool:
        """Run one durable job. A running job is reclaimable after process restart."""
        with (self.registry.root / "index-worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            job = self.registry.claim_job()
            if job is None:
                return False
            project_id, commit_sha = job
            current = self.registry.get(project_id, commit_sha)
            snapshot = Snapshot(project_id, current.get("commit_sha"), current["source_archive_sha256"])
            try:
                key = self.registry.archive_key(project_id, commit_sha)
                if not key:
                    raise RuntimeError("Staged archive expired")
                data = self.storage.get_snapshot_archive(key)
                with tempfile.TemporaryDirectory(prefix="source_", dir=self.registry.root) as workspace:
                    extract_zip_safely(data, workspace)
                    result = self.indexer(workspace, snapshot)
                count = result.get("chunk_count", 0)
                semantic_unavailable = result.get("semantic_index_status") == "unavailable"
                if (semantic_unavailable and any(result.get(key) for key in (
                        "chunk_count", "ingested", "graph_nodes", "graph_relations", "embed_fails"))):
                    raise RuntimeError("Unavailable semantic indexing returned graph data")
                if (not semantic_unavailable and (not count or result.get("ingested") != count
                        or result.get("graph_nodes") != count or result.get("embed_fails", 0))):
                    raise RuntimeError("Incomplete snapshot indexing")
                self.registry.finish_job(project_id, commit_sha, success=True)
            except Exception as exc:
                logging.getLogger(__name__).error(
                    "Snapshot indexing failed for project=%s revision_id=%s: %s",
                    project_id,
                    current.get("revision_id"),
                    str(exc)[:500],
                )
                self.registry.finish_job(project_id, commit_sha, success=False)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
            return True

    def expire_unmatched(self) -> None:
        for project_id, commit_sha, key in self.registry.expired_archives():
            try:
                self.storage.delete_snapshot_archive(key)
                self.registry.forget_expired_archive(project_id, commit_sha, key)
            except Exception:
                # Keep expiry best effort; metadata is retained for an exact reupload.
                pass
        self.registry.stopped_deployments_before()


def index_uploaded_snapshot(
    snapshot: Snapshot,
    indexer: Callable[[], dict],
    *,
    force_reindex: bool = False,
) -> dict:
    """Index a browser upload now and publish it through the snapshot gate."""
    registry = default_registry()
    with registry.lock(snapshot.project_id, snapshot.reference):
        if snapshot.revision_id is None:
            registry.register(snapshot)
        current = registry.get(snapshot.project_id, snapshot.reference)
        if (not current.get("revision_id") or current.get("state") == "deleting"
                or (snapshot.revision_id and current.get("revision_id") != snapshot.revision_id)):
            raise RuntimeError("The uploaded revision is no longer available for indexing")
        if current.get("state") == "ready" and not force_reindex:
            return {
                "collection_name": current.get("collection_name", snapshot.collection),
                "already_indexed": True,
                "ingested": 0,
                "chunk_count": 0,
                "graph_nodes": 0,
                "graph_relations": 0,
                "embed_fails": 0,
            }
        registry.transition(snapshot, "indexing")
        try:
            result = indexer()
            count = int(result.get("chunk_count", 0) or 0)
            if (not count or int(result.get("ingested", 0) or 0) != count
                    or int(result.get("graph_nodes", 0) or 0) != count
                    or int(result.get("embed_fails", 0) or 0)):
                raise RuntimeError("Incomplete snapshot indexing")
            registry.finish_job(snapshot.project_id, snapshot.reference, success=True)
            return result
        except Exception:
            registry.finish_job(snapshot.project_id, snapshot.reference, success=False)
            raise
