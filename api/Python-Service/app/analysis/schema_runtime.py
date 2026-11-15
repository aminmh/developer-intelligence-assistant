"""Verify helper extraction receipts against the reporting Runtime inventory."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from analysis.schema_model import Schema
from telemetry.registry import SnapshotRegistry

Text = Annotated[str, Field(min_length=1, max_length=512)]
Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class RuntimeCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    warnings: list[str] = Field(max_length=10000)
    complete: bool


class RuntimeProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    project_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
    environment: Text
    host_id: Fingerprint
    container_id: Fingerprint
    image_id: Text
    engine: Literal["postgresql", "mysql", "mariadb"]
    engine_version: Text
    database: Text
    schemas: list[Text] = Field(max_length=10000)
    extracted_at: Text
    coverage: RuntimeCoverage

    @model_validator(mode="after")
    def validate_extraction(self):
        try:
            extracted = datetime.fromisoformat(self.extracted_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Extraction time must be an ISO timestamp") from exc
        if extracted.tzinfo is None:
            raise ValueError("Extraction time requires a timezone")
        if len(self.schemas) != len(set(self.schemas)):
            raise ValueError("Extraction schemas must be distinct")
        return self


def runtime_sources(registry: SnapshotRegistry, project_id: str, environment: str | None = None) -> list[dict]:
    """Return current candidates, retaining the authenticated reporter's host identity."""
    if not isinstance(project_id, str) or not project_id or len(project_id) > 128:
        raise ValueError("Choose a Runtime project")
    fields = ("project_id", "environment", "host_id", "container_id", "image_id", "service", "state", "updated_at")
    result = []
    for item in registry.running_deployments(project_id):
        if environment is not None and item["environment"] != environment:
            continue
        # Legacy short container IDs cannot authorize an exact Docker operation.
        if any(not isinstance(item.get(key), str) for key in fields):
            continue
        if len(item["container_id"]) != 64 or any(c not in "0123456789abcdef" for c in item["container_id"]):
            continue
        result.append({key: item[key] for key in fields})
    return result


class InvalidRuntimeReceipt(ValueError):
    """A result did not belong to this authenticated user and application."""


class StaleRuntimeSource(ValueError):
    """The extracted container no longer matches the current reporting inventory."""


def verify_runtime_extraction(payload: dict, *, user: str, origin: str,
                              registry: SnapshotRegistry) -> tuple[Schema, RuntimeProvenance]:
    required = {"schema", "provenance", "receipt", "sub", "origin", "expires_at"}
    if set(payload) != required:
        raise InvalidRuntimeReceipt("Supply the complete signed Runtime extraction result")
    parsed_origin = urlsplit(origin)
    if (not origin or parsed_origin.scheme not in ("http", "https") or not parsed_origin.netloc
            or parsed_origin.path or parsed_origin.query or parsed_origin.fragment or parsed_origin.username):
        raise InvalidRuntimeReceipt("Application origin required")
    expires = payload["expires_at"]
    if (payload["sub"] != user or payload["origin"] != origin or type(expires) is not int
            or expires <= time.time() or expires > time.time() + 125):
        raise InvalidRuntimeReceipt("Runtime extraction expired or belongs to a different user or application")
    receipt = payload["receipt"]
    if not isinstance(receipt, str) or len(receipt) != 64 or any(c not in "0123456789abcdef" for c in receipt):
        raise InvalidRuntimeReceipt("Invalid Runtime extraction signature")
    secret = os.environ.get("RAG_CALLBACK_SECRET", "")
    if len(secret) < 32:
        raise InvalidRuntimeReceipt("Runtime signing is not configured")
    signed = {key: payload[key] for key in required - {"receipt"}}
    try:
        canonical = json.dumps(signed, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    except (ValueError, TypeError, UnicodeError) as exc:
        raise InvalidRuntimeReceipt("Invalid Runtime extraction signature") from exc
    key = hmac.new(secret.encode(), b"devassistant-runtime-schema-result-v1", hashlib.sha256).digest()
    expected = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(receipt, expected):
        raise InvalidRuntimeReceipt("Invalid Runtime extraction signature")
    provenance = RuntimeProvenance.model_validate(payload["provenance"])
    schema = Schema.model_validate(payload["schema"])
    identity = ("project_id", "environment", "host_id", "container_id", "image_id")
    if not any(all(source[name] == getattr(provenance, name) for name in identity)
               for source in runtime_sources(registry, provenance.project_id, provenance.environment)):
        raise StaleRuntimeSource("Runtime source changed or stopped reporting; refresh and extract again")
    return schema, provenance
