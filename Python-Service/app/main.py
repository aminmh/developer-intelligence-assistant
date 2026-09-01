from contextlib import asynccontextmanager
from typing import Annotated
from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from indexer import CodeIntelligenceIndex
from sandbox_runtime import sandbox_available
from settings import settings
import llm as llm_module
from router import classify as semantic_classify
from router import submit_feedback as router_submit_feedback
from intent import extract_intent
from orchestrator import orchestrator
from exception_logger import get_exception_logger, install_exception_hooks
from settings_store import (
    RAG_CONFIG_DEFAULTS,
    SECRET_KEYS,
    MASKED_VALUE,
    build_config,
    mask_config,
    settings_store,
)
from models import (
    HealthResponse,
    RebuildResponse,
    SearchResponse,
    SymbolLookupResponse,
    CallersResponse,
    CalleesResponse,
    DownloadJobResponse,
)
import jwt as pyjwt
import uuid
import os
import re
import json
import base64
import logging
import threading
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from qdrant_client import QdrantClient
from jinja2 import Template


# Matches tusd's `-max-size` (2 GiB).
MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024


def sanitize_collection_name(filename: str) -> str:
    """Convert 'my-project.zip' → 'my_project' — safe for Qdrant collection names."""
    stem = Path(filename).stem
    name = re.sub(r"[^a-zA-Z0-9_]", "_", stem)
    name = re.sub(r"_+", "_", name)
    return name.strip("_") or "unnamed"


def collection_exists(collection_name: str) -> bool:
    """Check whether a Qdrant collection already exists (skip re-indexing)."""
    try:
        qdrant = QdrantClient(url=settings.qdrant_url)
        existing = qdrant.get_collections().collections
        return any(c.name == collection_name for c in existing)
    except Exception:
        return False


def _mint_download_token(download_id: str, filename: str) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    return pyjwt.encode(
        {"download_id": download_id, "filename": filename, "exp": expires_at},
        settings.download_token_secret,
        algorithm="HS256",
    )


def setup_logger(name: str, filename: str) -> logging.Logger:
    """Create a file-based logger for tracing the download/index pipeline."""
    log_path = Path(settings.log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = logging.FileHandler(log_path / filename)
        handler.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)s  %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        ))
        logger.addHandler(handler)
    return logger


dl_logger = setup_logger("download_jobs", "download-jobs.log")


def write_status(download_id: str, status: str, total: int, indexed: int,
                 collection_name: str = "") -> None:
    """Write indexing progress to a status file for frontend polling."""
    try:
        status_path = Path(settings.download_dir) / f"{download_id}.status.json"
        status_path.write_text(json.dumps({
            "status": status,
            "total": total,
            "indexed": indexed,
            "collection_name": collection_name,
        }))
    except Exception:
        pass  # status file is best-effort — don't fail the job


def index_code_chunks_background(download_id: str, filename: str,
                                 chunks_list: list[dict], collection_name: str) -> None:
    """Index code chunks through the property-graph pipeline in a background thread."""
    dl_logger.info("Background indexing started: collection=%s count=%s",
                   collection_name, len(chunks_list))
    write_status(download_id, "embedding", len(chunks_list), 0, collection_name)

    try:
        from llamaindex_layer import CodePropertyGraph

        result = CodePropertyGraph().ingest(chunks_list, collection_name)
        ingested = int(result.get("ingested", 0) or 0)
        dl_logger.info(
            "Background indexing complete: collection=%s ingested=%s graph_nodes=%s relations=%s",
            collection_name,
            ingested,
            result.get("graph_nodes", 0),
            result.get("graph_relations", 0),
        )
        write_status(download_id, "done", len(chunks_list),
                     ingested, collection_name)

    except Exception as e:
        dl_logger.error("Background indexing failed for '%s': %s",
                        collection_name, e, exc_info=True)
        write_status(download_id, "error", len(chunks_list), 0, collection_name)


service = CodeIntelligenceIndex()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        if Path(settings.report_path).is_file():
            service.rebuild(settings.report_path)
    except Exception:
        pass  # First startup or empty report — skip rebuild

    # Warm the language servers so the first answer isn't slow (they index /
    # build caches on first use). Best-effort + non-blocking.
    if getattr(settings, "lsp_validation_enabled", True):
        try:
            from lsp import warmup

            warmup()
        except Exception:
            pass

    yield

    try:
        from lsp import shutdown

        shutdown()
    except Exception:
        pass


tags_metadata = [
    {
        "name": "Health & System",
        "description": "Service health checks and index rebuild operations.",
    },
    {
        "name": "Code Intelligence",
        "description": "Semantic search, symbol lookup, and call-graph traversal over indexed code symbols.",
    },
    {
        "name": "Upload & TUS",
        "description": "Resumable upload session management and tusd webhook integration for ZIP file processing.",
    },
]

