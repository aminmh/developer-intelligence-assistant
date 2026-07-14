from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Depends, Request, Header
from fastapi.responses import JSONResponse
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from jwt import JWT
import json
import httpx
from .indexer import CodeIntelligenceIndex
from .settings import settings
from .upload import UploadSessionRequest, get_current_user, verify_hook, resolve_object_key


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


app = FastAPI(
    title="LlamaIndex Code Intelligence Service",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "symbols": service.symbols_count,
        "edges": service.edges_count,
        "unresolved_edges": service.unresolved_edges_count,
        "collection": settings.qdrant_collection,
    }


@app.post("/rebuild")
def rebuild():
    service.rebuild(settings.report_path)
    return {
        "symbols": service.symbols_count,
        "edges": service.edges_count,
        "unresolved_edges": service.unresolved_edges_count,
        "collection": settings.qdrant_collection,
    }


@app.get("/search")
def search(q: str = Query(...), top_k: int = Query(5, ge=1, le=20)):
    return {
        "query": q,
        "results": service.semantic_search(q, top_k=top_k),
    }


@app.get("/symbols/{class_name}/{method_name}")
def symbol_context(class_name: str, method_name: str):
    result = service.get_symbol_context(class_name, method_name)
    if not result:
        raise HTTPException(status_code=404, detail="Symbol not found")
    return result


@app.get("/callers/{class_name}/{method_name}")
def callers(class_name: str, method_name: str):
    rows = service.get_callers_of(class_name, method_name)
    return {
        "target": f"{class_name}::{method_name}",
        "count": len(rows),
        "items": rows,
    }


@app.get("/callees/{class_name}/{method_name}")
def callees(class_name: str, method_name: str):
    rows = service.get_callees_of(class_name, method_name)
    return {
        "source": f"{class_name}::{method_name}",
        "count": len(rows),
        "items": rows,
    }


@app.post("/api/upload-sessions")
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
    jwt = JWT()

    token = jwt.encode(
        payload={
            "sub": user["id"],
            "upload_session_id": upload_session_id,
            "filename": request.filename,
            "maximum_size": request.size,
            "exp": expires_at,
        },
        key=UPLOAD_TOKEN_SECRET,
        alg="HS256",
    )

    # Persist this session in your database:
    # id, user_id, filename, expected_size, status="created"

    return {
        "upload_session_id": upload_session_id,
        "tus_endpoint": TUS_PUBLIC_URL,
        "upload_token": token,
        "expires_at": expires_at.isoformat(),
    }



@app.post("/internal/tusd/hooks")
async def tusd_hook(
    request: Request,
    x_hook_signature: str | None = Header(default=None),
):
    body = await request.body()
    verify_hook(body, x_hook_signature)

    event = json.loads(body)

    if event.get("Type") != "post-finish":
        return {"ok": True}

    upload = event["Event"]["Upload"]
    metadata = upload.get("MetaData", {})

    upload_session_id = metadata.get("upload_session_id")
    upload_id = upload["ID"]
    object_key = resolve_object_key(upload)

    if not upload_session_id:
        raise HTTPException(400, "Missing upload session ID")

    # Perform these operations transactionally:
    # 1. Verify the upload session exists and belongs to the authenticated user.
    # 2. Verify the final byte count matches the expected size.
    # 3. Mark the upload as completed.
    # 4. Enqueue an AST analysis job.
    #

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

    # Python calls the PHP service here.
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

    return JSONResponse({
        "ok": True,
        "forwarded_to_php": True,
        "php_status": response.status_code,
    })