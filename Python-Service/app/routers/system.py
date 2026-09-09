"""Provider health and system integration HTTP endpoints."""

from __future__ import annotations

import json
import logging
import time
import urllib.request

from fastapi import APIRouter

import runtime.llm as llm_module
from infra.settings import settings
from runtime.lmstudio import lmstudio_load, lmstudio_models

router = APIRouter(tags=["Health & System"])
_RUNPOD_API = "https://rest.runpod.io/v1"


def _probe_local_llm() -> dict:
    try:
        req = urllib.request.Request(
            f"{settings.ollama_base_url.rstrip('/')}/api/tags",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"configured": True, "reachable": True, "models": len(data.get("models") or [])}
    except Exception as exc:  # noqa: BLE001 — health probes never raise
        logging.getLogger(__name__).warning("Local LLM health probe failed: %s", exc)
        return {"configured": True, "reachable": False, "models": None}


def _probe_remote_llm() -> dict:
    base_url = (settings.llm_remote_base_url or "").strip()
    if not base_url:
        return {"configured": False, "reachable": False, "model": None, "error": None}
    try:
        headers = {"Accept": "application/json"}
        api_key = (settings.llm_remote_api_key or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(f"{base_url.rstrip('/')}/v1/models", headers=headers)
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("data") or []
        return {"configured": True, "reachable": True, "model": items[0].get("id") if items else None, "error": None}
    except Exception as exc:  # noqa: BLE001 — health probes never raise
        return {"configured": True, "reachable": False, "model": None, "error": str(exc)}


def _probe_runpod() -> dict:
    api_key = (settings.runpod_api_key or "").strip()
    if not api_key:
        return {"configured": False, "reachable": False, "endpoint_count": 0, "error": None}
    try:
        req = urllib.request.Request(
            f"{_RUNPOD_API}/endpoints",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"configured": True, "reachable": True, "endpoint_count": len(data) if isinstance(data, list) else 0, "error": None}
    except Exception as exc:  # noqa: BLE001 — health probes never raise
        return {"configured": True, "reachable": False, "endpoint_count": 0, "error": str(exc)}


@router.get("/health/llm", summary="LLM Provider Health")
def health_llm():
    return {"local": _probe_local_llm(), "remote": _probe_remote_llm(), "runpod": _probe_runpod()}


@router.get("/api/runpod/endpoints", tags=["RunPod Serverless"], summary="List RunPod Endpoints")
def runpod_list_endpoints():
    api_key = (settings.runpod_api_key or "").strip()
    if not api_key:
        return {"ok": False, "endpoints": [], "error": "not configured (no RunPod API key)"}
    try:
        req = urllib.request.Request(
            f"{_RUNPOD_API}/endpoints",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"ok": True, "endpoints": data if isinstance(data, list) else [], "error": None}
    except Exception as exc:  # noqa: BLE001 — endpoint listing is best-effort
        return {"ok": False, "endpoints": [], "error": str(exc)}


@router.post("/health/test/runpod", tags=["RunPod Serverless"], summary="Test RunPod Connection")
def runpod_test_connection():
    api_key = (settings.runpod_api_key or "").strip()
    started = time.monotonic()
    if not api_key:
        return {"ok": False, "latency_ms": 0, "detail": "not configured (no RunPod API key)"}
    try:
        req = urllib.request.Request(
            f"{_RUNPOD_API}/endpoints",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000), "detail": f"authenticated · {len(data) if isinstance(data, list) else 0} endpoint(s) on account"}
    except Exception as exc:  # noqa: BLE001 — connection test is best-effort
        return {"ok": False, "latency_ms": round((time.monotonic() - started) * 1000), "detail": str(exc)[:300]}


@router.post("/health/test/remote", tags=["LLM Providers"], summary="Test External OpenAI-Compatible Connection")
def remote_test_connection():
    return llm_module.test_remote()


@router.get("/lmstudio/models", tags=["LM Studio"], summary="List LM Studio Models")
def lmstudio_models_endpoint(base_url: str | None = None):
    result = lmstudio_models(base_url or settings.llm_remote_base_url or "")
    return {"success": result.pop("ok"), **result}


@router.post("/lmstudio/load", tags=["LM Studio"], summary="Load Model into LM Studio")
def lmstudio_load_endpoint(body: dict):
    model_id = (body.get("model") or "").strip()
    base_url = body.get("base_url") or settings.llm_remote_base_url or ""
    result = lmstudio_load(base_url, model_id)
    return {"success": result.pop("ok"), **result}

