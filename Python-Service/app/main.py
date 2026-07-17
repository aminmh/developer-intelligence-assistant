from contextlib import asynccontextmanager
from typing import Annotated
from fastapi import FastAPI, HTTPException, Query, Depends, Request, Header
from fastapi.responses import JSONResponse
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import jwt
import json
import httpx
from indexer import CodeIntelligenceIndex
from settings import settings
from upload import UploadSessionRequest, get_current_user, verify_hook, resolve_object_key
from models import (
    HealthResponse,
    RebuildResponse,
    SearchResponse,
    SymbolLookupResponse,
    CallersResponse,
    CalleesResponse,
    UploadSessionResponse,
    TUSHookResponse,
)


service = CodeIntelligenceIndex()


TUS_PUBLIC_URL = os.environ.get(
    "TUS_PUBLIC_URL",
    "https://uploads.example.com/files/",
)
UPLOAD_TOKEN_SECRET = os.environ["UPLOAD_TOKEN_SECRET"]
MAX_UPLOAD_SIZE = 5 * 1024 * 1024 * 1024  # 5 GiB


@asynccontextmanager
async def lifespan(app: FastAPI):
    service.rebuild(settings.report_path)
    yield


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
    "/api/upload-sessions",
    tags=["Upload & TUS"],
    summary="Create Upload Session",
    description=(
        "Creates a resumable upload session for a ZIP file. Validates the filename "
        "extension and file size, then returns a signed JWT token that the client can "
        "use with the TUS resumable upload protocol. The token expires after 30 minutes."
    ),
    response_model=UploadSessionResponse,
    responses={
        400: {"description": "Only ZIP files are accepted"},
        413: {"description": "ZIP file is too large (max 5 GiB)"},
    },
)
async def create_upload_session(
    request: UploadSessionRequest,
    user=Depends(get_current_user),
):
    if not request.filename.lower().endswith(".zip"):
        raise HTTPException(400, "Only ZIP files are accepted")

    if request.size > MAX_UPLOAD_SIZE:
        raise HTTPException(413, "ZIP file is too large")

    upload_session_id = str(uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)

    token = jwt.encode(
        payload={
            "sub": user["id"],
            "upload_session_id": upload_session_id,
            "filename": request.filename,
            "maximum_size": request.size,
            "exp": expires_at,
        },
        key=UPLOAD_TOKEN_SECRET,
        algorithm="HS256",
    )

    # Persist this session in your database:
    # id, user_id, filename, expected_size, status="created"

    return UploadSessionResponse(
        upload_session_id=upload_session_id,
        tus_endpoint=TUS_PUBLIC_URL,
        upload_token=token,
        expires_at=expires_at.isoformat(),
    )


@app.post(
    "/internal/tusd/hooks",
    tags=["Upload & TUS"],
    summary="TUSD Webhook Handler",
    description=(
        "Receives post-finish hook events from tusd after a resumable upload completes. "
        "Verifies the HMAC-SHA256 signature, extracts upload metadata, and forwards the "
        "job details to the internal PHP AST analysis service."
    ),
    response_model=TUSHookResponse,
    responses={
        400: {"description": "Missing upload session ID or metadata"},
        401: {"description": "Missing or invalid hook signature"},
    },
    include_in_schema=False,
)
async def tusd_hook(
    request: Request,
    x_hook_signature: str | None = Header(default=None),
):
    body = await request.body()
    verify_hook(body, x_hook_signature)

    event = json.loads(body)

    if event.get("Type") != "post-finish":
        return TUSHookResponse(ok=True, forwarded_to_php=False, php_status=None)

    upload = event["Event"]["Upload"]
    metadata = upload.get("MetaData", {})

    upload_session_id = metadata.get("upload_session_id")
    upload_id = upload["ID"]

    if not upload_session_id:
        raise HTTPException(400, "Missing upload session ID")

    TUSD_HOOK_SECRET = os.getenv("TUSD_HOOK_SECRET", "hook-secret")
    PHP_INTERNAL_URL = os.getenv(
        "PHP_INTERNAL_URL",
        "http://php/api/internal/analyze-ast",
    )
    PHP_SHARED_SECRET = os.getenv("PHP_SHARED_SECRET", "php-secret")
    processed_uploads = set()

    job_id = metadata.get("job_id")
    bucket = metadata.get("bucket")
    object_key = metadata.get("object_key")

    if not job_id or not bucket or not object_key:
        raise HTTPException(status_code=400, detail="Missing upload metadata")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            PHP_INTERNAL_URL,
            headers={
                "X-Internal-Secret": PHP_SHARED_SECRET,
                "Content-Type": "application/json",
            },
            json={
                "job_id": job_id,
                "bucket": bucket,
                "object_key": object_key,
            },
        )

    response.raise_for_status()
    processed_uploads.add(upload_id)

    return TUSHookResponse(
        ok=True,
        forwarded_to_php=True,
        php_status=response.status_code,
    )
