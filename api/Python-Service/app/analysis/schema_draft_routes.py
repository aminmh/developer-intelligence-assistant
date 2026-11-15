"""Internal API for schema drafts, called by the authenticated web proxy."""

from __future__ import annotations

import hmac
import hashlib
import json
import os
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from analysis.analysis_store import DB_PATH, analysis_store
from analysis.schema_drafts import DraftConflict, DraftNotFound, SchemaDraftStore
from analysis.schema_sources import schema_from_analysis
from analysis.schema_inspection import InspectionError, inspect_columns, compare_column_names
from analysis.schema_migrations import UnsupportedMigration, preview, validate_migration_timestamp
from analysis.schema_runtime import InvalidRuntimeReceipt, StaleRuntimeSource, runtime_sources, verify_runtime_extraction
from telemetry.registry import default_registry

router = APIRouter(prefix="/schema-drafts", tags=["Schema drafts"])
MAX_REQUEST_BYTES = 9 * 1024 * 1024


def _authorize(request: Request) -> None:
    secret = os.environ.get("RAG_CALLBACK_SECRET", "")
    supplied = request.headers.get("x-internal-secret", "")
    user = request.headers.get("x-user-id", "")
    if (len(secret) < 32 or not hmac.compare_digest(secret.encode(), supplied.encode())
            or not user or len(user) > 512):
        raise HTTPException(401, "Internal service credentials and user identity required")


def _store() -> SchemaDraftStore:
    return SchemaDraftStore(DB_PATH)


async def _payload(request: Request) -> dict:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_REQUEST_BYTES:
            raise HTTPException(413, "Draft request exceeds the size limit")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(422, "Draft request must be valid JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(422, "Draft request must be an object")
    return value


def _response(draft: dict) -> dict:
    # Saved profiles and imports do not establish complete database metadata.
    return {**draft, "baseline_coverage": "unverified"}


def _preview_token(result: dict, user: str) -> str:
    payload = json.dumps({"user": user, "preview": result}, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    return hmac.new(os.environ['RAG_CALLBACK_SECRET'].encode(), payload, hashlib.sha256).hexdigest()


@router.get("", dependencies=[Depends(_authorize)])
def list_drafts():
    return {"drafts": _store().list()}


@router.post("/analysis/{analysis_id}", status_code=201, dependencies=[Depends(_authorize)])
def create_analysis_draft(analysis_id: str):
    analysis = analysis_store.get(analysis_id)
    if analysis is None:
        raise HTTPException(404, "Analysis not found")
    try:
        baseline = schema_from_analysis(analysis.get("profile") or {}, analysis_id)
        return _response(_store().create(baseline, analysis_id=analysis_id))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "A draft already exists or the analysis was removed") from exc
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, str(exc)[:500]) from exc


@router.post("/import", status_code=201, dependencies=[Depends(_authorize)])
async def create_import_draft(request: Request):
    payload = await _payload(request)
    if set(payload) != {"schema"}:
        raise HTTPException(422, "Supply a structured schema")
    try:
        return _response(_store().create(payload["schema"]))
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, str(exc)[:500]) from exc


@router.get("/runtime/sources", dependencies=[Depends(_authorize)])
def list_runtime_sources(project_id: str, environment: str | None = None):
    try:
        return {"sources": runtime_sources(default_registry(), project_id, environment)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/runtime", status_code=201, dependencies=[Depends(_authorize)])
async def create_runtime_draft(request: Request):
    payload = await _payload(request)
    try:
        schema, provenance = verify_runtime_extraction(
            payload, user=request.headers['x-user-id'],
            origin=request.headers.get('x-application-origin', ''), registry=default_registry())
        return _response(_store().create(schema, provenance=provenance))
    except InvalidRuntimeReceipt as exc:
        raise HTTPException(403, str(exc)) from exc
    except StaleRuntimeSource as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, str(exc)[:500]) from exc


@router.get("/{draft_id}", dependencies=[Depends(_authorize)])
def get_draft(draft_id: str):
    try:
        return _response(_store().get(draft_id))
    except DraftNotFound as exc:
        raise HTTPException(404, "Draft not found") from exc


@router.delete("/{draft_id}", dependencies=[Depends(_authorize)])
def delete_draft(draft_id: str):
    try:
        _store().delete(draft_id)
    except DraftNotFound as exc:
        raise HTTPException(404, "Draft not found") from exc
    return {"deleted": True}


