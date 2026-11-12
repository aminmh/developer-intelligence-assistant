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


def validate_source_ref(project_id: str, source_ref: str) -> None:
    validate_project_id(project_id)
    if not isinstance(source_ref, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64}|(?:upload|wt)_[0-9a-f]{64})", source_ref):
        raise ValueError("Invalid source reference")


def source_reference(metadata: dict[str, Any]) -> str:
    """Return a source key without representing archive identity as Git metadata."""
    snapshot = Snapshot.from_row(metadata["project_id"], metadata)
    if metadata.get("source_ref") not in (None, snapshot.reference):
        raise ValueError("Source reference does not match archive identity")
    return snapshot.reference


def source_filter(metadata: dict[str, Any]) -> dict[str, str]:
    reference = source_reference(metadata)
    return {"commit_sha": reference} if metadata.get("commit_sha") else {"source_ref": reference}


def is_git_reference(source_ref: str) -> bool:
    """True for a commit reference; False for archive-identified uploads and working trees."""
    return not source_ref.startswith(("upload_", "wt_"))


def validate_project_id(project_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", project_id):
        raise ValueError("Invalid project ID")


def validate_revision_id(revision_id: str) -> None:
    if not re.fullmatch(r"rev_[0-9a-f]{32}", revision_id):
        raise ValueError("Invalid immutable revision ID")


def storage_id(project_id: str, commit_sha: str, symbol_id: str) -> str:
    validate_source_ref(project_id, commit_sha)
    raw = json.dumps([project_id, commit_sha, symbol_id], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


SOURCE_KINDS = ("upload", "git", "working_tree")


@dataclass(frozen=True)
class Snapshot:
    """One immutable source revision.

    Clean revisions are a Git commit (``commit_sha``) or an uploaded archive
    (``upload_<sha256>``). A working-tree revision is the uncommitted state of a
    developer's folder: ``dirty=True``, no ``commit_sha`` (its base commit is
    registry metadata only), and reference ``wt_<sha256>``, so it can never be
    mistaken for the clean revision of the commit it started from.
    """

    project_id: str
    commit_sha: str | None
    source_archive_sha256: str
    dirty: bool = False
    telemetry_schema_version: str = SCHEMA_VERSION
    revision_id: str | None = None

    def __post_init__(self) -> None:
        validate_project_id(self.project_id)
        if self.commit_sha is not None:
            validate_scope(self.project_id, self.commit_sha)
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_archive_sha256):
            raise ValueError("Source checksum must be SHA256")
        if self.dirty not in (False, True):
            raise ValueError("Snapshot dirty flag must be a boolean")
        if self.dirty and self.commit_sha is not None:
            raise ValueError("Commit snapshots require clean source; dirty evidence is approximate only")
        if self.telemetry_schema_version != SCHEMA_VERSION:
            raise ValueError("Unsupported telemetry schema version")
        if self.revision_id is not None:
            validate_revision_id(self.revision_id)

    @property
    def reference(self) -> str:
        if self.dirty:
            return "wt_" + self.source_archive_sha256
        return self.commit_sha or "upload_" + self.source_archive_sha256

    @property
    def source_kind(self) -> str:
        if self.dirty:
            return "working_tree"
        return "git" if self.commit_sha else "upload"

    @property
    def key(self) -> str:
        return storage_id(self.project_id, self.reference, "")

    @property
    def collection(self) -> str:
        return "snapshot_" + self.key

    def metadata(self) -> dict[str, Any]:
        return {**asdict(self), "source_ref": self.reference}

    @classmethod
    def from_row(cls, project_id: str, row: dict[str, Any]) -> "Snapshot":
        """The revision a registry row (or its metadata) describes, working trees included."""
        return cls(project_id, row.get("commit_sha"), row["source_archive_sha256"], dirty=row.get("dirty") is True)


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
                     storage_id=storage_id(snapshot.project_id, snapshot.reference, identity))
        result.append(chunk)
    return result


def identity_metadata(chunk: dict[str, Any]) -> dict[str, Any]:
    keys = ("symbol_id", "symbol_identity_error", "project_id", "commit_sha", "source_ref", "revision_id",
            "source_archive_sha256", "dirty", "telemetry_schema_version")
    return {key: chunk[key] for key in keys if chunk.get(key) is not None}


def resolve_registered_revision(snapshot: Snapshot) -> Snapshot:
    """Attach the registry's immutable revision ID before indexing its chunks."""
    from telemetry.registry import default_registry

    current = default_registry().get(snapshot.project_id, snapshot.reference)
    revision_id = current.get("revision_id")
    if not revision_id:
        return snapshot
    if current.get("source_archive_sha256") != snapshot.source_archive_sha256:
        raise ValueError("Snapshot content does not match its registered revision")
    if snapshot.revision_id and snapshot.revision_id != revision_id:
        raise ValueError("Snapshot revision ID does not match its registry entry")
    return replace(snapshot, revision_id=revision_id)