app = FastAPI(
    title="LlamaIndex Code Intelligence Service",
    description=(
        "A FastAPI service that indexes PHP code symbols using LlamaIndex and Qdrant, "
        "providing semantic search, call-graph analysis, and resumable upload (TUS) support. "
        "Ingests a JSON code-analysis report, builds a vector index and an in-memory call graph, "
        "and exposes REST endpoints for querying symbols, navigating callers/callees, and "
        "orchestrating AST analysis pipelines."
    ),
    version="1.0.0",
    contact={
        "name": "Developr Assistant",
        "url": "https://github.com/anomalyco/developr-assistant",
    },
    license_info={
        "name": "MIT",
    },
    openapi_tags=tags_metadata,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


install_exception_hooks()
_exception_logger = get_exception_logger()


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    _exception_logger.error(
        "Unhandled exception in %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


@app.get(
    "/health",
    tags=["Health & System"],
    summary="Service Health Check",
    description=(
        "Returns the current health status of the service, including the number of "
        "indexed symbols, call-graph edges, unresolved edges, and the active Qdrant "
        "collection name."
    ),
    response_model=HealthResponse,
)
def health():
    return HealthResponse(
        status="ok",
        symbols=service.symbols_count,
        edges=service.edges_count,
        unresolved_edges=service.unresolved_edges_count,
        collection=settings.qdrant_collection,
    )


def _probe_local_llm() -> dict:
    """Probe the local Ollama server; never raises."""
    try:
        req = urllib.request.Request(
            f"{settings.ollama_base_url.rstrip('/')}/api/tags",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = data.get("models") or []
        return {"configured": True, "reachable": True, "models": len(models)}
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Local LLM health probe failed: %s", exc
        )
        return {"configured": True, "reachable": False, "models": None}


def _probe_remote_llm() -> dict:
    """Probe the optional remote vLLM server; never raises."""
    base_url = (settings.llm_remote_base_url or "").strip()
    if not base_url:
        return {"configured": False, "reachable": False, "model": None, "error": None}
    try:
        headers = {"Accept": "application/json"}
        api_key = (settings.llm_remote_api_key or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/v1/models",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("data") or []
        model = items[0].get("id") if items else None
        return {"configured": True, "reachable": True, "model": model, "error": None}
    except Exception as exc:
        return {"configured": True, "reachable": False, "model": None, "error": str(exc)}


_RUNPOD_API = "https://rest.runpod.io/v1"


def _probe_runpod() -> dict:
    """Probe the RunPod REST API (list endpoints) with the stored key; never raises."""
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
        count = len(data) if isinstance(data, list) else 0
        return {"configured": True, "reachable": True, "endpoint_count": count, "error": None}
    except Exception as exc:
        return {"configured": True, "reachable": False, "endpoint_count": 0, "error": str(exc)}


@app.get(
    "/health/llm",
    tags=["Health & System"],
    summary="LLM Provider Health",
    description=(
        "Checks reachability of the local Ollama, optional remote vLLM LLM "
        "providers, and the RunPod serverless account. Never raises — each "
        "provider reports its own status."
    ),
)
def health_llm():
    """LLM provider health: local Ollama + optional remote vLLM + RunPod."""
    return {
        "local": _probe_local_llm(),
        "remote": _probe_remote_llm(),
        "runpod": _probe_runpod(),
    }


@app.get(
    "/api/runpod/endpoints",
    tags=["RunPod Serverless"],
    summary="List RunPod Endpoints",
    description=(
        "Lists the account's RunPod serverless endpoints via the RunPod REST "
        "API. Returns {ok, endpoints, error}; never raises."
    ),
)
def runpod_list_endpoints():
    """List RunPod serverless endpoints for the stored account key."""
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
        endpoints = data if isinstance(data, list) else []
        return {"ok": True, "endpoints": endpoints, "error": None}
    except Exception as exc:
        return {"ok": False, "endpoints": [], "error": str(exc)}


@app.post(
    "/health/test/runpod",
    tags=["RunPod Serverless"],
    summary="Test RunPod Connection",
    description=(
        "Validates the stored RunPod API key by calling the RunPod REST API "
        "list-endpoints endpoint. Returns a uniform {ok, latency_ms, detail} "
        "result; never raises."
    ),
)
def runpod_test_connection():
    """Test the stored RunPod key; uniform {ok, latency_ms, detail} response."""
    import time as _t

    api_key = (settings.runpod_api_key or "").strip()
    t0 = _t.monotonic()
    if not api_key:
        return {"ok": False, "latency_ms": 0, "detail": "not configured (no RunPod API key)"}
    try:
        req = urllib.request.Request(
            f"{_RUNPOD_API}/endpoints",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        count = len(data) if isinstance(data, list) else 0
        latency_ms = round((_t.monotonic() - t0) * 1000)
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "detail": f"authenticated · {count} endpoint(s) on account",
        }
    except Exception as exc:
        latency_ms = round((_t.monotonic() - t0) * 1000)
        return {"ok": False, "latency_ms": latency_ms, "detail": str(exc)[:300]}


@app.post(
    "/health/test/remote",
    tags=["LLM Providers"],
    summary="Test External OpenAI-Compatible Connection",
    description=(
        "Probes the configured external OpenAI-compatible endpoint (llmRemoteBaseUrl) "
        "with a tiny chat completion. Returns a uniform {ok, latency_ms, detail, model} "
        "result; never raises."
    ),
)
def remote_test_connection():
    """Test the configured external OpenAI-compatible endpoint."""
    return llm_module.test_remote()


@app.get(
    "/sandbox/status",
    tags=["Sandbox"],
    summary="Sandbox Runner Status",
    description=(
        "Probes the configured sandbox runner daemon (sandboxDockerHost, falling "
        "back to DOCKER_HOST or the host socket) and reports whether the runsc "
        "runtime is registered. Includes the effective Docker host and jobs root "
        "so logs can say which backend answered."
    ),
)
def sandbox_status():
    """Report sandbox backend availability for the configured runner."""
    host = settings.sandbox_docker_host or None
    result = sandbox_available(docker_host=host)
    result["dockerHost"] = host or "(default — host daemon socket)"
    result["jobsRoot"] = settings.sandbox_jobs_root or "/jobs"
    return {"success": True, **result}


@app.post(
    "/rebuild",
    tags=["Health & System"],
    summary="Rebuild Code Index",
    description=(
        "Re-parses the JSON report file, rebuilds the in-memory call graph, and "
        "re-indexes all code symbols into the Qdrant vector store. Useful after the "
        "report file has been updated with new AST analysis results."
    ),
    response_model=RebuildResponse,
)
def rebuild():
    service.rebuild(settings.report_path)
    return RebuildResponse(
        symbols=service.symbols_count,
        edges=service.edges_count,
        unresolved_edges=service.unresolved_edges_count,
        collection=settings.qdrant_collection,
    )


@app.get(
    "/search",
    tags=["Code Intelligence"],
    summary="Semantic Search",
    description=(
        "Performs a semantic (vector-similarity) search over indexed code symbols. "
        "Returns the top-k matches ranked by cosine similarity to the query text, "
        "along with a code snippet for each result."
    ),
    response_model=SearchResponse,
)
def search(
    q: Annotated[str, Query(description="Natural-language query to search for in the codebase.")],
    top_k: Annotated[int, Query(ge=1, le=20, description="Number of top results to return (1–20).")] = 5,
):
    return SearchResponse(
        query=q,
        results=service.semantic_search(q, top_k=top_k),
    )


@app.get(
    "/symbols/{class_name}/{method_name}",
    tags=["Code Intelligence"],
    summary="Symbol Context",
    description=(
        "Retrieves full details for a specific code symbol (class method) identified "
        "by its class and method name. Returns the symbol's metadata, parameters, code, "
        "docblock, as well as its incoming (callers) and outgoing (callees) call edges."
    ),
    response_model=SymbolLookupResponse,
    responses={404: {"description": "Symbol not found"}},
)
def symbol_context(class_name: str, method_name: str):
    result = service.get_symbol_context(class_name, method_name)
    if not result:
        raise HTTPException(status_code=404, detail="Symbol not found")
    return result


@app.get(
    "/callers/{class_name}/{method_name}",
    tags=["Code Intelligence"],
    summary="List Callers",
    description=(
        "Returns all methods that directly call the specified class method. "
        "Each caller entry includes the caller's symbol ID, class, name, and file path."
    ),
    response_model=CallersResponse,
    responses={404: {"description": "Symbol not found"}},
)
def callers(class_name: str, method_name: str):
    rows = service.get_callers_of(class_name, method_name)
    return CallersResponse(
        target=f"{class_name}::{method_name}",
        count=len(rows),
        items=rows,
    )


@app.get(
    "/callees/{class_name}/{method_name}",
    tags=["Code Intelligence"],
    summary="List Callees",
    description=(
        "Returns all methods that are called by the specified class method. "
        "Each callee entry indicates whether the call was resolved to a known symbol "
        "and, if so, provides the target's symbol ID, class, name, and file path."
    ),
    response_model=CalleesResponse,
    responses={404: {"description": "Symbol not found"}},
)
def callees(class_name: str, method_name: str):
    rows = service.get_callees_of(class_name, method_name)
    return CalleesResponse(
        source=f"{class_name}::{method_name}",
        count=len(rows),
        items=rows,
    )


@app.post(
    "/download-jobs",
    tags=["Upload & TUS"],
    summary="Create Download Job & Index Code",
    description=(
        "Stores parsed embeddable text received from the PHP service and returns "
        "a signed JWT download token. If code chunks are provided, also indexes them "
        "into a Qdrant collection named after the file for semantic code search."
    ),
    response_model=DownloadJobResponse,
    responses={401: {"description": "Missing or invalid internal secret"}},
)
def create_download_job(
    body: dict,
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
):
    if x_internal_secret != settings.internal_secret:
        dl_logger.warning("Unauthorized /download-jobs attempt")
        raise HTTPException(status_code=401, detail="Unauthorized")

    filename = body.get("filename")
    text = body.get("text")
    chunks_payload = body.get("chunks")

    dl_logger.info("Download job request received: filename=%s text_size=%s has_chunks=%s",
                    filename, len(text) if isinstance(text, str) else 0,
                    bool(chunks_payload))

    if not filename or not isinstance(text, str):
        dl_logger.warning("Invalid payload — missing filename or text")
        raise HTTPException(status_code=400, detail="Missing filename or text")

    download_id = str(uuid.uuid4())
    download_dir = Path(settings.download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    file_path = download_dir / f"{download_id}.txt"

    try:
        file_path.write_text(text, encoding="utf-8")
        dl_logger.debug("Text file stored: path=%s size=%s", file_path, len(text))
    except OSError as e:
        dl_logger.error("Failed to write download file: %s", e)
        raise HTTPException(status_code=500, detail="Failed to store download file")

    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    token = pyjwt.encode({
        "download_id": download_id,
        "filename": filename,
        "exp": expires_at,
    }, settings.download_token_secret, algorithm="HS256")

    response = {
        "download_id": download_id,
        "download_token": token,
        "download_url": f"http://localhost:9002/download/{token}",
        "expires_at": expires_at.isoformat(),
    }

    # Index code chunks through the property-graph pipeline in a background thread
    if chunks_payload and isinstance(chunks_payload, dict):
        chunks_list = chunks_payload.get("chunks", [])
        if chunks_list:
            collection_name = sanitize_collection_name(filename)

            response["collection_name"] = collection_name
            response["chunk_count"] = len(chunks_list)

            # Check if already indexed — skip re-indexing
            already = collection_exists(collection_name)

            if already:
                dl_logger.info("Collection already indexed — skipping: %s", collection_name)
                response["already_indexed"] = True
            else:
                dl_logger.info("Launching background indexing: collection=%s count=%s",
                               collection_name, len(chunks_list))

                thread = threading.Thread(
                    target=index_code_chunks_background,
                    args=(download_id, filename, chunks_list, collection_name),
                    daemon=True,
                )
                thread.start()

    dl_logger.info("Download job created: id=%s filename=%s collection=%s",
                   download_id, filename, response.get("collection_name", "none"))

    return response


@app.get(
    "/download/{token}",
    tags=["Upload & TUS"],
    summary="Download Parsed Result",
    description=(
        "Validates the JWT download token and returns the parsed embeddable text "
        "file as a streaming plain-text response."
    ),
    responses={
        200: {"description": "Streaming text file"},
        401: {"description": "Invalid or expired token"},
        404: {"description": "File not found"},
    },
)
def download_result(token: str):
    try:
        claims = pyjwt.decode(
            token, settings.download_token_secret, algorithms=["HS256"]
        )
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Download token has expired")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid download token")

    download_id = claims.get("download_id")
    filename = claims.get("filename", "result.txt")
    file_path = Path(settings.download_dir) / f"{download_id}.txt"

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found or expired")

    def iterfile():
        with open(file_path, "rb") as f:
            yield from f

    return StreamingResponse(
        iterfile(),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-cache",
        },
    )


@app.get(
    "/download-jobs/{download_id}/status",
    tags=["Upload & TUS"],
    summary="Indexing Progress Status",
    description="Returns the indexing progress for a download job.",
)
def indexing_status(download_id: str):
    status_path = Path(settings.download_dir) / f"{download_id}.status.json"
    if not status_path.is_file():
        return {"status": "unknown", "total": 0, "indexed": 0}
    return json.loads(status_path.read_text())


# ─── Workspace + RAG upload pipeline (replaces php-ast) ────────────────────────

def _extract_workspace(workspace_id: str, zip_data: bytes) -> str:
    """Extract a project ZIP into a fresh workspace dir, returning its path."""
    import shutil

    from php_tools import extract_zip_safely

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


def _parse_and_index(download_id: str, filename: str, zip_data: bytes,
                     collection_name: str) -> None:
    """Extract a ZIP, parse PHP via the CLI, and index chunks in the background."""
    import shutil

    from php_tools import extract_zip_safely, run_code_parse

    workdir = Path(settings.download_dir) / f"parse_{download_id}"
    try:
        write_status(download_id, "embedding", 0, 0, collection_name)
        file_count = extract_zip_safely(zip_data, str(workdir))
        dl_logger.info("Parse extracted %s files: %s", file_count, collection_name)
        chunks = run_code_parse(str(workdir))
        if not chunks:
            dl_logger.warning("No PHP chunks produced: %s", collection_name)
            write_status(download_id, "error", 0, 0, collection_name)
            return
        index_code_chunks_background(download_id, filename, chunks, collection_name)
    except Exception as exc:
        dl_logger.error("Parse failed for '%s': %s", collection_name, exc, exc_info=True)
        write_status(download_id, "error", 0, 0, collection_name)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.post(
    "/api/workspace/prepare",
    tags=["Upload & TUS"],
    summary="Prepare Workspace",
    description=(
        "Downloads the project ZIP from MinIO, extracts it to a local workspace "
        "directory, and returns the workspace id/path used by the PHP CLI tools."
    ),
)
def workspace_prepare(body: dict):
    from storage import storage

    filename = body.get("filename")
    session_id = body.get("upload_session_id")
    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename.")

    if not storage.find_rag_file(filename, session_id):
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")

    data = storage.get_rag_file_bytes(filename, session_id)
    if data is None:
        raise HTTPException(status_code=500, detail="Failed to read file from storage.")

    workspace_id = str(uuid.uuid4())
    try:
        extracted_path = _extract_workspace(workspace_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to extract ZIP archive.")

    file_count = sum(1 for _ in Path(extracted_path).rglob("*") if _.is_file())

    dl_logger.info("Workspace prepared: id=%s files=%s", workspace_id, file_count)
    return {
        "workspace_id": workspace_id,
        "extracted_path": extracted_path,
        "file_count": file_count,
    }


@app.delete(
    "/api/workspace/{workspace_id}",
    tags=["Upload & TUS"],
    summary="Delete Workspace",
    description="Removes an extracted workspace directory.",
)
def workspace_destroy(workspace_id: str):
    from php_tools import safe_rmtree

    workspace_dir = Path(settings.workspace_root) / workspace_id
    if workspace_dir.exists():
        safe_rmtree(str(workspace_dir))
    return {"deleted": True}


@app.get(
    "/api/rag/files",
    tags=["Upload & TUS"],
    summary="List Uploaded Files",
    description="Lists RAG uploads stored in MinIO (rag-uploads prefix).",
)
def rag_files():
    from storage import storage

    return {"files": storage.list_rag_files()}


@app.delete(
    "/api/rag/files",
    tags=["Upload & TUS"],
    summary="Delete Uploaded File",
    description="Deletes a file from MinIO rag-uploads by filename and optional session.",
)
def rag_files_delete(body: dict):
    from storage import storage

    filename = body.get("filename")
    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename.")
    match = storage.find_rag_file(filename, body.get("upload_session_id"))
    if not match:
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")
    storage.delete_rag_file(filename, match["upload_session_id"])
    return {
        "deleted": True,
        "filename": filename,
        "upload_session_id": match["upload_session_id"],
    }


@app.post(
    "/api/rag/upload",
    tags=["Upload & TUS"],
    summary="Upload a ZIP — direct or TUS session",
    description=(
        "Stores a project ZIP in MinIO. If {filename, content (base64), size} is "
        "provided the file is stored directly; if only {filename, size} is given a "
        "resumable TUS upload session is created and a JWT upload token returned."
    ),
)
def rag_upload(body: dict):
    from storage import storage

    filename = body.get("filename")
    content = body.get("content")
    size = int(body.get("size", 0) or 0)

    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename.")

    ext = Path(filename).suffix.lower()
    if ext != ".zip":
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext or '(none)'} — only .zip archives are supported.",
        )

    if size > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File is too large")

    # TUS session mode — no content, just filename + size
    if not content:
        upload_session_id = str(uuid.uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=60)
        token = pyjwt.encode(
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

    # Direct upload mode — base64 content provided
    try:
        decoded = base64.b64decode(content, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 content.")

    upload_session_id = str(uuid.uuid4())
    try:
        storage.put_rag_file(upload_session_id, filename, decoded)
    except Exception as exc:
        dl_logger.error("Upload store failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to store upload.")

    dl_logger.info("File uploaded: filename=%s session=%s size=%s",
                   filename, upload_session_id, len(decoded))
    return {
        "upload_session_id": upload_session_id,
        "filename": filename,
        "type": "zip",
        "text": f"Uploaded ZIP archive: {filename} (binary content, not text-indexed).",
        "size": len(decoded),
    }


@app.post(
    "/api/rag/parse-code",
    tags=["Upload & TUS"],
    summary="Parse PHP Code from ZIP",
    description=(
        "Finds an uploaded ZIP in MinIO, extracts it, parses PHP via the in-image "
        "CLI, and indexes the chunks in the background. Returns a download token "
        "and collection name for progress polling."
    ),
)
def rag_parse_code(body: dict):
    from storage import storage

    filename = body.get("filename")
    session_id = body.get("upload_session_id")
    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename.")

    ext = Path(filename).suffix.lower()
    if ext != ".zip":
        raise HTTPException(status_code=400, detail=f"Only ZIP files can be parsed. Got: {ext or '(none)'}")

    match = storage.find_rag_file(filename, session_id)
    if not match:
        raise HTTPException(status_code=404, detail=f"File not found in uploads: {filename}")

    data = storage.get_rag_file_bytes(filename, session_id)
    if data is None:
        raise HTTPException(status_code=500, detail="Failed to read file from storage.")

    collection_name = sanitize_collection_name(filename)
    already = collection_exists(collection_name)
    download_id = str(uuid.uuid4())
    token = _mint_download_token(download_id, filename)

    response = {
        "download_id": download_id,
        "download_token": token,
        "download_url": f"http://localhost:9002/download/{token}",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
        "collection_name": collection_name,
        "already_indexed": already,
    }

    if already:
        dl_logger.info("Parse skipped — already indexed: %s", collection_name)
        return response

    thread = threading.Thread(
        target=_parse_and_index,
        args=(download_id, filename, data, collection_name),
        daemon=True,
    )
    thread.start()
    return response


@app.post(
    "/api/internal/tusd/hooks",
    tags=["Upload & TUS"],
    summary="TUSD Webhook Handler",
    description=(
        "Receives post-finish hook events from tusd after a resumable ZIP upload "
        "completes. Verifies the upload-session JWT carried in the tus metadata and "
        "moves the object under rag-uploads/{session}/{filename}. No text extraction "
        "is performed — code archives are parsed on demand."
    ),
)
def tusd_hook(body: dict):
    from storage import RAG_UPLOADS_PREFIX, storage

    if body.get("Type") != "post-finish":
        return {"ok": True, "handled": False}

    upload = (body.get("Event") or {}).get("Upload") or {}
    metadata = upload.get("MetaData") or {}

    if metadata.get("upload_kind") != "rag-document":
        return {"ok": True, "handled": False}

    token = metadata.get("upload_token")
    if not token:
        raise HTTPException(status_code=401, detail="Missing upload token")

    try:
        claims = pyjwt.decode(token, settings.upload_token_secret, algorithms=["HS256"])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired upload token")

    if claims.get("upload_kind") != "rag-document":
        raise HTTPException(status_code=401, detail="Token not valid for RAG upload")
    if claims.get("upload_session_id") != metadata.get("upload_session_id"):
        raise HTTPException(status_code=401, detail="Upload session mismatch")

    storage_info = upload.get("Storage") or {}
    object_key = storage_info.get("Key")
    if not object_key:
        raise HTTPException(status_code=400, detail="Missing upload storage information")

    filename = claims.get("filename")
    upload_session_id = claims.get("upload_session_id")
    if not filename or not upload_session_id:
        raise HTTPException(status_code=400, detail="Missing upload metadata")

    dest_key = f"{RAG_UPLOADS_PREFIX}/{upload_session_id}/{filename}"
    try:
        storage.copy_object(object_key, dest_key)
    except Exception as exc:
        dl_logger.error("tusd hook copy failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to store upload")

    dl_logger.info("tusd upload stored: %s", dest_key)
    return {"ok": True, "handled": True}


@app.post(
    "/classify",
    tags=["Code Intelligence"],
    summary="Classify User Prompt",
    description=(
        "Uses Semantic Router with Ollama embeddings to classify a user prompt "
        "into one of six categories: code_explain, code_refactor, debug, search, "
        "knowledge, or chat. Returns the route name and confidence score."
    ),
)
def classify_prompt(body: dict):
    prompt = body.get("prompt", "")
    if not prompt or not isinstance(prompt, str):
        raise HTTPException(status_code=400, detail="Missing or invalid prompt")
    return semantic_classify(prompt)


@app.post(
    "/extract",
    tags=["Code Intelligence"],
    summary="Extract Intent & Entities",
    description=(
        "Extracts structured intent and entities (target file, class, method, "
        "action type, error code, framework, constraints) from a user prompt via "
        "a structured-output LLM. Degrades to a neutral intent on failure."
    ),
)
def extract_prompt(body: dict):
    prompt = body.get("prompt", "")
    if not prompt or not isinstance(prompt, str):
        raise HTTPException(status_code=400, detail="Missing or invalid prompt")
    return extract_intent(prompt).model_dump()


@app.get(
    "/retrieval-insights/{trace_id}",
    tags=["Code Intelligence"],
    summary="Get Retrieval Insights",
)
def retrieval_insights(
    trace_id: str,
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
):
    if x_internal_secret != settings.internal_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")
    from llamaindex_layer import get_retrieval_trace

    trace = get_retrieval_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Retrieval trace not found")
    return trace


def _answer_context(
    body: dict, query: str, x_rag_trace_id: str | None
) -> tuple[list, list, str | None]:
    """Resolve retrieval contexts for an answer request.

    Returns (results, contexts, extra_context). An explicit tool context (e.g.
    project structure from the `knowledge` route) grounds the answer directly;
    otherwise Qdrant is searched via CodePropertyGraph.hybrid_retrieve.
    """
    from llamaindex_layer import CodePropertyGraph
    from llamaindex_layer import record_retrieval_trace

    collection = body.get("collection")
    class_name = body.get("class_name")
    file = body.get("file")
    extra_context = body.get("extra_context")

    if extra_context and isinstance(extra_context, str) and extra_context.strip():
        record_retrieval_trace(
            x_rag_trace_id or "",
            {
                "query_hash": __import__("hashlib").sha1(query.encode("utf-8")).hexdigest()[:12],
                "mode": "explicit_context",
                "collection": collection,
                "searched_collections": [],
                "vector_result_count": 0,
                "graph_neighbor_count": 0,
                "graph_context_count": 0,
                "graph_context_with_code_count": 0,
                "graph_available": False,
                "graph_fallback": False,
                "sources": [],
            },
        )
        return [], [], extra_context

    result = CodePropertyGraph().hybrid_retrieve(
        query=query,
        collection_name=collection,
        limit=10,
        class_name=class_name,
        file=file,
        trace_id=x_rag_trace_id,
    )
    results = result.get("results", []) or []
    contexts = [(r.get("payload") or {}) for r in results]
    return results, contexts, None


@app.post(
    "/answer",
    tags=["LLM"],
    summary="Generate Answer",
    description=(
        "Generate a final answer from retrieved code context, optional "
        "metadata filters (class_name/file) and extracted entities."
    ),
)
def answer_prompt(
    body: dict,
    x_rag_trace_id: Annotated[str | None, Header(alias="X-RAG-Trace-Id")] = None,
):
    query = body.get("query", "")
    if not query or not isinstance(query, str):
        raise HTTPException(status_code=400, detail="Missing or invalid query")

    collection = body.get("collection")
    class_name = body.get("class_name")
    file = body.get("file")
    entities = body.get("entities") or {}
    temperature = body.get("temperature")
    top_p = body.get("top_p")
    extra_context = body.get("extra_context")
    # Default to the remote OpenAI-compatible model when the field is omitted;
    # falls back to the local Ollama model automatically when none is configured.
    use_remote = body.get("use_remote", True) is True
    route = body.get("route", "chat")

    from answer import generate_answer

    results, contexts, extra_context = _answer_context(body, query, x_rag_trace_id)

    gen = generate_answer(
        query,
        contexts=contexts,
        entities=entities,
        temperature=temperature,
        top_p=top_p,
        extra_context=extra_context,
        use_remote=use_remote,
        route=route,
    )

    matches = []
    for i, r in enumerate(results):
        p = r.get("payload") or {}
        name = f"{p.get('class') or ''}::{p.get('name') or ''}".strip(":")
        if not name.strip():
            name = p.get("file") or "symbol"
        matches.append(
            {
                "chunk": {
                    "id": p.get("node_id") or f"c{i}",
                    "fileId": p.get("file") or "",
                    "fileName": name,
                    "text": p.get("code") or "",
                    "index": i,
                },
                "score": r.get("score", 0.0),
            }
        )

    return {
        "success": True,
        "answer": gen["answer"],
        "matches": matches,
        "llmUsed": (
            f"Remote GPU LLM ({gen['model']})"
            if gen.get("provider") == "remote"
            else f"Local LLM ({gen['model']})"
        ),
        "provider": gen.get("provider", "local"),
        "durationMs": gen["duration_ms"],
        "code_validation": gen.get("code_validation", []),
    }


@app.post(
    "/answer/stream",
    tags=["LLM"],
    summary="Streaming Answer (SSE)",
    description=(
        "Generate an answer and stream it as Server-Sent Events: start, delta, "
        "validation and done events. Same retrieval semantics as /answer."
    ),
)
def answer_stream(
    body: dict,
    x_rag_trace_id: Annotated[str | None, Header(alias="X-RAG-Trace-Id")] = None,
):
    query = body.get("query", "")
    if not query or not isinstance(query, str):
        raise HTTPException(status_code=400, detail="Missing or invalid query")

    entities = body.get("entities") or {}
    temperature = body.get("temperature")
    top_p = body.get("top_p")
    use_remote = body.get("use_remote", True) is True
    route = body.get("route", "chat")

    results, contexts, extra_context = _answer_context(body, query, x_rag_trace_id)

    from answer import generate_answer_stream

    def _sse():
        try:
            for event in generate_answer_stream(
                query,
                contexts=contexts,
                entities=entities,
                temperature=temperature,
                top_p=top_p,
                extra_context=extra_context,
                use_remote=use_remote,
                route=route,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            get_exception_logger().error("SSE answer failed: %s", exc)
            yield f"data: {json.dumps({'type': 'error', 'detail': str(exc)})}\n\n"

    return StreamingResponse(
        _sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post(
    "/feedback",
    tags=["Code Intelligence"],
    summary="Submit Routing Feedback",
    description=(
        "Stores a user correction for a query so future identical queries are "
        "routed to the corrected route. The query is embedded and upserted into "
        "the route_feedback Qdrant collection."
    ),
)
def submit_feedback(body: dict):
    query = body.get("query", "")
    correct_route = body.get("route") or body.get("correct_route", "")
    result = router_submit_feedback(query, correct_route)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to submit feedback"))
    return result


_eval_cache: dict = {}


@app.post(
    "/evaluate",
    tags=["Code Intelligence"],
    summary="Evaluate a Response",
    description=(
        "Scores a single (query, answer, retrieved contexts) triple with "
        "reference-free RAGAS metrics: faithfulness, answer_relevancy, and "
        "context_precision. Returns per-metric score (0-1) and a human-readable "
        "tier. On evaluation failure (e.g. judge model timeout) returns success=false."
    ),
)
def evaluate_prompt(body: dict):
    query = body.get("query", "")
    answer = body.get("answer", "")
    contexts = body.get("contexts") or []
    if not query or not answer:
        raise HTTPException(status_code=400, detail="Missing query or answer")

    cache_key = json.dumps([query, answer, contexts], sort_keys=True, default=str)
    if cache_key in _eval_cache:
        return {"success": True, "metrics": _eval_cache[cache_key]}

    from evaluation import evaluate_response

    try:
        metrics = evaluate_response(query, answer, contexts)
    except Exception as exc:
        get_exception_logger().error("Response evaluation failed: %s", exc)
        return {"success": False, "error": "Evaluation unavailable for this response"}

    _eval_cache[cache_key] = metrics
    return {"success": True, "metrics": metrics}


def _coerce_setting(key: str, value):
    default = RAG_CONFIG_DEFAULTS.get(key)
    if isinstance(default, int) and not isinstance(value, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, bool):
        return bool(value)
    if value is None:
        return default
    return value


@app.get(
    "/settings",
    tags=["Code Intelligence"],
    summary="Get UI Settings",
    description=(
        "Returns the full RAG configuration merged over defaults. Secret fields "
        "are masked unless the internal secret is provided via X-Internal-Secret."
    ),
)
def get_settings(
    x_internal_secret: Annotated[str | None, Header(alias="X-Internal-Secret")] = None,
):
    config = build_config(settings_store)
    if x_internal_secret != settings.internal_secret:
        config = mask_config(config)
    return {"success": True, "config": config}


@app.put(
    "/settings",
    tags=["Code Intelligence"],
    summary="Update UI Settings",
    description=(
        "Upserts the provided RAG configuration fields. Secret fields are "
        "write-only and only overwritten when a non-empty value is supplied. "
        "Changes take effect on the next action without a restart."
    ),
)
def update_settings(body: dict):
    incoming = body.get("config", body) if isinstance(body, dict) else {}
    updates = {}
    for key in RAG_CONFIG_DEFAULTS:
        if key not in incoming:
            continue
        value = incoming[key]
        if key in SECRET_KEYS and value in (None, "", MASKED_VALUE):
            continue
        updates[key] = _coerce_setting(key, value)

    settings_store.set_many(updates)
    return {"success": True, "config": mask_config(build_config(settings_store))}


@app.post(
    "/orchestrate",
    tags=["Code Intelligence"],
    summary="Start Orchestration Job",
    description=(
        "Launches an async orchestration job that parses code, runs configured "
        "tools, and collects outputs. Returns a job_id for polling status."
    ),
)
def start_orchestration(body: dict):
    route = body.get("route", "chat")
    filename = body.get("filename", "")
    prompt = body.get("prompt", "")
    skip_index = bool(body.get("skip_index", False))
    entities = body.get("entities") or {}
    if not filename or not prompt:
        raise HTTPException(status_code=400, detail="Missing filename or prompt")

    job_id = str(uuid.uuid4())
    orchestrator.start_job(
        job_id, route, filename, prompt, skip_index=skip_index, entities=entities
    )
    return {"job_id": job_id}


@app.get(
    "/orchestrate/{job_id}/status",
    tags=["Code Intelligence"],
    summary="Orchestration Job Status",
    description="Returns the current status and step progress of an orchestration job.",
)
def orchestration_status(job_id: str):
    job = orchestrator.get_job(job_id)
    if job is None:
        return {"status": "unknown", "step": 0, "total_steps": 0}
    return job


@app.get(
    "/templates/{name}",
    tags=["Code Intelligence"],
    summary="Fetch Prompt Template",
    description="Returns a Jinja2 prompt template by name.",
)
def get_template(name: str):
    content = orchestrator.get_template(name)
    if not content:
        raise HTTPException(status_code=404, detail=f"Template '{name}' not found")
    return {"template": content}


@app.post(
    "/templates/{name}/render",
    tags=["Code Intelligence"],
    summary="Render Prompt Template",
    description="Renders a Jinja2 template with tool outputs and user query.",
)
def render_template(name: str, body: dict):
    content = orchestrator.get_template(name)
    if not content:
        raise HTTPException(status_code=404, detail=f"Template '{name}' not found")

    outputs = body.get("outputs", {})
    user_query = body.get("user_query", "")

    # Flatten tool outputs so {{ phpstan }} works as {{ phpstan.stdout }}
    context = {"user_query": user_query}
    for key, val in outputs.items():
        if isinstance(val, dict):
            context[key] = val.get("stdout", str(val))
        else:
            context[key] = str(val) if val else ""
    # Also add ast_chunks from parse-code output
    if "parsing" in outputs:
        context["ast_chunks"] = str(outputs["parsing"])

    try:
        template = Template(content)
        rendered = template.render(**context)
        return {"rendered": rendered}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Template render failed: {e}")


@app.post(
    "/search-all",
    tags=["Code Intelligence"],
    summary="Search Across All Indexed Collections",
    description=(
        "Embeds the query via Ollama and searches all Qdrant collections that "
        "were created by parse-code indexing, enriching top vector hits with "
        "direct callee/caller neighbours from the Neo4j property graph. Falls "
        "back to vector-only search when the graph is unavailable."
    ),
)
def search_all(body: dict):
    query = body.get("query", "")
    limit = body.get("limit", 10)
    filter_collection = body.get("collection")
    class_name = body.get("class_name")
    file = body.get("file")
    if not query:
        raise HTTPException(status_code=400, detail="Missing query")

    from llamaindex_layer import CodePropertyGraph

    return CodePropertyGraph().hybrid_retrieve(
        query=query,
        collection_name=filter_collection,
        limit=limit,
        class_name=class_name,
        file=file,
    )
