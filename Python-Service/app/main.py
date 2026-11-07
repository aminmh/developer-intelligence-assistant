from contextlib import asynccontextmanager
from typing import Annotated, Any, Optional
from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from index.indexer import CodeIntelligenceIndex
from infra.settings import settings
from rag.router import classify as semantic_classify
from rag.router import submit_feedback as router_submit_feedback
from rag.intent import extract_intent
from rag.answer_service import AnswerService, InvalidAnswerRequest
from pipelines.orchestrator import orchestrator
from infra.exception_logger import get_exception_logger, install_exception_hooks
from index.indexing_service import indexing_service
from index.upload_service import upload_service
from infra.settings_store import (
    RAG_CONFIG_DEFAULTS,
    SECRET_KEYS,
    MASKED_VALUE,
    build_config,
    mask_config,
    settings_store,
)
from infra.tool_store import tool_store
from infra.tool_executor import execute_tool
from index.models import (
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
from datetime import datetime, timezone
import re
import json
import time
import logging
import threading
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
    """Check whether a Qdrant collection exists and contains indexed points."""
    try:
        qdrant = QdrantClient(url=settings.qdrant_url)
        existing = qdrant.get_collections().collections
        if not any(c.name == collection_name for c in existing):
            return False
        count = qdrant.count(collection_name=collection_name, exact=True)
        return int(getattr(count, "count", 0) or 0) > 0
    except Exception:
        return False


def collection_vector_size(collection_name: str) -> int | None:
    """Return a single-vector Qdrant collection's dimension when available."""
    try:
        qdrant = QdrantClient(url=settings.qdrant_url)
        vectors = qdrant.get_collection(collection_name).config.params.vectors
        size = getattr(vectors, "size", None)
        if isinstance(size, int):
            return size
        if isinstance(vectors, dict) and len(vectors) == 1:
            value = next(iter(vectors.values()))
            size = getattr(value, "size", None)
            return size if isinstance(size, int) else None
    except Exception:
        pass
    return None


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


service = CodeIntelligenceIndex()


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    try:
        from telemetry.runtime_storage import initialize_runtime_storage
        initialize_runtime_storage()
    except Exception as exc:
        logging.getLogger(__name__).warning("Runtime Collector target configuration was not applied: %s", exc)

    async def telemetry_jobs():
        import time
        from index.storage import storage
        from telemetry.registry import default_registry
        from telemetry.source import SourceService
        service = SourceService(default_registry(), storage)
        next_maintenance = 0.0
        next_aggregation = 0.0
        next_policy_refresh = 0.0
        next_schema_check = 0.0
        schema_ready = False
        while True:
            try:
                worked = await asyncio.to_thread(service.process_one)
                now = time.monotonic()
                if now >= next_schema_check:
                    # Cached per storage target, so this is a no-op once the active target is ready.
                    # Policy and aggregation query the owned columns, so they wait for it.
                    try:
                        from telemetry.span_schema import ensure_active_span_schema
                        schema_ready = await asyncio.to_thread(ensure_active_span_schema)
                        next_schema_check = now + 30
                    except Exception as exc:
                        schema_ready = False
                        logging.getLogger(__name__).warning("Runtime span schema is not ready: %s", exc)
                        next_schema_check = now + 60
                if now >= next_maintenance:
                    await asyncio.to_thread(service.expire_unmatched)
                    next_maintenance = now + 3600
                if schema_ready and now >= next_policy_refresh:
                    try:
                        from telemetry.policy import refresh_sampling_policy
                        from telemetry.runtime import runtime_sampling_policy
                        policy = runtime_sampling_policy(default_registry())
                        await asyncio.to_thread(refresh_sampling_policy, default_registry())
                        next_policy_refresh = now + max(60, policy["refresh_minutes"] * 60)
                    except Exception as exc:
                        logging.getLogger(__name__).warning("Runtime sampling policy refresh failed: %s", exc)
                        next_policy_refresh = now + 60
                if schema_ready and now >= next_aggregation:
                    try:
                        from telemetry.aggregation import aggregate_runtime
                        result = await asyncio.to_thread(aggregate_runtime)
                        if result.get("processed", 0) > 0:
                            from telemetry.events import publish_runtime_update
                            publish_runtime_update()
                    except Exception as exc:
                        logging.getLogger(__name__).warning("Runtime aggregation cycle failed: %s", exc)
                    next_aggregation = now + 300
                if not worked:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(5)

    telemetry_task = asyncio.create_task(telemetry_jobs())
    try:
        if Path(settings.report_path).is_file():
            service.rebuild(settings.report_path)
    except Exception:
        pass  # First startup or empty report — skip rebuild

    # Warm the language servers so the first answer isn't slow (they index /
    # build caches on first use). Best-effort + non-blocking.
    if settings.lsp_validation_enabled and settings.lsp_warmup_enabled:
        try:
            from lsp import warmup

            warmup()
        except Exception:
            pass

    yield

    telemetry_task.cancel()
    try:
        await telemetry_task
    except asyncio.CancelledError:
        pass

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

from telemetry.http import (
    router as telemetry_router,
    host_router as telemetry_host_router,
    runtime_router as telemetry_runtime_router,
    git_router as telemetry_git_router,
)
app.include_router(telemetry_router)
app.include_router(telemetry_host_router)
app.include_router(telemetry_runtime_router)
app.include_router(telemetry_git_router)

from inspection.http import router as inspection_router  # noqa: E402 — after app exists
from routers.developer_logs import router as developer_logs_router  # noqa: E402 — after app exists
from routers.system import router as system_router  # noqa: E402 — after app exists
from routers.evaluator import router as evaluator_router  # noqa: E402 — after app exists
from routers.embedding_jobs import router as embedding_jobs_router  # noqa: E402 — after app exists
from code_quality.http import router as code_quality_router  # noqa: E402 — after app exists
from analysis.schema_draft_routes import router as schema_draft_router  # noqa: E402 — after app exists

app.include_router(inspection_router)
app.include_router(developer_logs_router)
app.include_router(system_router)
app.include_router(evaluator_router)
app.include_router(embedding_jobs_router)
app.include_router(code_quality_router)
app.include_router(schema_draft_router)

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


@app.get(
    "/docretrieval/deps",
    tags=["Documentation Retrieval"],
    summary="Scan workspace dependencies",
    description=(
        "Scans a workspace path for dependency files (composer.lock, "
        "package-lock.json, uv.lock, ...) and returns the normalized, "
        "version-aware dependency map used for documentation lookups."
    ),
)
def docretrieval_deps(workspace: str = ""):
    """Scan a workspace for its installed dependencies."""
    from projects.dependencies import scan_dependencies

    path = workspace or settings.workspace_root
    result = scan_dependencies(path)
    result["workspace"] = path
    return {"success": True, **result}


@app.post(
    "/docretrieval/test",
    tags=["Documentation Retrieval"],
    summary="Test documentation retrieval",
    description=(
        "Resolves documentation for a sample package via the configured "
        "provider pipeline (Context7 → GitMCP) and returns the retrieved "
        "chunk count + provenance. Safe to call — no prompts are generated."
    ),
)
def docretrieval_test(body: dict):
    """Probe the doc-retrieval pipeline for a package/query."""
    from rag.doc_retrieval import retrieve_documentation

    package = (body.get("package") or "laravel/framework").strip()
    query = (body.get("query") or "routing basics").strip()
    packages = {
        package: {
            "requested_version": body.get("requested_version"),
            "resolved_version": body.get("resolved_version") or None,
            "source": "test",
            "installed": True,
        }
    }
    strict = body.get("strict") if isinstance(body.get("strict"), bool) else None
    result = retrieve_documentation(packages, query, strict=strict)
    return {"success": result.get("ok"), **result}


@app.get(
    "/docretrieval/status",
    tags=["Documentation Retrieval"],
    summary="Doc-retrieval configuration status",
)
def docretrieval_status():
    """Report the effective doc-retrieval settings (no secrets)."""
    return {
        "success": True,
        "enabled": settings.doc_retrieval_enabled,
        "context7": {
            "enabled": settings.doc_context7_enabled,
            "use_public_endpoint_without_key": True,
            "timeout_seconds": settings.doc_context7_timeout,
            "max_retries": settings.doc_context7_max_retries,
        },
        "gitmcp": {
            "enabled": settings.doc_gitmcp_enabled,
            "timeout_seconds": settings.doc_gitmcp_timeout,
        },
        "strict_mode": settings.doc_strict_mode,
    }


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
                    target=indexing_service.index_chunks_background,
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
    from index.storage import storage

    filename = body.get("filename")
    session_id = body.get("upload_session_id")
    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename.")

    if not storage.find_rag_file(filename, session_id):
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")

    data = storage.get_rag_file_bytes(filename, session_id)
    if data is None:
        raise HTTPException(status_code=500, detail="Failed to read file from index.storage.")

    workspace_id = str(uuid.uuid4())
    try:
        extracted_path = upload_service.extract_workspace(workspace_id, data)
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
    upload_service.destroy_workspace(workspace_id)
    try:
        from projects.workspace_registry import forget

        forget(workspace_id)
    except Exception:  # noqa: BLE001 — registry is best-effort
        pass
    return {"deleted": True}


@app.get(
    "/api/workspaces",
    tags=["Upload & TUS"],
    summary="List Retained Workspaces",
    description=(
        "Retained (bounded-LRU) extracted workspaces for analysis — "
        "used by the inspect file reads and manual cleanup. Newest first."
    ),
)
def workspaces_list():
    from projects.workspace_registry import list_workspaces

    entries = list_workspaces()
    out = []
    for e in entries:
        path = e.get("path") or ""
        try:
            file_count = sum(1 for _ in Path(path).rglob("*") if _.is_file())
        except OSError:
            file_count = 0
        out.append(
            {
                "id": e.get("id"),
                "filename": e.get("filename"),
                "path": path,
                "files": file_count,
                "age_s": max(0, int(time.time() - float(e.get("ts") or 0))),
            }
        )
    return {"workspaces": out}


@app.get(
    "/api/rag/files",
    tags=["Upload & TUS"],
    summary="List Uploaded Files",
    description="Lists RAG uploads stored in MinIO (rag-uploads prefix).",
)
def rag_files():
    from index.storage import storage

    return {"files": storage.list_rag_files()}


@app.delete(
    "/api/rag/files",
    tags=["Upload & TUS"],
    summary="Delete Uploaded File",
    description="Deletes a file from MinIO rag-uploads by filename and optional session.",
)
def rag_files_delete(body: dict):
    from index.storage import storage

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
    from index.storage import storage

    filename = body.get("filename")
    content = body.get("content")
    size = int(body.get("size", 0) or 0)
    project_id = body.get("project_id")
    commit_sha = body.get("commit_sha")

    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename.")

    try:
        project_id, commit_sha = upload_service.validate_identity(project_id, commit_sha)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        return upload_service.create_tus_session(filename, size, project_id, commit_sha)

    # Direct upload mode — base64 content provided
    try:
        result = upload_service.store_direct(storage, filename, content, project_id, commit_sha)
    except Exception as exc:
        dl_logger.error("Upload store failed: %s", exc, exc_info=True)
        if isinstance(exc, ValueError):
            if "different archive" in str(exc):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise HTTPException(status_code=500, detail="Failed to store upload.")

    dl_logger.info("File uploaded: filename=%s session=%s size=%s",
                   filename, result["upload_session_id"], result["size"])
    return result


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
    from index.storage import storage

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

    identity = None
    if match.get("project_id"):
        if (body.get("project_id") != match["project_id"] or body.get("commit_sha") != match.get("commit_sha")
                or (body.get("revision_id") and body["revision_id"] != match.get("revision_id"))
                or (body.get("source_ref") and body["source_ref"] != match.get("source_ref"))):
            raise HTTPException(status_code=403, detail="Upload project identity does not match the archive record.")
        if not match.get("archive_sha256"):
            raise HTTPException(status_code=409, detail="Upload archive checksum is unavailable.")
        if not match.get("revision_id"):
            raise HTTPException(status_code=409, detail="Upload revision identity is unavailable. Upload the project again.")
        identity = {
            "project_id": match["project_id"],
            "commit_sha": match.get("commit_sha"),
            "source_ref": match.get("source_ref") or match.get("commit_sha"),
            "source_archive_sha256": match["archive_sha256"],
            "revision_id": match.get("revision_id"),
        }
    elif body.get("project_id") or body.get("commit_sha"):
        raise HTTPException(status_code=409, detail="Legacy uploads cannot be assigned an inferred project identity.")

    data = storage.get_rag_file_bytes(filename, session_id)
    if data is None:
        raise HTTPException(status_code=500, detail="Failed to read file from index.storage.")

    vector_collection_name = None
    force_reindex = False
    if identity:
        from telemetry.snapshot import Snapshot
        collection_name = Snapshot(
            identity["project_id"], identity["commit_sha"], identity["source_archive_sha256"]
        ).collection
    else:
        collection_name = sanitize_collection_name(filename)
    already = collection_exists(collection_name)
    if identity:
        from telemetry.registry import default_registry
        ready = default_registry().is_ready_collection(collection_name)
        if settings.embedding_provider == "cohere":
            from index.embedding_rebuild import active_generation
            active = active_generation(collection_name)
            already = bool(ready and active and collection_exists(active))
            if not already:
                vector_collection_name = f"{collection_name}_cohere_{uuid.uuid4().hex}"
        else:
            already = already and ready
            if ready and not already:
                # A previously published Cohere generation may leave the base
                # collection absent. The provider switch must rebuild vectors
                # instead of letting the ready registry short-circuit work.
                force_reindex = True
            if already:
                from index.embeddings import embedding_profile
                expected_size = embedding_profile("search_document").get("dimensions")
                actual_size = collection_vector_size(collection_name)
                if expected_size and actual_size and actual_size != expected_size:
                    dl_logger.info(
                        "Reindexing %s after embedding provider dimension change: %s -> %s",
                        collection_name,
                        actual_size,
                        expected_size,
                    )
                    already = False
                    force_reindex = True
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
        target=indexing_service.parse_and_index,
        args=(download_id, filename, data, collection_name, identity, vector_collection_name, force_reindex),
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
    from index.storage import RAG_UPLOADS_PREFIX, storage

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
        import hashlib
        archive_bytes = storage.get_rag_file_bytes(filename, upload_session_id)
        if archive_bytes is None:
            raise RuntimeError("Uploaded archive could not be read after transfer")
        project_id = claims.get("project_id")
        commit_sha = claims.get("commit_sha")
        project_id, commit_sha = upload_service.validate_identity(project_id, commit_sha)
        storage.set_rag_identity(
            dest_key, project_id, commit_sha, hashlib.sha256(archive_bytes).hexdigest(), data=archive_bytes
        )
    except Exception as exc:
        dl_logger.error("tusd hook copy failed: %s", exc, exc_info=True)
        if isinstance(exc, ValueError) and "different archive" in str(exc):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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
    from index.llamaindex_layer import get_retrieval_trace

    trace = get_retrieval_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Retrieval trace not found")
    return trace


def _index_plan_for(body: dict, query: str, route: str) -> dict:
    """Resolve the index-aware grounding plan for an answer request.

    Lazy import of ``rag.context_plan.build_index_plan`` stays behind this tiny
    total helper: anything unexpected degrades to an off/empty plan dict, never
    a raise into ``_answer_context``.
    """
    try:
        from rag.context_plan import build_index_plan

        plan = build_index_plan(
            route=route,
            filename=body.get("filename"),
            collection=body.get("collection"),
        )
        if not isinstance(plan, dict):
            raise TypeError(f"build_index_plan returned {type(plan).__name__}")
        return plan
    except Exception:  # noqa: BLE001 — planning must never raise
        return {
            "mode": "off",
            "route": route,
            "stem": None,
            "collections": [],
            "saved_kinds": [],
            "has_sym": False,
            "notes": ["plan error"],
        }


def _merge_plan_results(outputs: list[dict]) -> tuple[list, list[str]]:
    """Merge per-collection hybrid retrievals, dedup by identity keeping score.

    The same code symbol can surface from two plan collections (a chunk and its
    knowledge sibling); a payload is deduped on its stable identity — the Qdrant
    ``node_id`` for vector/rehydrated rows and the ``graph_key`` attached by
    ``llamaindex_layer`` to graph-evidence rows (sym evidence and metadata-only
    rehydration fallbacks carry no node id) — so identical evidence returned by
    two per-collection calls collapses instead of surviving as duplicates. A
    row wins only when a later hit scores higher.
    Returns (results sorted by score desc, union of searched collections).
    """
    best: dict[object, dict] = {}
    searched: set[str] = set()
    for out in outputs:
        searched.update(out.get("searched_collections") or [])
        for r in out.get("results") or []:
            payload = r.get("payload") or {}
            key = (
                payload.get("node_id")
                or payload.get("graph_key")
                or ("unkeyed", id(r))
            )
            current = best.get(key)
            if current is None or (r.get("score") or 0.0) > (current.get("score") or 0.0):
                best[key] = r
    merged = sorted(best.values(), key=lambda r: r.get("score") or 0.0, reverse=True)
    return merged, sorted(searched)


def _answer_context(
    body: dict, query: str, x_rag_trace_id: str | None
) -> tuple[list, list, str | None]:
    """Resolve retrieval contexts for an answer request.

    Returns (results, contexts, extra_context). An explicit tool context (e.g.
    project structure from the `knowledge` route) with NO project context
    grounds the answer directly (legacy byte-identical short circuit). When a
    project context (filename and/or collection) is present and the route is
    not chat, the persisted-index plan decides which Qdrant collections are
    searched — structure/tool text still flows through ``extra_context`` so
    both retrieval and structure evidence reach the model. Chat never grounds
    from the PLANNED project indexes.

    Pinned-collection nuance (legacy client safety): a pinned ``collection`` on
    a chat or unknown route is retrieved exactly like the pre-plan /answer path
    (one ``hybrid_retrieve`` call, default graph legs, no plan) — a route label
    that is missing, defaults to ``chat``, or names a route the planner does
    not know can never silently zero-out a pinned retrieval. Chat only blocks
    NEW index use (the no-pin project path); an unknown route with a filename
    (no pinned collection) still runs plan discovery because the filename is
    the caller's project-grounding decision.
    """
    from index.llamaindex_layer import CodePropertyGraph
    from index.llamaindex_layer import record_retrieval_trace
    from rag.context_plan import PROJECT_ROUTES

    collection = body.get("collection")
    class_name = body.get("class_name")
    file = body.get("file")
    extra_context = body.get("extra_context")
    filename = body.get("filename")
    raw_route = body.get("route")
    route = (
        raw_route.strip()
        if isinstance(raw_route, str) and raw_route.strip()
        else "chat"
    )

    # Legacy type guard: only STRING extra_context is honoured (short-circuit /
    # evidence). A non-string truthy value (list/dict/…) was ignored before
    # Phase 3 and stays ignored — never forwarded to the model.
    if extra_context is not None and not isinstance(extra_context, str):
        extra_context = None

    has_extra_context = isinstance(extra_context, str) and bool(extra_context.strip())
    has_project_context = bool(filename or collection)
    pinned = isinstance(collection, str) and bool(collection.strip())

    def _trace(payload: dict) -> None:
        record_retrieval_trace(x_rag_trace_id or "", payload)

    def _query_hash() -> str:
        return __import__("hashlib").sha1(
            query.encode("utf-8")
        ).hexdigest()[:12]

    def _sources_for(results: list) -> list[dict]:
        return [
            {
                "file": (r.get("payload") or {}).get("file", ""),
                "class_name": (
                    (r.get("payload") or {}).get("class")
                    or (r.get("payload") or {}).get("class_fqn", "")
                ),
                "name": (r.get("payload") or {}).get("name", ""),
                "via_graph": bool(r.get("via_graph")),
                "score": round(float(r.get("score", 0.0)), 4),
            }
            for r in results
        ]

    def _empty_trace(mode: str, searched: list) -> dict:
        return {
            "query_hash": _query_hash(),
            "mode": mode,
            "collection": collection,
            "searched_collections": list(searched),
            "vector_result_count": 0,
            "graph_neighbor_count": 0,
            "graph_context_count": 0,
            "graph_context_with_code_count": 0,
            "graph_available": False,
            "graph_fallback": False,
            "sources": [],
        }

    # Legacy explicit-context grounding: tool/structure text with NO project
    # context short-circuits exactly as it did before Phase 3.
    if has_extra_context and not has_project_context:
        _trace(_empty_trace("explicit_context", []))
        return [], [], extra_context

    is_project_route = route in PROJECT_ROUTES

    # Chat + filename only (no pinned collection): chat never grounds from the
    # planned project indexes — structure text still flows through.
    if route == "chat" and not pinned and has_project_context:
        _trace(_empty_trace("chat_no_index", []))
        return [], [], extra_context

    # Legacy pinned retrieval: a pinned collection on a chat or unknown
    # (non-project) route is retrieved exactly like the pre-plan /answer path —
    # ONE call, default graph legs, no plan. Keeps old /answer clients that pin
    # a collection (even with a default/unknown route label) from getting a
    # silent empty result.
    if pinned and not is_project_route:
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
        return results, contexts, extra_context

    # No project context at all — legacy retrieval semantics unchanged.
    if not has_project_context:
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
        return results, contexts, extra_context

    # Project context + non-chat route: ground from the persisted indexes the
    # plan resolves (Qdrant collections; sym graph leg when it exists). Each
    # plan collection is searched ONCE: ``join_sibling=False`` disables
    # hybrid_retrieve's internal knowledge-sibling join because the planner
    # already enumerated the sibling as its own collection — otherwise a
    # two-collection plan would scan the knowledge collection twice.
    plan = _index_plan_for(body, query, route)
    plan_collections = plan.get("collections") or []
    if not plan_collections:
        # Honest no-persisted-indexes case: nothing to search. extra_context is
        # still honoured so structure text never disappears.
        _trace(_empty_trace("no_index", []))
        return [], [], extra_context

    graph_legs = ["calls", "has_knowledge"]
    if plan.get("has_sym") and plan.get("stem"):
        graph_legs.append("sym")

    outputs = []
    for col in plan_collections:
        outputs.append(
            CodePropertyGraph().hybrid_retrieve(
                query=query,
                collection_name=col,
                limit=10,
                class_name=class_name,
                file=file,
                trace_id=x_rag_trace_id,
                graph_legs=list(graph_legs),
                join_sibling=False,
                apply_rerank=False,
            )
        )
    results, searched = _merge_plan_results(outputs)
    rerank_summary = None
    if settings.reranker_enabled and results:
        from rag.reranker import rerank

        results = results[:min(len(results), int(settings.reranker_max_candidates))]
        rerank_summary = rerank(query, results)
        if rerank_summary.get("reranked_count"):
            results.sort(
                key=lambda r: (
                    float(r.get("rerank_score", float("-inf"))),
                    float(r.get("score") or 0.0),
                ),
                reverse=True,
            )
    vector_count = sum(1 for r in results if not r.get("via_graph"))
    graph_count = len(results) - vector_count
    with_code = sum(
        1 for r in results if r.get("via_graph") and (r.get("payload") or {}).get("code")
    )
    _trace(
        {
            "query_hash": _query_hash(),
            "mode": "project",
            "route": route,
            "collection": collection,
            "stem": plan.get("stem"),
            "has_sym": bool(plan.get("has_sym")),
            "plan_notes": plan.get("notes") or [],
            "searched_collections": searched,
            "vector_result_count": vector_count,
            "graph_neighbor_count": graph_count,
            "graph_context_count": graph_count,
            "graph_context_with_code_count": with_code,
            "graph_available": any(bool(r.get("via_graph")) for r in results),
            "graph_fallback": False,
            "reranker": rerank_summary or {
                "enabled": bool(settings.reranker_enabled),
                "model": settings.reranker_model,
                "candidate_count": len(results),
                "reranked_count": 0,
                "latency_ms": 0,
                "fallback_reason": None,
            },
            "sources": _sources_for(results),
        }
    )
    contexts = [(r.get("payload") or {}) for r in results]
    return results, contexts, extra_context


_PROJECT_QUESTION_HINTS = re.compile(
    r"api|endpoint|route|router|payment|checkout|stripe|mollie|table|column|"
    r"schema|migration|model|eloquent|controller|middleware|auth|login|register|"
    r"config|mail|queue|provider|binding|service container|db|database|"
    r"which .* (does|handles|store|has)|how .* (works|is .* wired|stored)",
    re.IGNORECASE,
)


def _resolve_project_profile(body: dict, query: str) -> dict | None:
    """Auto-inject framework knowledge for project-aware questions.

    Priority: explicit ``project_profile`` in the request body, else the
    per-project knowledge cache keyed by the Qdrant collection. Only attached
    when the query looks project-knowledge oriented (auto-detect intent — no
    UI mode needed) and the collection/profile belongs to an analyzed upload.
    """
    explicit = body.get("project_profile")
    if isinstance(explicit, dict) and explicit.get("framework"):
        return explicit
    query_text = str(query or "")
    if not _PROJECT_QUESTION_HINTS.search(query_text):
        return None
    try:
        from projects.knowledge_cache import get_for_collection

        collection = body.get("collection") or body.get("collection_name")
        return get_for_collection(str(collection)) if collection else None
    except Exception:  # noqa: BLE001 — best-effort enrichment
        return None


def _maybe_inspect_context(results: list, body: dict) -> str | None:
    """Agent-style symbol-graph grounding for /answer requests.

    Opt-in via ``auto_inspect: true`` + ``project_dir``/``stem`` in the body.
    Bounded, best-effort; any failure degrades to None (no inspection block).
    """
    if not body.get("auto_inspect"):
        return None
    project_dir = body.get("project_dir")
    stem = body.get("stem")
    if not isinstance(project_dir, str) or not project_dir or not isinstance(stem, str):
        return None
    if not results:
        return None
    try:
        from inspection.auto import build_inspection_grounding

        return build_inspection_grounding(results, project_dir, stem)
    except Exception:  # noqa: BLE001 — inspection is optional enrichment
        get_exception_logger().warning("Auto-inspection grounding skipped", exc_info=True)
        return None


def _domain_project_id(body: dict) -> str | None:
    """Concept-store project id anchored to an answer request body.

    Mirrors ``_resolve_project_profile``/``knowledge_cache`` anchoring: prefer
    the upload ``filename`` (project_stem of the zip), else strip the known
    Qdrant prefixes off a pinned ``collection`` name. The derived id matches
    what ``analysis.analysis_service.index_domain_for_workspace`` writes.
    """
    filename = body.get("filename")
    if isinstance(filename, str) and filename.strip():
        try:
            from analysis.analysis_service import project_stem

            return project_stem(filename)
        except Exception:  # noqa: BLE001 — enrichment is best-effort
            return None
    collection = body.get("collection") or body.get("collection_name")
    if not isinstance(collection, str) or not collection.strip():
        return None
    value = collection.strip()
    for prefix in ("chunk_code_", "proj_knowledge_", "sym_"):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
            break
    if not value:
        return None
    try:
        from analysis.analysis_service import project_stem

        return project_stem(value)
    except Exception:  # noqa: BLE001 — enrichment is best-effort
        return None


def _render_domain_block(result: Any) -> str | None:
    """Markdown grounding block for a RecognitionResult (or None).

    Renders only recognized concepts (+ verified relationship facts when the
    recognizer expanded any); ambiguous/rejected evidence never appears and the
    raw query is never echoed. ``result`` is duck-typed so a recognizer-shaped
    fake is renderable without importing the domain layer.
    """
    recognized = list(getattr(result, "recognized", None) or [])
    if not recognized:
        return None
    lines = ["# Recognized domain concepts", ""]
    for row in recognized[:10]:
        if not isinstance(row, dict):
            continue
        canonical = str(row.get("canonical_name") or row.get("identity_key") or "?")
        concept_type = str(row.get("concept_type") or "concept")
        label = str(row.get("qualified_name") or row.get("normalized_name") or canonical)
        lines.append(f"- **{canonical}** ({concept_type}) — {label}")

    relationships = list(getattr(result, "relationships", None) or [])
    if relationships:
        lines.extend(["", "# Verified related facts", ""])
        for edge in relationships:
            if not isinstance(edge, dict):
                continue
            source = edge.get("source") or {}
            target = edge.get("target") or {}
            source_name = (
                str(source.get("canonical_name") or source.get("identity_key") or "?")
                if isinstance(source, dict)
                else "?"
            )
            target_name = (
                str(target.get("canonical_name") or target.get("identity_key") or "?")
                if isinstance(target, dict)
                else "?"
            )
            lines.append(
                f"- {source_name} {edge.get('relation_type')} {target_name}"
            )
    return "\n".join(lines)


def _domain_store_has_concepts(project_id: str) -> bool:
    """Cheap guard: has the project been domain-indexed at all?

    Avoids a pointless (and potentially network-backed) recognition attempt on
    every project /answer when the concept store has no rows yet. Best-effort:
    a store failure returns True so the guarded recognizer decides.
    """
    try:
        from projects.domain.store import concept_store

        counts = concept_store.counts(project_id) or {}
        return int(counts.get("concepts") or 0) > 0
    except Exception:  # noqa: BLE001 — guard degrades to the recognizer attempt
        return True


# Read-only domain browsing is intentionally kept behind small composition
# helpers. The SQLite concept store remains the semantic owner; HTTP only adds
# pagination, redaction, and bounded response shaping.
_DOMAIN_CONCEPT_LIMIT = 200
_DOMAIN_CONTEXT_NODE_CAP = 50
_DOMAIN_CONTEXT_EDGE_CAP = 50
_DOMAIN_EVIDENCE_CAP = 100
_DOMAIN_TEXT_CAP = 500


def _analysis_domain_source(analysis_id: str) -> tuple[dict[str, Any], str, str]:
    from analysis.analysis_service import project_stem
    from analysis.analysis_store import analysis_store

    row = analysis_store.get(analysis_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    filename = str(row.get("filename") or "")
    project_id = project_stem(filename)
    if not project_id:
        raise HTTPException(status_code=422, detail="Analysis has no project filename")
    profile = row.get("profile") if isinstance(row.get("profile"), dict) else {}
    framework = profile.get("framework") if isinstance(profile, dict) else None
    if isinstance(framework, dict):
        framework = framework.get("name") or framework.get("id")
    source = {
        "analysis_id": analysis_id,
        "filename": filename,
        "project_id": project_id,
        "framework": str(framework or "unknown"),
        "provenance": ["saved_analysis", "domain_concept_store"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return source, project_id, filename


def _public_domain_concept(row: dict[str, Any], store: Any) -> dict[str, Any]:
    """Return a bounded, non-code representation of one concept row."""
    concept_id = str(row.get("id") or "")
    aliases: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    try:
        if concept_id and hasattr(store, "aliases_for_concept"):
            aliases = list(store.aliases_for_concept(concept_id, limit=50) or [])
    except Exception:
        aliases = []
    try:
        project_id = str(row.get("project_id") or "")
        identity_key = str(row.get("identity_key") or "")
        if project_id and identity_key and hasattr(store, "find_sources_for_concept"):
            sources = list(store.find_sources_for_concept(project_id, identity_key) or [])
    except Exception:
        sources = []
    clean_sources = [
        {
            "file": str(item.get("source_file") or item.get("file") or "")[:300],
            "start_line": item.get("start_line"),
            "end_line": item.get("end_line"),
            "method": str(item.get("extraction_method") or item.get("method") or "")[:80],
            "confidence": item.get("confidence"),
        }
        for item in sources[:50]
        if isinstance(item, dict)
    ]
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    safe_metadata = {
        str(key)[:80]: value
        for key, value in list(metadata.items())[:20]
        if str(key).casefold() not in {"code", "raw", "source_hash", "excerpt"}
        and isinstance(value, (str, int, float, bool, type(None), list, dict))
    }
    return {
        "id": concept_id,
        "identity_key": str(row.get("identity_key") or ""),
        "canonical_name": str(row.get("canonical_name") or "")[:200],
        "qualified_name": str(row.get("qualified_name") or "")[:300],
        "concept_type": str(row.get("concept_type") or "")[:80],
        "roles": [str(role)[:80] for role in (row.get("roles") or [])[:20]],
        "normalized_name": str(row.get("normalized_name") or "")[:200],
        "description": str(row.get("description") or "")[:_DOMAIN_TEXT_CAP] or None,
        "aliases": [
            {
                "alias": str(item.get("alias") or "")[:200],
                "alias_type": str(item.get("alias_type") or "")[:80],
                "confidence": item.get("confidence"),
            }
            for item in aliases[:50]
            if isinstance(item, dict)
        ],
        "sources": clean_sources,
        "metadata": safe_metadata,
        "relationship_count": 0,
    }


def _domain_relationship_public(edge: dict[str, Any]) -> dict[str, Any]:
    source_identity = str(edge.get("source_identity") or "")
    target_identity = str(edge.get("target_identity") or "")
    relation_id = str(edge.get("id") or "")
    return {
        "id": relation_id or None,
        "relation_type": str(edge.get("relation_type") or "")[:100],
        "source_identity": source_identity,
        "target_identity": target_identity,
        "source": {
            "id": str(edge.get("source_concept_id") or ""),
            "identity_key": source_identity,
            "canonical_name": str(edge.get("source_canonical_name") or ""),
        },
        "target": {
            "id": str(edge.get("target_concept_id") or ""),
            "identity_key": target_identity,
            "canonical_name": str(edge.get("target_canonical_name") or ""),
        },
        "confidence": edge.get("confidence"),
    }


def _domain_evidence(concepts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for concept in concepts:
        score = concept.get("final_score")
        try:
            score = round(float(score), 6) if score is not None else None
        except (TypeError, ValueError):
            score = None
        sources = concept.get("sources") or []
        if not sources:
            evidence.append(
                {
                    "kind": "concept",
                    "name": concept.get("canonical_name") or concept.get("identity_key"),
                    "file": None,
                    "start_line": None,
                    "end_line": None,
                    "score": score,
                    "excerpt": (concept.get("description") or "")[:_DOMAIN_TEXT_CAP] or None,
                }
            )
            continue
        for source in sources[:20]:
            evidence.append(
                {
                    "kind": "source",
                    "name": concept.get("canonical_name") or concept.get("identity_key"),
                    "file": source.get("file"),
                    "start_line": source.get("start_line"),
                    "end_line": source.get("end_line"),
                    "score": score,
                    "excerpt": (concept.get("description") or "")[:_DOMAIN_TEXT_CAP] or None,
                }
            )
            if len(evidence) >= _DOMAIN_EVIDENCE_CAP:
                return evidence
    return evidence


def _expanded_domain_payload(
    project_id: str,
    *,
    query: str | None = None,
    concept_id: str | None = None,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build expanded domain context without exceeding fixed graph/text caps."""
    from projects.domain.recognizer import recognize
    from projects.domain.store import concept_store

    warnings: list[str] = []
    recognized: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    truncated = False
    degraded = False
    try:
        if concept_id:
            concept = concept_store.find_by_id(project_id, concept_id)
            if concept is None:
                concept = concept_store.find_by_identity(project_id, concept_id)
            if concept is None:
                return {
                    "success": True,
                    "source": source,
                    "recognized": [],
                    "ambiguous": [],
                    "rejected": [],
                    "relationships": [],
                    "evidence": [],
                    "warnings": ["concept not found"],
                    "truncated": False,
                    "degraded": False,
                }
            recognized = [dict(concept)]
        elif isinstance(query, str) and query.strip():
            result = recognize(query, project_id, expand_relationships=False)
            recognized = list(result.recognized or [])
            ambiguous = list(result.ambiguous or [])
            rejected = list(result.rejected or [])
            degraded = bool(result.degraded)
            if result.error:
                warnings.append(str(result.error)[:_DOMAIN_TEXT_CAP])
            if result.query_note:
                warnings.append(str(result.query_note)[:_DOMAIN_TEXT_CAP])
        else:
            warnings.append("query or concept_id is required")
    except Exception:  # noqa: BLE001 — read-only context is best effort
        degraded = True
        warnings.append("domain recognition unavailable")

    expanded_rows: list[dict[str, Any]] = []
    for row in recognized[:5]:
        public = _public_domain_concept(row, concept_store)
        try:
            if hasattr(concept_store, "related_nodes") and row.get("id"):
                graph = concept_store.related_nodes(
                    project_id,
                    str(row["id"]),
                    depth=1,
                    node_cap=_DOMAIN_CONTEXT_NODE_CAP,
                    edge_cap=_DOMAIN_CONTEXT_EDGE_CAP,
                )
                if graph.get("truncated"):
                    truncated = True
                for node in graph.get("nodes") or []:
                    expanded_rows.append(_public_domain_concept(node, concept_store))
                for edge in graph.get("edges") or []:
                    relationships.append(_domain_relationship_public(edge))
        except Exception:  # noqa: BLE001 — graph expansion is optional
            degraded = True
            warnings.append("relationship expansion unavailable")
        expanded_rows.append(public)

    by_id = {str(row.get("id")): row for row in expanded_rows if row.get("id")}
    for row in by_id.values():
        row["relationship_count"] = sum(
            1
            for edge in relationships
            if edge["source"].get("id") == row.get("id")
            or edge["target"].get("id") == row.get("id")
        )
    if truncated:
        warnings.append("context expansion was bounded")
    return {
        "success": True,
        "source": source,
        "recognized": [_public_domain_concept(row, concept_store) for row in recognized[:5]],
        "ambiguous": [_public_domain_concept(row, concept_store) for row in ambiguous[:5]],
        "rejected": rejected[:5],
        "relationships": relationships[:_DOMAIN_CONTEXT_EDGE_CAP],
        "evidence": _domain_evidence(
            [_public_domain_concept(row, concept_store) for row in recognized[:5]]
            + list(by_id.values())[: _DOMAIN_CONTEXT_NODE_CAP]
        ),
        "warnings": warnings[:20],
        "truncated": truncated,
        "degraded": degraded,
    }


def _render_expanded_domain_block(payload: dict[str, Any]) -> str | None:
    recognized = payload.get("recognized") or []
    relationships = payload.get("relationships") or []
    analysis_context = payload.get("analysis_context")
    if not recognized and not relationships and not (isinstance(analysis_context, str) and analysis_context.strip()):
        return None
    lines = ["# Expanded domain context", ""]
    for row in recognized[:5]:
        lines.append(
            f"- {row.get('canonical_name') or row.get('identity_key')} "
            f"({row.get('concept_type') or 'concept'})"
        )
        if row.get("description"):
            lines.append(f"  Description: {str(row['description'])[:_DOMAIN_TEXT_CAP]}")
    if relationships:
        lines.extend(["", "## Verified relationships", ""])
        for edge in relationships[:_DOMAIN_CONTEXT_EDGE_CAP]:
            source = edge.get("source") or {}
            target = edge.get("target") or {}
            lines.append(
                f"- {source.get('canonical_name') or source.get('identity_key')} "
                f"{edge.get('relation_type')} "
                f"{target.get('canonical_name') or target.get('identity_key')}"
            )
    if isinstance(analysis_context, str) and analysis_context.strip():
        lines.extend(["", "## Saved analysis context", "", analysis_context[:4000]])
    return "\n".join(lines)


def _saved_analysis_context(analysis_id: str, project_id: str) -> str | None:
    """Return bounded, already-normalized saved-analysis facts for grounding."""
    from analysis.analysis_service import project_stem
    from analysis.analysis_store import analysis_store

    row = analysis_store.get(analysis_id)
    if not row or project_stem(str(row.get("filename") or "")) != project_id:
        return None
    profile = row.get("profile") if isinstance(row.get("profile"), dict) else {}
    grounding = str(row.get("grounding_md") or "").strip()
    facts: list[str] = []
    if isinstance(profile, dict):
        for key in ("summary", "routes", "schema", "models", "behavior", "wiring", "config"):
            value = profile.get(key)
            if value:
                rendered = (
                    json.dumps(value, ensure_ascii=False, default=str)
                    if isinstance(value, (dict, list))
                    else str(value)
                )
                facts.append(f"{key}: {rendered[:700]}")
    if grounding:
        facts.append(grounding[:2200])
    return "\n\n".join(facts)[:4000] or None


def _maybe_expanded_domain_context(results: list, body: dict) -> str | None:
    if body.get("domain_context_mode") != "expanded":
        return None
    project_id = _domain_project_id(body)
    if not project_id:
        return None
    from analysis.analysis_service import project_stem
    payload = _expanded_domain_payload(
        project_id,
        query=body.get("query") if isinstance(body.get("query"), str) else None,
        concept_id=(
            str(body.get("domain_concept_id")).strip()
            if body.get("domain_concept_id")
            else None
        ),
    )
    analysis_id = str(body.get("domain_analysis_id") or "").strip()
    if analysis_id:
        try:
            saved_context = _saved_analysis_context(analysis_id, project_id)
            if saved_context:
                payload["analysis_context"] = saved_context
        except Exception:  # noqa: BLE001 — saved context is optional enrichment
            payload.setdefault("warnings", []).append("saved analysis context unavailable")
    return _render_expanded_domain_block(payload)


def _maybe_domain_context(results: list, body: dict) -> str | None:
    """Recognized-domain-concept grounding for /answer requests.

    Opt-in via ``settings.domain_concepts_enabled`` plus a project anchor
    (``filename`` or pinned ``collection``). Runs the bounded recognizer over
    the project's indexed domain concepts and renders a block only when at
    least one concept is recognized. Best-effort: disabled, no anchor, an
    unindexed project, only rejected evidence, or any failure degrades to None
    (no block).
    """
    if not settings.domain_concepts_enabled:
        return None
    if not isinstance(body, dict):
        return None
    if body.get("domain_context_mode") == "expanded":
        return _maybe_expanded_domain_context(results, body)
    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        return None
    project_id = _domain_project_id(body)
    if not project_id or not _domain_store_has_concepts(project_id):
        return None
    try:
        from projects.domain.recognizer import recognize

        return _render_domain_block(recognize(query, project_id))
    except Exception:  # noqa: BLE001 — recognition is optional enrichment
        get_exception_logger().warning(
            "Domain-context grounding skipped", exc_info=True
        )
        return None


_PREVIEW_MODES = {"off", "inline", "gate"}


def _resolve_preview_mode(body: dict) -> str:
    mode = body.get("preview_mode")
    if not isinstance(mode, str) or mode not in _PREVIEW_MODES:
        mode = settings.context_preview_mode
    if mode not in _PREVIEW_MODES:
        mode = "off"
    return mode


def _maybe_business_rule_context(body: dict, query: str) -> tuple[str | None, list[dict]]:
    """Ground project questions in the synced Outline business rules.

    Rule text reaches a remote chat model only when the project allows it.
    """
    project_id = body.get("project_id")
    # The web server sets business_rules=true only after checking the user's project access.
    if not isinstance(project_id, str) or not project_id or body.get("business_rules") is not True:
        return None, []
    from telemetry.snapshot import validate_project_id
    try:
        validate_project_id(project_id)
    except ValueError:
        return None, []
    from code_quality.rule_index import RuleIndex
    from code_quality.rule_search import chat_context
    from runtime.llm import remote_configured
    index = RuleIndex()
    remote_chat = body.get("use_remote", True) is True and remote_configured()
    if remote_chat and not index.allow_remote(project_id):
        return None, []
    context, sources = chat_context(project_id, query, index=index)
    return (context or None), sources


def _answer_service() -> AnswerService:
    return AnswerService(
        answer_context=_answer_context,
        resolve_profile=_resolve_project_profile,
        inspect_context=_maybe_inspect_context,
        domain_context=_maybe_domain_context,
        resolve_preview_mode=_resolve_preview_mode,
        report_stream_error=lambda exc: get_exception_logger().error(
            "SSE answer failed: %s", exc
        ),
        rule_context=_maybe_business_rule_context,
    )


@app.post(
    "/answer",
    tags=["LLM"],
    summary="Generate Answer",
    description=(
        "Generate a final answer from retrieved code context, optional "
        "metadata filters (class_name/file) and extracted entities. "
        "An optional `filename` anchors the request to a project's persisted "
        "indexes (Qdrant + Neo4j); `extra_context` structure/tool text is "
        "always honoured alongside retrieval. "
        "Context-window preview is optional: the request may send "
        "`preview_mode` (\"off\"|\"inline\"|\"gate\") and `preview`: true. "
        "When previewing, the response carries `context_preview`/`preview_only` "
        "with `answer` and `llmUsed` null and no model call is made."
    ),
)
def answer_prompt(
    body: dict,
    x_rag_trace_id: Annotated[str | None, Header(alias="X-RAG-Trace-Id")] = None,
):
    try:
        return _answer_service().answer(body, x_rag_trace_id)
    except InvalidAnswerRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/answer/stream",
    tags=["LLM"],
    summary="Streaming Answer (SSE)",
    description=(
        "Generate an answer and stream it as Server-Sent Events: start, delta, "
        "validation and done events. Same retrieval semantics as /answer "
        "(an optional `filename` anchors project-index grounding). When "
        "context preview is enabled (preview_mode inline/gate) a leading "
        "\"context\" event carrying the rendered context window precedes the "
        "stream; gate-preview requests emit only the context event followed by "
        "a done event (no start/delta) and never invoke the model."
    ),
)
def answer_stream(
    body: dict,
    x_rag_trace_id: Annotated[str | None, Header(alias="X-RAG-Trace-Id")] = None,
):
    if not isinstance(body.get("query"), str) or not body.get("query"):
        raise HTTPException(status_code=400, detail="Missing or invalid query")
    try:
        stream = _answer_service().stream(body, x_rag_trace_id)
    except InvalidAnswerRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StreamingResponse(
        stream,
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

    from rag.evaluation import evaluate_response

    try:
        metrics = evaluate_response(query, answer, contexts)
    except Exception as exc:
        get_exception_logger().error("Response evaluation failed: %s", exc)
        return {"success": False, "error": "Evaluation unavailable for this response"}

    _eval_cache[cache_key] = metrics
    return {"success": True, "metrics": metrics}


def _coerce_setting(key: str, value):
    default = RAG_CONFIG_DEFAULTS.get(key)
    if key == "embeddingProvider":
        if value not in {"ollama", "openAICompatible", "cohere", "cloudflareWorkersAI"}:
            raise HTTPException(status_code=400, detail="embeddingProvider must be ollama, openAICompatible, cohere, or cloudflareWorkersAI")
        return value
    if key == "logDeletionMode":
        return value if value in {"soft", "hard"} else default
    if isinstance(default, int) and not isinstance(value, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            parsed = float(value)
            if key == "rerankerTimeoutS":
                return max(0.1, min(120.0, parsed))
            return parsed
        except (TypeError, ValueError):
            return default
    if key == "rerankerMaxCandidates":
        try:
            return max(1, min(200, int(value)))
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
    if (
        "contextPreviewMode" in incoming
        and incoming["contextPreviewMode"] not in _PREVIEW_MODES
    ):
        raise HTTPException(
            status_code=400,
            detail="contextPreviewMode must be one of off, inline, gate",
        )
    if "embeddingProvider" in incoming and incoming["embeddingProvider"] not in {"ollama", "openAICompatible", "cohere", "cloudflareWorkersAI"}:
        raise HTTPException(status_code=400, detail="embeddingProvider must be ollama, openAICompatible, cohere, or cloudflareWorkersAI")
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


def _tool_view(tool: dict[str, Any]) -> dict[str, Any]:
    out = dict(tool)
    cfg = dict(out.get("config") or {})
    for key in ("apiKey", "token", "password", "secret"):
        if key in cfg and cfg[key]: cfg[key] = ""
    out["config"] = cfg
    return out


@app.get("/tools", tags=["LLM Tools"], summary="List LLM tools")
def list_tools(include_deleted: bool = False):
    builtins = []
    for name, cfg in (orchestrator.config.get("tools") or {}).items():
        builtins.append({"id": f"builtin:{name}", "type": "shell", "name": name, "description": cfg.get("description", name), "config": {"builtin": True}, "enabled": True, "builtin": True})
    return {"success": True, "tools": builtins + [_tool_view(t) for t in tool_store.list(include_deleted)]}


@app.post("/tools", tags=["LLM Tools"], summary="Create an LLM tool")
def create_tool(body: dict):
    try: return {"success": True, "tool": _tool_view(tool_store.save(body))}
    except ValueError as exc: raise HTTPException(status_code=400, detail=str(exc))


@app.put("/tools/{tool_id}", tags=["LLM Tools"], summary="Update an LLM tool")
def update_tool(tool_id: str, body: dict):
    if tool_id.startswith("builtin:"): raise HTTPException(status_code=400, detail="Built-in tools cannot be edited")
    if not tool_store.get(tool_id): raise HTTPException(status_code=404, detail="Tool not found")
    try: return {"success": True, "tool": _tool_view(tool_store.save(body, tool_id))}
    except ValueError as exc: raise HTTPException(status_code=400, detail=str(exc))


@app.post("/tools/{tool_id}/enable", tags=["LLM Tools"])
def enable_tool(tool_id: str):
    tool = tool_store.set_enabled(tool_id, True)
    if not tool: raise HTTPException(status_code=404, detail="Tool not found")
    return {"success": True, "tool": _tool_view(tool)}


@app.post("/tools/{tool_id}/disable", tags=["LLM Tools"])
def disable_tool(tool_id: str):
    tool = tool_store.set_enabled(tool_id, False)
    if not tool: raise HTTPException(status_code=404, detail="Tool not found")
    return {"success": True, "tool": _tool_view(tool)}


@app.delete("/tools/{tool_id}", tags=["LLM Tools"])
def delete_tool(tool_id: str):
    if tool_id.startswith("builtin:"): raise HTTPException(status_code=400, detail="Built-in tools cannot be deleted")
    tool = tool_store.soft_delete(tool_id)
    if not tool: raise HTTPException(status_code=404, detail="Tool not found")
    return {"success": True, "tool": _tool_view(tool)}


@app.post("/tools/{tool_id}/test", tags=["LLM Tools"])
def test_tool(tool_id: str, body: dict):
    tool = tool_store.get(tool_id)
    if not tool: raise HTTPException(status_code=404, detail="Tool not found")
    result = execute_tool(tool, filename=str(body.get("filename") or "project.zip"), arguments=body.get("arguments") or {})
    tool_store.record_run(tool_id, "success" if result.get("success") else "failed", result.get("error"))
    return {"success": bool(result.get("success")), "result": result}


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
    upload_session_id = body.get("upload_session_id")
    snapshot_identity = None
    if not filename or not prompt:
        raise HTTPException(status_code=400, detail="Missing filename or prompt")

    if upload_session_id:
        from index.storage import storage
        match = storage.find_rag_file(filename, upload_session_id)
        if not match:
            raise HTTPException(status_code=404, detail="Uploaded archive not found.")
        if match.get("project_id"):
            if (body.get("project_id") != match["project_id"] or body.get("commit_sha") != match.get("commit_sha")
                    or (body.get("revision_id") and body["revision_id"] != match.get("revision_id"))
                    or (body.get("source_ref") and body["source_ref"] != match.get("source_ref"))):
                raise HTTPException(status_code=403, detail="Upload project identity does not match the archive record.")
            if not match.get("archive_sha256"):
                raise HTTPException(status_code=409, detail="Upload archive checksum is unavailable.")
            snapshot_identity = {
                "project_id": match["project_id"],
                "commit_sha": match.get("commit_sha"),
                "source_ref": match.get("source_ref") or match.get("commit_sha"),
                "source_archive_sha256": match["archive_sha256"],
            }
        elif body.get("project_id") or body.get("commit_sha"):
            raise HTTPException(status_code=409, detail="Legacy uploads cannot be assigned an inferred project identity.")

    job_id = str(uuid.uuid4())
    orchestrator.start_job(
        job_id, route, filename, prompt, skip_index=skip_index, entities=entities,
        upload_session_id=upload_session_id, snapshot_identity=snapshot_identity,
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


_ANSI_RE = None


def _strip_ansi(text: object) -> object:
    """Remove terminal escapes + stray CR so payloads stay strict-JSON friendly."""
    global _ANSI_RE
    if not isinstance(text, str):
        return text
    if _ANSI_RE is None:
        import re as _re

        _ANSI_RE = _re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\r")
    return _ANSI_RE.sub("", text)


def _job_summary(job: dict | None) -> dict | None:
    """Compact, ANSI-free projection of a job (cons F16).

    Excludes the heavy keys (doc_context, full profile, chunk stdout) that made
    the whole status payload unparseable for strict JSON consumers.
    """
    if not job:
        return None
    steps = []
    for s in job.get("steps") or []:
        if isinstance(s, dict):
            steps.append(
                {
                    "key": s.get("key"),
                    "label": s.get("label"),
                    "status": s.get("status"),
                    "detail": s.get("detail"),
                }
            )
    # structure_analyze / index_code jobs store the static structure_profile
    # (framework/version/summary/routes…) and never a project_profile — fall
    # back to it so the summary's profile object is never a blank shape.
    profile = job.get("project_profile") or job.get("structure_profile") or {}
    output = _strip_ansi(job.get("output"))
    summary = {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "error": job.get("error"),
        "route_name": job.get("route_name"),
        "filename": job.get("filename"),
        "collection_name": job.get("collection_name"),
        "created_at": job.get("created_at"),
        "steps": steps,
        "profile": {
            "framework": profile.get("framework"),
            "version": profile.get("version"),
            "routes": len(profile.get("routes") or []),
            "routes_source": profile.get("routes_source"),
            "summary": profile.get("summary"),
        },
        "output_preview": (str(output)[:500] + ("…" if output and len(str(output)) > 500 else ""))
        if output
        else None,
    }
    if job.get("steps_progress"):
        summary["steps_progress"] = job["steps_progress"]
    return summary


@app.get(
    "/orchestrate/{job_id}/summary",
    tags=["Code Intelligence"],
    summary="Orchestration Job Summary (compact)",
    description=(
        "Compact, ANSI-stripped projection of an orchestration job for status "
        "polling and UI history — the full status payload embeds raw stdout "
        "(chunks/doc_context) that breaks strict JSON parsers. Falls back to "
        "the durable result bundle once the in-memory job is evicted."
    ),
)
def orchestration_summary(job_id: str):
    job = orchestrator.get_job(job_id)
    if job is None:
        try:
            from analysis.analysis_service import load_bundle

            bundle = load_bundle(job_id)
        except Exception:  # noqa: BLE001 — job may simply be unknown
            bundle = None
        if bundle:
            job = {
                "job_id": job_id,
                "status": bundle.get("status") or "done",
                "route_name": bundle.get("route_name"),
                "filename": bundle.get("filename"),
                "collection_name": bundle.get("collection_name"),
                "created_at": bundle.get("created_at"),
                "output": bundle.get("output"),
                "project_profile": bundle.get("project_profile"),
                "structure_profile": bundle.get("structure_profile"),
            }
    summary = _job_summary(job)
    if summary is None:
        return {"status": "unknown"}
    return summary


@app.post(
    "/structure/analyze",
    tags=["Analysis"],
    summary="Analyze Project Structure",
    description=(
        "Launches a deterministic static structure-profile job: a multi-language "
        "census of the extracted workspace (framework / language / database / "
        "architecture). Returns a job_id for polling."
    ),
)
def start_structure_analyze(body: dict):
    filename = body.get("filename", "")
    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    prompt = body.get("prompt", "")
    job_id = str(uuid.uuid4())
    orchestrator.start_job(
        job_id,
        "structure_analyze",
        filename,
        prompt or "Analyze this project structure.",
    )
    return {"job_id": job_id}


@app.post(
    "/code/index",
    tags=["Analysis"],
    summary="Index Project Codebase",
    description=(
        "Launches an index_code orchestration job that parses the project "
        "codebase (language-dispatched preview: no embedding at run time). "
        "Returns a job_id for polling; POST /analysis later persists the "
        "chunks into Qdrant + Neo4j."
    ),
)
def start_code_index(body: dict):
    filename = body.get("filename", "")
    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    job_id = str(uuid.uuid4())
    orchestrator.start_job(
        job_id,
        "index_code",
        filename,
        "Index this project codebase.",
    )
    return {"job_id": job_id}


@app.post(
    "/analysis",
    tags=["Analysis"],
    summary="Save an Analysis Run",
    description=(
        "Persists a finished orchestration run (by its durable job bundle) as "
        "a saved analysis. Route-aware: structure_analyze → structure facts, "
        "index_code → code index, every other route → knowledge chunks into "
        "proj_knowledge_<stem> (Qdrant + Neo4j), session row + artifacts."
    ),
)
def save_analysis(body: dict):
    job_id = body.get("job_id", "")
    label = body.get("label", "")
    if not job_id:
        raise HTTPException(status_code=400, detail="Missing job_id")
    try:
        from analysis.analysis_service import persist_for_bundle

        row = persist_for_bundle(str(job_id), label=str(label or ""))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Save failed: {exc}") from exc
    if row is None:
        raise HTTPException(status_code=404, detail="No durable bundle for job_id")
    return {"success": True, "analysis": row}


@app.get(
    "/analysis",
    tags=["Analysis"],
    summary="List Saved Analyses",
    description="Returns saved analyses, newest first.",
)
def list_analyses(limit: int = Query(100, ge=1, le=500)):
    try:
        from analysis.analysis_store import analysis_store

        rows = analysis_store.list(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"List failed: {exc}") from exc
    return {"analyses": rows}


@app.get(
    "/analysis/{analysis_id}",
    tags=["Analysis"],
    summary="Get Saved Analysis",
    description="Returns one saved analysis with its full profile/grounding.",
)
def get_analysis(analysis_id: str):
    try:
        from analysis.analysis_store import analysis_store

        row = analysis_store.get(analysis_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Get failed: {exc}") from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return row


@app.get(
    "/analysis/{analysis_id}/schema-visualization",
    tags=["Analysis"],
    summary="Build Database Schema Visualization",
    description=(
        "Regenerates a deterministic database schema graph from the saved "
        "analysis profile using the detected framework adapter. Migration or "
        "mapping-derived schema is authoritative; model relationships are "
        "optional enrichment. No live database is queried."
    ),
)
def get_schema_visualization(analysis_id: str):
    try:
        from analysis.analysis_store import analysis_store

        row = analysis_store.get(analysis_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Get failed: {exc}") from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Analysis not found")

    from agent.schema_visualization import build_schema_visualization

    profile = row.get("profile") if isinstance(row, dict) else None
    return build_schema_visualization(profile, analysis_id)


@app.get(
    "/analysis/{analysis_id}/domain-model",
    tags=["Analysis"],
    summary="Build Domain Entity Model",
    description=(
        "Returns a deterministic framework-neutral entity/class model from the "
        "saved analysis profile. No database or LLM is queried; optional domain "
        "concept facts are read from the verified concept store."
    ),
)
def get_domain_model(analysis_id: str):
    try:
        from analysis.analysis_store import analysis_store

        row = analysis_store.get(analysis_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Get failed: {exc}") from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    from analysis.analysis_service import project_stem
    from agent.domain_model import build_domain_model, profile_for_saved_analysis

    profile, fallback_id = profile_for_saved_analysis(row, analysis_store)
    project_id = project_stem(str(row.get("filename") or ""))
    result = build_domain_model(profile, analysis_id, project_id=project_id or None)
    if fallback_id:
        result.setdefault("warnings", []).append({
            "code": "profile-fallback",
            "message": "This saved code-index run had no profile; reused authoritative metadata from a same-file structure analysis.",
        })
        result.setdefault("source", {})["profile_analysis_id"] = fallback_id
    return result


def _relative_usage_file(value: Any) -> str | None:
    """Return a safe project-relative-looking path, never an absolute path."""
    if not isinstance(value, str) or not value.strip():
        return None
    path = value.replace("\\", "/").strip()
    if not os.path.isabs(path):
        return path.lstrip("./") or None
    # Inspection exports currently carry absolute workspace paths. Preserve a
    # useful source suffix while refusing to expose the workspace root.
    parts = [part for part in path.split("/") if part]
    markers = {"app", "src", "routes", "database", "config", "resources", "public", "bin", "tests", "lib", "modules", "packages"}
    # An extracted upload is commonly rooted at
    # ``/app/workspaces/<id>/<project>/app/...``.  Prefer the *deepest*
    # project-root marker so the response exposes ``app/...`` rather than the
    # container's temporary workspace path.
    marker_indexes = [
        index for index, part in enumerate(parts)
        if part.casefold() in markers
    ]
    if marker_indexes:
        return "/".join(parts[marker_indexes[-1]:])
    return parts[-1] if parts else None


def _domain_behavior_lookup(model: dict[str, Any], entity_name: str, behavior: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """Resolve an entity behavior only against normalized model metadata."""
    entity = next(
        (item for item in model.get("entities", [])
         if str(item.get("name") or "").casefold() == entity_name.casefold()),
        None,
    )
    if entity is None:
        return None, None, "unknown-entity"
    candidates = [item for item in (entity.get("methods") or []) if isinstance(item, dict)]
    needle = behavior.strip()
    selected = next(
        (item for item in candidates if str(item.get("behavior_key") or "") == needle),
        None,
    )
    if selected is None:
        selected = next(
            (item for item in candidates if str(item.get("signature") or "") == needle),
            None,
        )
    if selected is None:
        same_name = [item for item in candidates if str(item.get("name") or "") == needle]
        selected = same_name[0] if len(same_name) == 1 else None
    if selected is None:
        return entity, None, "unknown-behavior"
    return entity, selected, None


@app.get(
    "/analysis/{analysis_id}/domain-model/usages",
    tags=["Analysis"],
    summary="Get Verified Domain Behavior Usages",
    description=(
        "Returns verified incoming reference sites for one domain-model behavior. "
        "Results come only from the saved project's indexed symbol graph; no "
        "text-search fallback is used."
    ),
)
def get_domain_behavior_usages(
    analysis_id: str,
    entity: str = Query(..., min_length=1, max_length=240),
    behavior: str = Query(..., min_length=1, max_length=400),
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(25, ge=1, le=25),
):
    from analysis.analysis_store import analysis_store
    from analysis.analysis_service import project_stem
    from agent.domain_model import build_domain_model, profile_for_saved_analysis

    row = analysis_store.get(analysis_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    profile, fallback_id = profile_for_saved_analysis(row, analysis_store)
    stem = project_stem(str(row.get("filename") or ""))
    model = build_domain_model(profile, analysis_id, project_id=stem or None)
    entity_row, selected, resolution_error = _domain_behavior_lookup(model, entity, behavior)
    warnings: list[dict[str, str]] = []
    if fallback_id:
        warnings.append({"code": "profile-fallback", "message": "Behavior metadata reused a same-file structure analysis profile."})
    if resolution_error == "unknown-entity":
        warnings.append({"code": "unknown-entity", "message": f"Entity {entity!r} is not present in the saved domain model."})
    elif resolution_error == "unknown-behavior":
        warnings.append({"code": "unknown-behavior", "message": f"Behavior {behavior!r} is not present on entity {entity!r}."})

    definition = None
    if selected:
        definition = {
            "entity": entity_row.get("name") if entity_row else entity,
            "name": selected.get("name"),
            "signature": selected.get("signature") or selected.get("name"),
            "owner": selected.get("owner"),
            "file": _relative_usage_file(selected.get("source_file")),
            "line": selected.get("definition_line"),
            "lookup_key": selected.get("lookup_key"),
            "behavior_key": selected.get("behavior_key"),
            "has_source_identity": bool(selected.get("has_source_identity")),
            "usage_available": bool(selected.get("usage_available")),
        }

    usages: list[dict[str, Any]] = []
    collection = f"sym_{stem or 'unnamed'}"
    index_available = False
    if selected and not resolution_error and selected.get("usage_available"):
        try:
            from inspection.service import inspection_service, sym_collection_for

            collection = sym_collection_for(stem)
            service = inspection_service()
            projects = service.list_projects()
            index_available = any(str(item.get("collection") or "") == collection for item in projects)
            if not index_available:
                warnings.append({"code": "unavailable-index", "message": "No verified reference index is available for this saved analysis."})
            else:
                lookup_key = str(selected.get("lookup_key") or "")
                resolved = service.resolve_symbol(collection, lookup_key) if lookup_key else []
                if not resolved:
                    warnings.append({"code": "behavior-not-indexed", "message": "The behavior has source metadata but no matching indexed symbol."})
                else:
                    for caller in service.find_references(collection, lookup_key):
                        ref = caller.get("ref") if isinstance(caller, dict) else {}
                        file_name = _relative_usage_file((ref or {}).get("file"))
                        line = (ref or {}).get("line")
                        if not file_name or not isinstance(line, int):
                            continue
                        usages.append({
                            "file": file_name,
                            "line": line,
                            "location": f"{file_name}:{line}",
                            "usage_kind": str((ref or {}).get("kind") or "reference"),
                            "caller_class": caller.get("owner") or None,
                            "caller_method": caller.get("name") or None,
                            "caller_fqn": caller.get("fqn") or None,
                        })
        except Exception as exc:  # noqa: BLE001 — graph reads are honest degradation
            warnings.append({"code": "unavailable-index", "message": f"Verified usage index could not be read: {str(exc)[:180]}"})
    elif selected and not selected.get("has_source_identity"):
        warnings.append({"code": "no-source-identity", "message": "No verified usage data is available because this synthesized behavior has no indexed source symbol."})

    usages.sort(key=lambda item: (
        str(item.get("file") or ""),
        int(item.get("line") or 0),
        str(item.get("usage_kind") or ""),
        str(item.get("caller_class") or ""),
        str(item.get("caller_method") or ""),
    ))
    total = len(usages)
    page = usages[offset : offset + limit]
    next_offset = offset + len(page) if offset + len(page) < total else None
    if next_offset is not None:
        warnings.append({"code": "truncated", "message": "More verified usage locations are available."})
    return {
        "success": True,
        "analysis_id": analysis_id,
        "entity": entity_row.get("name") if entity_row else entity,
        "behavior": definition,
        "definition": definition,
        "usages": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": next_offset is not None,
        "next_offset": next_offset,
        "index": {"available": index_available, "collection": collection},
        "warnings": warnings,
    }


@app.post(
    "/analysis/{analysis_id}/domain-model/enrich",
    tags=["Analysis"],
    summary="Enrich Domain Entity Model",
    description=(
        "Explicitly asks the configured LLM to enrich a deterministic entity "
        "model with bounded descriptions, behaviors, or ambiguous relationship "
        "labels. Authoritative schema and key metadata are never overwritten; "
        "errors return the deterministic model with warnings."
    ),
)
def enrich_domain_model_endpoint(analysis_id: str):
    try:
        from analysis.analysis_store import analysis_store

        row = analysis_store.get(analysis_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Get failed: {exc}") from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    from analysis.analysis_service import project_stem
    from agent.domain_model import build_domain_model, enrich_domain_model, profile_for_saved_analysis

    profile, fallback_id = profile_for_saved_analysis(row, analysis_store)
    project_id = project_stem(str(row.get("filename") or ""))
    deterministic = build_domain_model(profile, analysis_id, project_id=project_id or None)
    if fallback_id:
        deterministic.setdefault("warnings", []).append({
            "code": "profile-fallback",
            "message": "This saved code-index run had no profile; reused authoritative metadata from a same-file structure analysis.",
        })
        deterministic.setdefault("source", {})["profile_analysis_id"] = fallback_id
    try:
        enriched = enrich_domain_model(deterministic, profile)
        return enriched
    except Exception as exc:  # enrichment is explicitly optional
        degraded = dict(deterministic)
        degraded["status"] = "degraded"
        degraded["warnings"] = list(degraded.get("warnings") or []) + [
            {"code": "enrichment-failed", "message": str(exc)[:300]}
        ]
        return degraded


@app.delete(
    "/analysis/{analysis_id}",
    tags=["Analysis"],
    summary="Delete Saved Analysis",
    description="Removes the session row + its artifact files.",
)
def delete_analysis(analysis_id: str):
    try:
        from analysis.analysis_store import analysis_store

        deleted = analysis_store.delete(analysis_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return {"success": True}


@app.get(
    "/analysis/{analysis_id}/domain-concepts",
    tags=["Domain Concepts"],
    summary="Browse Saved Analysis Domain Concepts",
    description=(
        "Returns the bounded, read-only canonical domain concepts associated "
        "with a saved analysis. Search and type filters are project-scoped."
    ),
)
def analysis_domain_concepts(
    analysis_id: str,
    search: str = Query("", max_length=200),
    concept_type: str | None = Query(None, max_length=80),
    type_filter: str | None = Query(None, alias="type", max_length=80),
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(50, ge=1, le=_DOMAIN_CONCEPT_LIMIT),
):
    source, project_id, _filename = _analysis_domain_source(analysis_id)
    from projects.domain.store import concept_store

    search = search.strip()
    concept_type = concept_type or type_filter
    concept_type = concept_type.strip() if isinstance(concept_type, str) else None
    warnings: list[str] = []
    try:
        total = (
            concept_store.count_concepts(
                project_id, search=search or None, concept_type=concept_type or None
            )
            if hasattr(concept_store, "count_concepts")
            else len(concept_store.list_concepts(project_id, limit=10000))
        )
        try:
            rows = concept_store.list_concepts(
                project_id,
                limit=limit,
                search=search or None,
                concept_type=concept_type or None,
                offset=offset,
            )
        except TypeError:
            # Keep endpoint seams easy to fake in hermetic tests and compatible
            # with older store adapters while the canonical store evolves.
            all_rows = list(concept_store.list_concepts(project_id, limit=10000) or [])
            needle = search.casefold()
            filtered_rows = [
                row
                for row in all_rows
                if (not needle or needle in " ".join(
                    str(row.get(key) or "")
                    for key in ("identity_key", "canonical_name", "qualified_name", "normalized_name")
                ).casefold())
                and (not concept_type or str(row.get("concept_type") or "") == concept_type)
            ]
            rows = filtered_rows[offset : offset + limit]
            total = len(filtered_rows)
        items = [_public_domain_concept(dict(row), concept_store) for row in rows]
        if hasattr(concept_store, "relationships_for"):
            for item in items:
                if item.get("id"):
                    item["relationship_count"] = len(
                        concept_store.relationships_for(
                            project_id, concept_id=str(item["id"]), limit=5000
                        )
                        or []
                    )
    except Exception as exc:  # noqa: BLE001 — browsing must not expose internals
        raise HTTPException(status_code=500, detail=f"Domain concepts unavailable: {str(exc)[:200]}") from exc
    if offset + len(items) < total:
        warnings.append("more concepts available")
    return {
        "success": True,
        "source": source,
        "items": items,
        "total": int(total),
        "offset": offset,
        "limit": limit,
        "warnings": warnings,
    }


@app.get(
    "/analysis/{analysis_id}/domain-context",
    tags=["Domain Concepts"],
    summary="Get Bounded Expanded Domain Context",
    description=(
        "Recognizes a query or expands one saved-analysis concept through its "
        "verified one-hop relationship neighborhood. Output is bounded and "
        "contains no raw source code."
    ),
)
def analysis_domain_context(
    analysis_id: str,
    query: str = Query("", max_length=500),
    concept_id: str | None = Query(None, max_length=200),
):
    source, project_id, _filename = _analysis_domain_source(analysis_id)
    query = query.strip()
    concept_id = concept_id.strip() if isinstance(concept_id, str) else None
    if not query and not concept_id:
        raise HTTPException(status_code=400, detail="Missing query or concept_id")
    payload = _expanded_domain_payload(
        project_id,
        query=query or None,
        concept_id=concept_id or None,
        source=source,
    )
    saved_context = _saved_analysis_context(analysis_id, project_id)
    if saved_context:
        payload["analysis_context"] = saved_context
    return payload


def _read_domain_dictionary(
    *, search: str, offset: int, limit: int
) -> tuple[list[dict[str, Any]], int, list[str]]:
    from rag.domain_dictionary import DICTIONARY_PATH

    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    try:
        with DICTIONARY_PATH.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, 1):
                line = raw.strip()
                if not line:
                    continue
                term, separator, count = line.partition("\t")
                if not separator or not term.strip():
                    warnings.append(f"ignored malformed dictionary line {line_number}")
                    continue
                try:
                    count_value = int(count.strip())
                except (TypeError, ValueError):
                    warnings.append(f"ignored malformed dictionary count at line {line_number}")
                    continue
                rows.append({"term": term.strip()[:200], "count": max(0, count_value)})
    except OSError:
        warnings.append("domain dictionary unavailable")
    needle = search.casefold()
    if needle:
        rows = [row for row in rows if needle in row["term"].casefold()]
    rows.sort(key=lambda row: row["term"])
    return rows[offset : offset + limit], len(rows), warnings[:20]


@app.get(
    "/domain/dictionary",
    tags=["Domain Concepts"],
    summary="Browse Global Domain Dictionary",
    description="Returns the generated global term-frequency dictionary used by typo correction.",
)
def domain_dictionary(
    search: str = Query("", max_length=200),
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(50, ge=1, le=_DOMAIN_CONCEPT_LIMIT),
):
    items, total, warnings = _read_domain_dictionary(
        search=search.strip(), offset=offset, limit=limit
    )
    return {
        "success": True,
        "scope": "global",
        "source": "domain_dictionary.txt",
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "warnings": warnings,
    }


@app.get(
    "/domain/recognize",
    tags=["Domain Concepts"],
    summary="Recognize Domain Concepts in a Query",
    description=(
        "Maps a free-form query onto a project's indexed domain concepts "
        "(exact/alias/identity matches plus a bounded semantic search). "
        "Returns recognized/ambiguous/rejected hints with verified related "
        "facts. The raw query text is never persisted or logged; any reported "
        "matched alias carries the canonical store's own stored spelling."
    ),
)
def domain_recognize(
    query: str = "",
    project_id: str = "",
    type_hint: str | None = None,
    fuzzy: bool = False,
):
    project_id = project_id.strip() if isinstance(project_id, str) else ""
    if not project_id:
        raise HTTPException(status_code=400, detail="Missing project_id")
    try:
        from projects.domain.recognizer import recognize

        result = recognize(
            str(query or ""),
            project_id,
            type_hint=(
                type_hint.strip()
                if isinstance(type_hint, str) and type_hint.strip()
                else None
            ),
            fuzzy=bool(fuzzy),
        )
    except Exception as exc:  # noqa: BLE001 — recognition stays total over HTTP
        return {"success": False, "error": f"Recognition failed: {exc}"[:300]}
    return {
        "success": True,
        "result": {
            "recognized": list(result.recognized),
            "ambiguous": list(result.ambiguous),
            "rejected": list(result.rejected),
            "relationships": list(result.relationships),
            "degraded": bool(result.degraded),
            "error": result.error,
            "query_note": result.query_note,
        },
    }


@app.post(
    "/domain/reconcile",
    tags=["Domain Concepts"],
    summary="Rebuild a Project's Domain Concept Index",
    description=(
        "Full rebuild of one project's domain concept index from its retained "
        "or HTTP-prepared workspace: clears index state (before the purge so "
        "unchanged rows are re-embedded), re-extracts + upserts concepts, "
        "removes canonical concepts absent from the fresh extraction, then "
        "purges and re-mirrors the Qdrant collection. Never raises; failures "
        "are reported with success:false."
    ),
)
def domain_reconcile(body: dict):
    filename = (body.get("filename") or "") if isinstance(body, dict) else ""
    if not isinstance(filename, str) or not filename.strip():
        raise HTTPException(status_code=400, detail="Missing filename")
    from analysis.analysis_service import reconcile_domain_index

    return reconcile_domain_index(filename.strip())


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

    from index.llamaindex_layer import CodePropertyGraph

    return CodePropertyGraph().hybrid_retrieve(
        query=query,
        collection_name=filter_collection,
        limit=limit,
        class_name=class_name,
        file=file,
    )
