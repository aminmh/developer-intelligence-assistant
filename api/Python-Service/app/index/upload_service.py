"""Upload and workspace application services."""

from __future__ import annotations

import base64
import hashlib
import shutil
import uuid
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt

from infra.php_tools import extract_zip_safely, safe_rmtree
from infra.settings import settings


class UploadService:
    """Own upload token creation, binary storage, and workspace extraction."""

    @staticmethod
    def validate_identity(project_id: str, commit_sha: str | None) -> tuple[str, str | None]:
        project_id = project_id.strip() if isinstance(project_id, str) else ""
        if commit_sha is not None and not isinstance(commit_sha, str):
            raise ValueError("commit_sha must be a full Git SHA or omitted.")
        commit_sha = (commit_sha.strip().lower() or None) if isinstance(commit_sha, str) else None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", project_id):
            raise ValueError("project_id must be a 1-128 character ASCII slug.")
        if commit_sha is not None and not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit_sha):
            raise ValueError("commit_sha must be a full lowercase Git SHA-1 or SHA-256.")
        return project_id, commit_sha

    def create_tus_session(self, filename: str, size: int, project_id: str | None = None, commit_sha: str | None = None) -> dict[str, Any]:
        identity = {}
        if project_id is not None or commit_sha is not None:
            project_id, commit_sha = self.validate_identity(project_id or "", commit_sha or "")
            identity = {"project_id": project_id, "commit_sha": commit_sha}
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
                **identity,
            },
            settings.upload_token_secret,
            algorithm="HS256",
        )
        return {
            "upload_session_id": upload_session_id,
            "tus_endpoint": settings.tus_public_url,
            "upload_token": token,
            "expires_at": expires_at.isoformat(),
            **identity,
        }

    @staticmethod
    def decode_content(content: str) -> bytes:
        try:
            return base64.b64decode(content, validate=True)
        except Exception as exc:
            raise ValueError("Invalid base64 content.") from exc

    def store_direct(self, storage: Any, filename: str, content: str, project_id: str | None = None, commit_sha: str | None = None) -> dict[str, Any]:
        decoded = self.decode_content(content)
        upload_session_id = str(uuid.uuid4())
        if project_id is None and commit_sha is None:
            storage.put_rag_file(upload_session_id, filename, decoded)
            identity = {}
        else:
            project_id, commit_sha = self.validate_identity(project_id or "", commit_sha or "")
            storage.put_rag_file(upload_session_id, filename, decoded, project_id, commit_sha)
            from telemetry.snapshot import Snapshot
            from telemetry.registry import default_registry
            snapshot = Snapshot(project_id, commit_sha, hashlib.sha256(decoded).hexdigest())
            current = default_registry().get(project_id, snapshot.reference)
            identity = {"project_id": project_id, "commit_sha": commit_sha,
                        "source_ref": snapshot.reference, "revision_id": current["revision_id"]}
        return {
            "upload_session_id": upload_session_id,
            "filename": filename,
            "type": "zip",
            "text": f"Uploaded ZIP archive: {filename} (binary content, not text-indexed).",
            "size": len(decoded),
            "archive_sha256": hashlib.sha256(decoded).hexdigest(),
            **identity,
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
