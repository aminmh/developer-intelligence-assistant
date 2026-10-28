"""Validated snapshot metadata and storage identity; no legacy migration."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import re
from typing import Any

from telemetry.symbols import from_chunk, relative_path

SCHEMA_VERSION = "indexer-telemetry-v2"


def validate_scope(project_id: str, commit_sha: str) -> None:
    validate_project_id(project_id)
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit_sha):
        raise ValueError("Commit must be a full lowercase Git SHA")


def validate_project_id(project_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", project_id):
        raise ValueError("Invalid project ID")


def validate_revision_id(revision_id: str) -> None:
    if not re.fullmatch(r"rev_[0-9a-f]{32}", revision_id):
        raise ValueError("Invalid immutable revision ID")


def storage_id(project_id: str, commit_sha: str, symbol_id: str) -> str:
    validate_scope(project_id, commit_sha)
    raw = json.dumps([project_id, commit_sha, symbol_id], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    project_id: str
    commit_sha: str
    source_archive_sha256: str
    dirty: bool = False
    telemetry_schema_version: str = SCHEMA_VERSION
    revision_id: str | None = None

    def __post_init__(self) -> None:
        validate_scope(self.project_id, self.commit_sha)
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_archive_sha256):
            raise ValueError("Source checksum must be SHA256")
        if self.dirty is not False:
            raise ValueError("CI snapshots require clean source; dirty evidence is approximate only")
        if self.telemetry_schema_version != SCHEMA_VERSION:
            raise ValueError("Unsupported telemetry schema version")
        if self.revision_id is not None:
            validate_revision_id(self.revision_id)

    @property
    def key(self) -> str:
        return storage_id(self.project_id, self.commit_sha, "")

    @property
    def collection(self) -> str:
        return "snapshot_" + self.key

    def metadata(self) -> dict[str, Any]:
        return asdict(self)


def prepare_chunks(chunks: list[dict[str, Any]], snapshot: Snapshot) -> list[dict[str, Any]]:
    result = []
    seen: set[str] = set()
    for original in chunks:
        chunk = dict(original)
        if chunk.get("symbol_identity_error"):
            raise ValueError("Exporter reported an ambiguous symbol identity")
        canonical = from_chunk(chunk)
        if chunk.get("symbol_id") is not None and chunk["symbol_id"] != canonical:
            raise ValueError("Exporter canonical ID differs from the shared contract")
        path = relative_path(chunk.get("file_path") or chunk.get("file"))
        # Non-runtime chunks (class summaries/properties/etc.) need isolation too.
        # This key is deliberately not advertised as a telemetry symbol_id.
        identity = canonical or "ast:" + json.dumps([
            chunk.get("node_type") or chunk.get("type"),
            chunk.get("fq_symbol_name") or chunk.get("name"), path,
            chunk.get("start_line"), chunk.get("end_line"),
        ], ensure_ascii=False, separators=(",", ":"))
        if identity in seen:
            raise ValueError(f"Ambiguous duplicate identity: {identity}")
        seen.add(identity)
        chunk.update(snapshot.metadata())
        chunk.update(symbol_id=canonical, file_path=path, file=path,
                     storage_id=storage_id(snapshot.project_id, snapshot.commit_sha, identity))
        result.append(chunk)
    return result


def identity_metadata(chunk: dict[str, Any]) -> dict[str, Any]:
    keys = ("symbol_id", "symbol_identity_error", "project_id", "commit_sha", "revision_id",
            "source_archive_sha256", "dirty", "telemetry_schema_version")
    return {key: chunk[key] for key in keys if chunk.get(key) is not None}


def resolve_registered_revision(snapshot: Snapshot) -> Snapshot:
    """Attach the registry's immutable revision ID before indexing its chunks."""
    from telemetry.registry import default_registry

    current = default_registry().get(snapshot.project_id, snapshot.commit_sha)
    revision_id = current.get("revision_id")
    if not revision_id:
        return snapshot
    if current.get("source_archive_sha256") != snapshot.source_archive_sha256:
        raise ValueError("Snapshot content does not match its registered revision")
    if snapshot.revision_id and snapshot.revision_id != revision_id:
        raise ValueError("Snapshot revision ID does not match its registry entry")
    return replace(snapshot, revision_id=revision_id)
