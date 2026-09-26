"""CI archive ingestion: immutable source and fail-closed ready publication."""
from __future__ import annotations

import hashlib
import fcntl
import io
from pathlib import Path
import stat
import tempfile
from typing import Any, Callable
import zipfile

from infra.php_tools import extract_zip_safely
from telemetry.registry import SnapshotRegistry
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


def index_source(workspace: str, snapshot: Snapshot) -> dict:
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

    def upload(self, project_id: str, commit_sha: str, data: bytes) -> dict:
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
                self.registry.transition(snapshot, "awaiting_deployment", archive_key=key)
            self.registry.queue_if_deployable(project_id, commit_sha)
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
            snapshot = Snapshot(project_id, commit_sha, current["source_archive_sha256"])
            try:
                key = self.registry.archive_key(project_id, commit_sha)
                if not key:
                    raise RuntimeError("Staged archive expired")
                data = self.storage.get_snapshot_archive(key)
                with tempfile.TemporaryDirectory(prefix="source_", dir=self.registry.root) as workspace:
                    extract_zip_safely(data, workspace)
                    result = self.indexer(workspace, snapshot)
                count = result.get("chunk_count", 0)
                if (not count or result.get("ingested") != count
                        or result.get("graph_nodes") != count or result.get("embed_fails", 0)):
                    raise RuntimeError("Incomplete snapshot indexing")
                self.registry.finish_job(project_id, commit_sha, success=True)
            except Exception:
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
