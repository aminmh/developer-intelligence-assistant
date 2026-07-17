import asyncio
from contextlib import asynccontextmanager
from typing import Annotated
from fastapi import FastAPI, HTTPException, Query, Depends, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import os
import tempfile
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import jwt
import json
import httpx
import boto3
from pypdf import PdfReader
from indexer import CodeIntelligenceIndex
from settings import settings
from upload import UploadSessionRequest, RagUploadSessionRequest, get_current_user, verify_hook, resolve_object_key
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

# RAG document upload (large-file TUS path for the web app's knowledge base).
# A separate, parallel pipeline from the ZIP/AST upload session above.
RAG_ALLOWED_EXTENSIONS = {".pdf", ".zip", ".md", ".txt", ".js", ".ts", ".tsx", ".json"}
MAX_RAG_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2 GiB
WEB_ORIGIN = os.getenv("WEB_ORIGIN", "http://localhost:3000")


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

# Allows the browser to call the RAG upload-session endpoint directly (the
# actual file bytes go straight to tusd, not through this API).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[WEB_ORIGIN],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Content-Type"],
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
    "/api/rag/upload-sessions",
    tags=["Upload & TUS"],
    summary="Create RAG Document Upload Session",
    description=(
        "Creates a resumable upload session for a RAG knowledge-base document "
        "(PDF/MD/TXT/JS/TS/TSX/JSON). Returns a signed JWT token that the client "
        "uses with the TUS resumable upload protocol. Unlike the ZIP upload-session "
        "endpoint above, this is for the web app's document-indexing pipeline, not "
        "AST/code-graph analysis. The token expires after 60 minutes, since large "
        "resumable uploads can take longer than the ZIP flow's 30-minute window."
    ),
    response_model=UploadSessionResponse,
    responses={
        400: {"description": "Unsupported file type for RAG upload"},
        413: {"description": "File is too large (max 2 GiB)"},
    },
)
async def create_rag_upload_session(
    request: RagUploadSessionRequest,
    user=Depends(get_current_user),
):
    ext = os.path.splitext(request.filename.lower())[1]
    if ext not in RAG_ALLOWED_EXTENSIONS:
        raise HTTPException(400, "Unsupported file type for RAG upload")

    if request.size > MAX_RAG_UPLOAD_SIZE:
        raise HTTPException(413, "File is too large")

    upload_session_id = str(uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=60)

    token = jwt.encode(
        payload={
            "sub": user["id"],
            "upload_session_id": upload_session_id,
            "filename": request.filename,
            "maximum_size": request.size,
            "upload_kind": "rag-document",
            "exp": expires_at,
        },
        key=UPLOAD_TOKEN_SECRET,
        algorithm="HS256",
    )

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
    event = json.loads(body)

    if event.get("Type") != "post-finish":
        return TUSHookResponse(ok=True, forwarded_to_php=False, php_status=None)

    upload = event["Event"]["Upload"]
    metadata = upload.get("MetaData", {})

    # RAG documents (web app's large-file upload path) are a separate pipeline
    # from the ZIP/AST one below. tusd cannot sign its own hook deliveries (no
    # HMAC secret flag exists), so this branch authenticates via the session
    # JWT carried in the upload metadata instead of verify_hook's HMAC check.
    if metadata.get("upload_kind") == "rag-document":
        return await handle_rag_upload(upload, metadata)

    verify_hook(body, x_hook_signature)

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


async def handle_rag_upload(upload: dict, metadata: dict) -> TUSHookResponse:
    # Auth: verify the session JWT (unforgeable without UPLOAD_TOKEN_SECRET).
    # This is the real trust boundary for this branch, since tusd has no
    # pre-create hook configured to reject unauthorized uploads before they land
    # in MinIO — only a valid, unexpired, session-matching token gets indexed.
    token = metadata.get("upload_token")
    if not token:
        raise HTTPException(401, "Missing upload token")

    try:
        claims = jwt.decode(token, UPLOAD_TOKEN_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired upload token")

    if claims.get("upload_kind") != "rag-document":
        raise HTTPException(401, "Token not valid for RAG upload")
    if claims.get("upload_session_id") != metadata.get("upload_session_id"):
        raise HTTPException(401, "Upload session mismatch")

    # Authoritative bucket/key come from tusd itself (Storage), not from
    # client-supplied metadata, which could otherwise be spoofed.
    storage = upload.get("Storage") or {}
    bucket = storage.get("Bucket")
    object_key = storage.get("Key")
    if not bucket or not object_key:
        raise HTTPException(400, "Missing upload storage information")

    filename = claims["filename"]
    ext = os.path.splitext(filename.lower())[1]

    if ext == ".zip":
        # ZIP is accepted but its contents are not extracted/parsed, so there's
        # no need to download the (possibly huge) archive at all -- index a
        # placeholder instead, same as the small-file path in rag.ts.
        text = f"Uploaded ZIP archive: {filename} (binary content, not text-indexed)."
        file_type = "zip"
    else:
        # boto3 and pypdf are both synchronous/blocking; run them off the event
        # loop so a huge download/parse doesn't stall other requests (health
        # checks, concurrent hook deliveries) for the duration.
        tmp_path = await asyncio.to_thread(_download_to_tempfile, bucket, object_key)
        try:
            if ext == ".pdf":
                text = await asyncio.to_thread(_extract_pdf_text, tmp_path)
                file_type = "pdf"
            else:
                with open(tmp_path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
                file_type = "markdown" if ext == ".md" else "text"
        finally:
            os.unlink(tmp_path)

    node_callback_url = os.environ["NODE_RAG_CALLBACK_URL"]
    rag_callback_secret = os.environ["RAG_CALLBACK_SECRET"]

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            node_callback_url,
            headers={
                "X-Internal-Secret": rag_callback_secret,
                "Content-Type": "application/json",
            },
            json={
                "name": filename,
                "type": file_type,
                "size": upload.get("Size", 0),
                "text": text,
                "uploadSessionId": metadata.get("upload_session_id"),
            },
        )

    response.raise_for_status()

    return TUSHookResponse(
        ok=True,
        forwarded_to_node=True,
        node_status=response.status_code,
    )


def _download_to_tempfile(bucket: str, key: str) -> str:
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT"],
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.getenv("AWS_REGION", "us-east-1"),
    )
    # download_fileobj streams the object in parts rather than buffering the
    # whole thing in memory, and writing straight to disk means we never hold
    # a second full in-heap copy of a potentially huge file's raw bytes.
    fd, tmp_path = tempfile.mkstemp()
    with os.fdopen(fd, "wb") as tmp:
        s3.download_fileobj(bucket, key, tmp)
    return tmp_path


def _extract_pdf_text(path: str) -> str:
    # PdfReader accepts a file path directly, so parsing reads/seeks against
    # the on-disk file rather than requiring a second in-memory copy. Page
    # objects are extracted and discarded one at a time rather than all at once.
    reader = PdfReader(path)
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n".join(pages)
