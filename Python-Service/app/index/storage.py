"""MinIO (S3-compatible) storage for RAG uploads and workspaces.

Replaces the former php-ast service's Laravel `Storage` disks:
  * `tus-uploads` bucket   — RAG uploads live under `rag-uploads/{session}/{filename}`,
                             written either by the direct upload endpoint or copied
                             there from a finished tusd upload.
  * workspace root         — extracted project ZIPs, stored on the local filesystem
                             (shared with the in-container PHP CLI).
"""

from __future__ import annotations

import io
import hashlib
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from minio import Minio
from minio.commonconfig import CopySource

from infra.settings import settings
from infra.settings_store import settings_store

RAG_UPLOADS_PREFIX = "rag-uploads"


class Storage:
    """Lazy MinIO client wrapper. Singleton-ish; reconnects on revision change."""

    def __init__(self) -> None:
        self._client: Optional[Minio] = None
        self._revision = -1
        self._identity_lock = threading.RLock()

    @property
    def tus_bucket(self) -> str:
        return settings.minio_tus_bucket or "tus-uploads"

    def _get_client(self) -> Minio:
        revision = settings_store.revision
        if self._client is None or self._revision != revision:
            endpoint = settings.minio_endpoint or "minio:9000"
            endpoint = endpoint.replace("https://", "").replace("http://", "").rstrip("/")
            self._client = Minio(
                endpoint,
                access_key=settings.minio_access_key or "minio",
                secret_key=settings.minio_secret_key or "minio123",
                secure=False,
            )
            self._revision = revision
        return self._client

    # --- RAG uploads (rag-uploads/{session}/{filename}) ---------------------

    def list_rag_files(self) -> list[dict[str, Any]]:
        """List every object under `rag-uploads/` with session context."""
        client = self._get_client()
        bucket = self.tus_bucket
        files: list[dict[str, Any]] = []
        try:
            objects = client.list_objects(bucket, prefix=RAG_UPLOADS_PREFIX + "/", recursive=True)
            for obj in objects:
                key = obj.object_name
                parts = key.split("/")
                if len(parts) < 3:
                    continue
                session_dir = parts[1]
                filename = "/".join(parts[2:])
                last_modified = obj.last_modified.isoformat() if obj.last_modified else None
                try:
                    tags = client.get_object_tags(bucket, key).to_dict()
                except Exception:
                    tags = {}
                files.append({
                        "filename": filename,
                        "path": key,
                        "size": obj.size or 0,
                        "last_modified": last_modified,
                        "upload_session_id": session_dir,
                        "project_id": tags.get("project-id"),
                        "commit_sha": tags.get("commit-sha"),
                        "archive_sha256": tags.get("archive-sha256"),
                    })
        except Exception:
            return []
        return files

    def find_rag_file(self, filename: str, session_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Locate an uploaded file, preferring an explicit session id."""
        matches = [
            f for f in self.list_rag_files()
            if f["filename"] == filename
        ]
        if not matches:
            return None
        if session_id:
            for m in matches:
                if m["upload_session_id"] == session_id:
                    return m
            return None
        matches.sort(key=lambda m: m.get("last_modified") or "", reverse=True)
        return matches[0]

    def get_rag_file_bytes(self, filename: str, session_id: Optional[str] = None) -> Optional[bytes]:
        match = self.find_rag_file(filename, session_id)
        if not match:
            return None
        client = self._get_client()
        try:
            response = client.get_object(self.tus_bucket, match["path"])
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        except Exception:
            return None

    def put_rag_file(self, session_id: str, filename: str, data: bytes, project_id: str | None = None, commit_sha: str | None = None) -> str:
        with self._identity_lock:
            if project_id is None and commit_sha is None:
                return self._put_archive(f"{RAG_UPLOADS_PREFIX}/{session_id}/{filename}", data, self._content_type(filename))
            if not project_id or not commit_sha:
                raise ValueError("project_id and commit_sha must be provided together")
            checksum = hashlib.sha256(data).hexdigest()
            for item in self.list_rag_files():
                if item.get("project_id") == project_id and item.get("commit_sha") == commit_sha:
                    if item.get("archive_sha256") != checksum:
                        raise ValueError("A different archive is already registered for this project and commit.")
            self._register_snapshot_identity(project_id, commit_sha, checksum)
            key = f"{RAG_UPLOADS_PREFIX}/{session_id}/{filename}"
            self._put_archive(key, data, self._content_type(filename))
            self.set_rag_identity(key, project_id, commit_sha, checksum)
            return key

    def set_rag_identity(self, key: str, project_id: str, commit_sha: str, checksum: str) -> None:
        with self._identity_lock:
            for item in self.list_rag_files():
                if (item.get("path") != key and item.get("project_id") == project_id
                        and item.get("commit_sha") == commit_sha
                        and item.get("archive_sha256") != checksum):
                    self._get_client().remove_object(self.tus_bucket, key)
                    raise ValueError("A different archive is already registered for this project and commit.")
            try:
                self._register_snapshot_identity(project_id, commit_sha, checksum)
            except ValueError:
                self._get_client().remove_object(self.tus_bucket, key)
                raise
            self._get_client().set_object_tags(self.tus_bucket, key, {
                "project-id": project_id,
                "commit-sha": commit_sha,
                "archive-sha256": checksum,
            })

    @staticmethod
    def _register_snapshot_identity(project_id: str, commit_sha: str, checksum: str) -> None:
        from telemetry.registry import SnapshotConflict, default_registry
        from telemetry.snapshot import Snapshot

        snapshot = Snapshot(project_id, commit_sha, checksum)
        registry = default_registry()
        with registry.lock(project_id, commit_sha):
            try:
                registry.register(snapshot)
            except SnapshotConflict as exc:
                raise ValueError("A different archive is already registered for this project and commit.") from exc

    def put_snapshot_archive(self, snapshot_key: str, checksum: str, data: bytes) -> str:
        """Immutable CI archive namespace, outside mutable legacy upload lists."""
        import re
        if not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in (snapshot_key, checksum)):
            raise ValueError("Invalid source archive identity")
        return self._put_archive(f"telemetry-sources/{snapshot_key}/{checksum}.zip", data, "application/zip")

    def get_snapshot_archive(self, key: str) -> bytes:
        """Read a previously staged immutable snapshot archive."""
        response = self._get_client().get_object(self.tus_bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def delete_snapshot_archive(self, key: str) -> None:
        if not key.startswith("telemetry-sources/") or ".." in key.split("/"):
            raise ValueError("Invalid snapshot archive key")
        self._get_client().remove_object(self.tus_bucket, key)

    def _put_archive(self, key: str, data: bytes, content_type: str) -> str:
        client = self._get_client()
        client.put_object(
            self.tus_bucket,
            key,
            data=io.BytesIO(data),
            length=len(data),
            content_type=content_type,
        )
        return key

    def delete_rag_file(self, filename: str, session_id: Optional[str] = None) -> bool:
        match = self.find_rag_file(filename, session_id)
        if not match:
            return False
        client = self._get_client()
        client.remove_object(self.tus_bucket, match["path"])
        return True

    def copy_object(self, source_key: str, dest_key: str) -> None:
        """Server-side copy within the same bucket (used to move tusd uploads
        into `rag-uploads/{session}/{filename}`)."""
        client = self._get_client()
        client.copy_object(
            self.tus_bucket,
            dest_key,
            CopySource(self.tus_bucket, source_key),
        )

    @staticmethod
    def _content_type(filename: str) -> str:
        return "application/zip" if filename.lower().endswith(".zip") else "application/octet-stream"


storage = Storage()
