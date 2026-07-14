from pydantic import BaseModel, Field


class UploadSessionRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)


def get_current_user():
    # Replace with your authentication implementation.
    return {"id": "user-123"}

import hashlib
import hmac
import os

from fastapi import HTTPException

TUSD_HOOK_SECRET = os.environ["TUSD_HOOK_SECRET"]


def verify_hook(body: bytes, signature: str | None) -> None:
    if not signature:
        raise HTTPException(401, "Missing hook signature")

    expected = hmac.new(
        TUSD_HOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "Invalid hook signature")



def resolve_object_key(upload: dict) -> str:
    # Map tusd's upload ID to the relevant MinIO/S3 object key according
    # to your tusd storage configuration.
    return f"projects/{upload['ID']}"
