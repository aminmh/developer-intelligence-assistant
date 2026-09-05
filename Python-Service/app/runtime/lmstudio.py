"""LM Studio / Bionic client — list models and warm-load one into the engine.

The server (LM Studio desktop or the Bionic app on :1234) exposes an
OpenAI-compatible API at ``{base}/v1`` plus a model listing at
``{base}/api/v0/models`` (rich metadata: type, publisher, compatibility_type).

There is no explicit "load model" HTTP endpoint on these servers — a model is
loaded lazily on the first chat request. ``lmstudio_load`` therefore performs
a minimal 1-token chat probe so the selected model ends up resident in the MLX
engine (pre-load) and the first real answer is not cold.

This module is deliberately stdlib-only (``urllib``), mirroring
``sandbox_runtime.py`` — no extra HTTP dependency.
"""

from __future__ import annotations

import json
import time
import urllib.error as error
import urllib.request
from typing import Any


def normalize_base_url(base_url: str) -> str:
    """Return ``base_url`` without a trailing slash or ``/v1`` suffix.

    Endpoints are joined as ``{base}/v1/...`` so a stored value that already
    includes ``/v1`` must be stripped first.
    """
    url = (base_url or "").strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")].rstrip("/")
    return url


def _http_json(url: str, body: dict | None = None, timeout: float = 15.0) -> Any:
    """Perform a GET (or POST when ``body`` given) and parse the JSON reply."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def lmstudio_models(base_url: str, timeout: float = 15.0) -> dict:
    """List models available on the server.

    Prefers the richer ``/api/v0/models`` payload (type, publisher,
    compatibility_type) and falls back to the OpenAI-style ``/v1/models``
    list. Returns ``{"ok": True, "models": [{"id", "engine", "type",
    "quantization"}]}`` where ``engine`` is ``"mlx"``, ``"llama.cpp"`` or
    ``"unknown"`` and ``type`` is ``"llm"``, ``"embeddings"`` or ``"unknown"``.
    """
    base = normalize_base_url(base_url)
    if not base:
        return {"ok": False, "models": [], "detail": "No base URL configured"}

    started = time.monotonic()
    try:
        payload = _http_json(f"{base}/api/v0/models", timeout=timeout)
        models = _parse_v0_models(payload)
    except Exception:  # noqa: BLE001 — v0 list is optional; fall back to /v1
        try:
            payload = _http_json(f"{base}/v1/models", timeout=timeout)
            models = _parse_v1_models(payload)
        except Exception as exc:  # noqa: BLE001 — report any failure shape to the UI
            return {
                "ok": False,
                "models": [],
                "detail": f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__,
                "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
            }

    models.sort(key=lambda m: (m["engine"] != "mlx", m["id"]))
    return {
        "ok": True,
        "models": models,
        "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
    }


def _parse_v0_models(payload: Any) -> list[dict]:
    """Parse the /api/v0/models payload (LM Studio / Bionic rich listing)."""
    models: list[dict] = []
    for entry in payload.get("data", []) if isinstance(payload, dict) else []:
        model_id = entry.get("id") or ""
        if not model_id:
            continue
        compatibility = str(entry.get("compatibility_type") or "")
        publisher = str(entry.get("publisher") or "")
        engine = compatibility or ("mlx" if "mlx" in publisher else "unknown")
        mtype = str(entry.get("type") or "unknown")
        models.append(
            {"id": model_id, "engine": engine, "type": mtype, "quantization": None}
        )
    return models


def _parse_v1_models(payload: Any) -> list[dict]:
    """Parse the OpenAI-style /v1/models payload."""
    models: list[dict] = []
    for entry in payload.get("data", []) if isinstance(payload, dict) else []:
        model_id = entry.get("id") or ""
        if not model_id:
            continue
        meta = entry.get("meta") or {}
        engine = str(meta.get("engine") or "unknown")
        quantization = str(meta.get("quantization") or "") or None
        mtype = str(entry.get("type") or meta.get("type") or "unknown")
        models.append(
            {
                "id": model_id,
                "engine": engine,
                "type": mtype,
                "quantization": quantization,
            }
        )
    return models


def lmstudio_load(base_url: str, model_id: str, timeout: float = 120.0) -> dict:
    """Warm-load ``model_id`` into the engine.

    The server auto-loads a model on the first chat request, so "load" is a
    minimal 1-token chat probe — the model ends up resident in the MLX engine
    and the first real answer is not cold. Returns
    ``{"ok": True, "status": ..., "detail": ...}`` on success.
    """
    base = normalize_base_url(base_url)
    if not base:
        return {"ok": False, "detail": "No base URL configured"}
    if not (model_id or "").strip():
        return {"ok": False, "detail": "No model selected"}

    started = time.monotonic()
    body = {
        "model": model_id.strip(),
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "max_tokens": 1,
        "stream": False,
    }
    try:
        payload = _http_json(f"{base}/v1/chat/completions", body=body, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — report any failure shape to the UI
        return {
            "ok": False,
            "detail": f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__,
            "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
        }

    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        detail = payload.get("error") if isinstance(payload, dict) else None
        return {
            "ok": False,
            "detail": str(detail or "Model load probe failed"),
            "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
        }
    return {
        "ok": True,
        "status": "loaded",
        "detail": f"Loaded {model_id.strip()} into the engine",
        "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
    }
