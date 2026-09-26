"""CI archive ingestion: immutable source and fail-closed ready publication."""
from __future__ import annotations

import hashlib
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
            try:
                self.registry.transition(snapshot, "storing")
                # Reuse the app's MinIO archive owner, with a deterministic
                # session unique to the snapshot. Never overwrite legacy ZIPs.
                key = self.storage.put_snapshot_archive(snapshot.key, snapshot.source_archive_sha256, data)
                self.registry.transition(snapshot, "indexing", archive_key=key)
                with tempfile.TemporaryDirectory(prefix="source_", dir=self.registry.root) as workspace:
                    extract_zip_safely(data, workspace)
                    result = self.indexer(workspace, snapshot)
                count = result.get("chunk_count", 0)
                if (not count or result.get("ingested") != count
                        or result.get("graph_nodes") != count or result.get("embed_fails", 0)):
                    raise RuntimeError("Incomplete snapshot indexing")
                self.registry.transition(snapshot, "ready")
            except Exception:
                # Parser/DB/embedding diagnostics may include source values.
                # Status is deliberately a bounded code, never exception text.
                self.registry.transition(snapshot, "failed", error_code="snapshot_indexing_failed")
            return self.registry.get(project_id, commit_sha)
