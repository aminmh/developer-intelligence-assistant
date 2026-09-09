"""Upload and workspace application services."""

from __future__ import annotations

import base64
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt

from infra.php_tools import extract_zip_safely, safe_rmtree
from infra.settings import settings


class UploadService:
    """Own upload token creation, binary storage, and workspace extraction."""

    def create_tus_session(self, filename: str, size: int) -> dict[str, Any]:
        upload_session_id = str(uuid.uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=60)
        token = jwt.encode(
            {
                "sub": "user-123",
                "upload_session_id": upload_session_id,
                "filename": filename,
                "maximum_size": size,
                "upload_kind": "rag-document",
                "exp": expires_at,
            },
            settings.upload_token_secret,
            algorithm="HS256",
        )
        return {
            "upload_session_id": upload_session_id,
            "tus_endpoint": settings.tus_public_url,
            "upload_token": token,
            "expires_at": expires_at.isoformat(),
        }

    @staticmethod
    def decode_content(content: str) -> bytes:
        try:
            return base64.b64decode(content, validate=True)
        except Exception as exc:
            raise ValueError("Invalid base64 content.") from exc

    def store_direct(self, storage: Any, filename: str, content: str) -> dict[str, Any]:
        decoded = self.decode_content(content)
        upload_session_id = str(uuid.uuid4())
        storage.put_rag_file(upload_session_id, filename, decoded)
        return {
            "upload_session_id": upload_session_id,
            "filename": filename,
            "type": "zip",
            "text": f"Uploaded ZIP archive: {filename} (binary content, not text-indexed).",
            "size": len(decoded),
        }

    def extract_workspace(self, workspace_id: str, zip_data: bytes) -> str:
        workspace_dir = Path(settings.workspace_root) / workspace_id
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir, ignore_errors=True)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        try:
            extract_zip_safely(zip_data, str(workspace_dir))
        except Exception:
            shutil.rmtree(workspace_dir, ignore_errors=True)
            raise
        return str(workspace_dir)

    def destroy_workspace(self, workspace_id: str) -> None:
        workspace_dir = Path(settings.workspace_root) / workspace_id
        if workspace_dir.exists():
            safe_rmtree(str(workspace_dir))


upload_service = UploadService()

