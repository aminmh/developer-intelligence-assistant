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
                files.append(
                    {
                        "filename": filename,
                        "path": key,
                        "size": obj.size or 0,
                        "last_modified": last_modified,
                        "upload_session_id": session_dir,
                    }
                )
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

    def put_rag_file(self, session_id: str, filename: str, data: bytes) -> str:
        client = self._get_client()
        key = f"{RAG_UPLOADS_PREFIX}/{session_id}/{filename}"
        client.put_object(
            self.tus_bucket,
            key,
            data=io.BytesIO(data),
            length=len(data),
            content_type=self._content_type(filename),
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
