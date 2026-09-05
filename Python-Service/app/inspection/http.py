"""HTTP surface for the inspection package (/api/inspect/*).

Endpoints are thin stateless facades over InspectionService. Query params
carry the ``collection`` (``sym_<stem>``) exactly as produced by
``sym_collection_for``; helper ``resolve_collection`` tolerates callers that
pass a bare project stem.

Tier-2/3 file reads accept absolute paths — intended for agent/orchestrator
use on already-extracted workspaces. Every failure surfaces as an explicit
HTTP 4xx/5xx JSON, never a silent partial answer.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from inspection.service import InspectionService, inspection_service, sym_collection_for

router = APIRouter(prefix="/api/inspect", tags=["Inspection"])


def _service() -> InspectionService:
    return inspection_service()


def _body(payload: dict, *keys: str) -> tuple:
    """Extract required string keys or raise 400."""
    missing = [k for k in keys if not isinstance(payload.get(k), str) or not payload.get(k)]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing or invalid: {', '.join(missing)}")
    return tuple(payload[k] for k in keys)


def _collection(payload: dict) -> str:
    collection = payload.get("collection")
    stem = payload.get("stem")
    if isinstance(collection, str) and collection:
        return collection
    if isinstance(stem, str) and stem:
        return sym_collection_for(stem)
    raise HTTPException(status_code=400, detail="Missing or invalid: collection or stem")


@router.post("/index", summary="Build the sym_<stem> symbol graph for a project")
def inspect_index(payload: dict) -> dict[str, Any]:
    project_dir, stem = _body(payload, "project_dir", "stem")
    return _service().index(project_dir, stem)


@router.post("/skeleton", summary="Tier-2 API surface of one file (no bodies)")
def inspect_skeleton(payload: dict) -> dict[str, Any]:
    (file_path,) = _body(payload, "file")
    return _service().skeleton(file_path)


@router.post("/attributes", summary="Tier-3 attributes + docblock tags of a file")
def inspect_attributes(payload: dict) -> list[dict[str, Any]]:
    (file_path,) = _body(payload, "file")
    class_name = payload.get("class")
    return _service().attributes(file_path, class_name if isinstance(class_name, str) else None)


@router.post("/method", summary="Tier-3 exact single-method source")
def inspect_method(payload: dict) -> dict[str, Any]:
    file_path, method = _body(payload, "file", "method")
    found = _service().method(file_path, method)
    if found is None:
        raise HTTPException(status_code=404, detail=f"Method {method} not found in {file_path}")
    return found


@router.post("/symbol", summary="Tier-4 resolve a symbol (fqn first, then by name)")
def inspect_symbol(payload: dict) -> list[dict[str, Any]]:
    collection = _collection(payload)
    (symbol,) = _body(payload, "symbol")
    return _service().resolve_symbol(collection, symbol)


@router.post("/implementations", summary="Tier-4 concrete implementations of a type")
def inspect_implementations(payload: dict) -> list[dict[str, Any]]:
    collection = _collection(payload)
    (fqn,) = _body(payload, "fqn")
    return _service().find_implementations(collection, fqn)


@router.post("/references", summary="Tier-4 use-sites of a symbol")
def inspect_references(payload: dict) -> list[dict[str, Any]]:
    collection = _collection(payload)
    (fqn,) = _body(payload, "fqn")
    return _service().find_references(collection, fqn)


@router.post("/call-hierarchy", summary="Tier-4 callees + callers of a method")
def inspect_call_hierarchy(payload: dict) -> dict[str, Any]:
    collection = _collection(payload)
    (fqn,) = _body(payload, "fqn")
    return _service().call_hierarchy(collection, fqn)