@router.put("/{draft_id}", dependencies=[Depends(_authorize)])
async def save_draft(draft_id: str, request: Request):
    payload = await _payload(request)
    if (set(payload) != {"schema", "layout", "revision"}
            or type(payload.get("revision")) is not int or payload["revision"] < 1):
        raise HTTPException(422, "Supply schema, layout, and the current revision")
    try:
        return _response(_store().save(draft_id, payload["schema"], payload["layout"], payload["revision"]))
    except DraftNotFound as exc:
        raise HTTPException(404, "Draft not found") from exc
    except DraftConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, str(exc)[:500]) from exc


@router.post("/{draft_id}/migration-preview", dependencies=[Depends(_authorize)])
async def migration_preview(draft_id: str, request: Request):
    payload = await _payload(request)
    if (set(payload) != {"framework", "database", "revision", "migration_timestamp"}
            or not isinstance(payload.get("framework"), str)
            or not isinstance(payload.get("database"), str)
            or len(payload["framework"]) > 64 or len(payload["database"]) > 64
            or type(payload.get("revision")) is not int or payload["revision"] < 1):
        raise HTTPException(422, "Supply framework, database, migration timestamp, and the saved revision")
    try:
        validate_migration_timestamp(payload['migration_timestamp'])
        draft = _store().get(draft_id)
        if draft['revision'] != payload['revision']:
            raise HTTPException(409, "Draft changed; reload before previewing")
        result = await run_in_threadpool(preview, draft, payload['framework'], payload['database'], payload['migration_timestamp'])
        return {**result, 'preview_token': _preview_token(result, request.headers['x-user-id'])}
    except DraftNotFound as exc:
        raise HTTPException(404, "Draft not found") from exc
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, str(exc)[:500]) from exc


@router.post("/{draft_id}/migration-download", dependencies=[Depends(_authorize)])
async def migration_download(draft_id: str, request: Request):
    payload = await _payload(request)
    if (set(payload) != {"framework", "database", "revision", "confirmed", "migration_timestamp", "preview_token", "reviewed_preview"}
            or not isinstance(payload.get("reviewed_preview"), dict)
            or type(payload.get("confirmed")) is not bool
            or not isinstance(payload.get("preview_token"), str) or len(payload['preview_token']) != 64
            or not isinstance(payload.get("framework"), str)
            or not isinstance(payload.get("database"), str)
            or len(payload["framework"]) > 64 or len(payload["database"]) > 64
            or type(payload.get("revision")) is not int or payload["revision"] < 1):
        raise HTTPException(422, "Supply framework, database, migration timestamp, saved revision, and confirmation")
    try:
        validate_migration_timestamp(payload['migration_timestamp'])
        draft = _store().get(draft_id)
        if draft['revision'] != payload['revision']:
            raise HTTPException(409, "Draft changed; reload before downloading")
        result = payload['reviewed_preview']
        if not hmac.compare_digest(_preview_token(result, request.headers['x-user-id']).encode(), payload['preview_token'].encode('utf-8')):
            raise HTTPException(409, 'Migration preview changed; review the current candidate')
        if any(result.get(k) != payload[k] for k in ('framework', 'database', 'revision', 'migration_timestamp')) or result.get('draft_id') != draft_id:
            raise HTTPException(409, 'Migration preview changed; review the current candidate')
        if result['generation_blocked']:
            raise HTTPException(422, result['generation_error'])
        if result['requires_confirmation'] and not payload['confirmed']:
            raise HTTPException(409, "Confirm destructive or ambiguous changes before downloading")
        if len(result['files']) != 1:
            raise HTTPException(422, "This adapter does not provide a single downloadable file")
        file = result['files'][0]
        return Response(file['content'], media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{file["name"]}"',
                                 "X-Content-Type-Options": "nosniff"})
    except DraftNotFound as exc:
        raise HTTPException(404, "Draft not found") from exc
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, str(exc)[:500]) from exc


@router.post("/{draft_id}/inspect", dependencies=[Depends(_authorize)])
async def inspect_draft(draft_id: str, request: Request):
    payload = await _payload(request)
    if set(payload) != {"target", "consent"} or payload.get("consent") is not True or not isinstance(payload.get("target"), str):
        raise HTTPException(422, "Supply a configured target and explicit consent to read its metadata")
    try:
        draft = _store().get(draft_id)
        report = await run_in_threadpool(inspect_columns, payload["target"])
        report["name_comparison"] = compare_column_names(draft["baseline"], report)
        return report
    except DraftNotFound as exc:
        raise HTTPException(404, "Draft not found") from exc
    except InspectionError as exc:
        raise HTTPException(422, str(exc)) from exc
