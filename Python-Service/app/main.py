from contextlib import asynccontextmanager
from typing import Annotated
from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from indexer import CodeIntelligenceIndex
from settings import settings
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
import io
import re
import json
import logging
import threading
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from jinja2 import Template


def sanitize_collection_name(filename: str) -> str:
    """Convert 'my-project.zip' → 'my_project' — safe for Qdrant collection names."""
    stem = Path(filename).stem
    name = re.sub(r"[^a-zA-Z0-9_]", "_", stem)
    name = re.sub(r"_+", "_", name)
    return name.strip("_") or "unnamed"


def split_text_into_chunks(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Split raw text into overlapping, size-bounded chunks for indexing."""
    if chunk_size <= 0:
        chunk_size = 500
    if chunk_overlap < 0:
        chunk_overlap = 0
    if chunk_overlap >= chunk_size:
        chunk_overlap = chunk_size // 2

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)

        # Prefer breaking on a newline or space near the target boundary.
        if end < n:
            window_start = max(start, end - 150)
            window = text[window_start : end + 50]
            nl = window.rfind("\n")
            sp = window.rfind(" ")
            if nl != -1 and (window_start + nl) > start:
                end = window_start + nl + 1
            elif sp != -1 and (window_start + sp) > start:
                end = window_start + sp + 1

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= n:
            break
        next_start = end - chunk_overlap
        start = next_start if next_start > start else end

    return chunks


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


def index_chunks_background(download_id: str, filename: str,
                            chunks_list: list[dict], collection_name: str) -> None:
    """Run embedding + Qdrant indexing in a background thread."""
    dl_logger.info("Background indexing started: collection=%s count=%s",
                   collection_name, len(chunks_list))
    write_status(download_id, "embedding", len(chunks_list), 0, collection_name)

    try:
        qdrant = QdrantClient(url=settings.qdrant_url)

        try:
            qdrant.delete_collection(collection_name)
        except Exception:
            pass

        points = []
        embed_fails = 0
        vector_size = None

        for i, chunk in enumerate(chunks_list):
            chunk_text = chunk.get("code", "")
            chunk_label = chunk.get("name", "unknown")

            if not chunk_text:
                continue

            try:
                embedding = embed_text(chunk_text)
            except Exception as e:
                embed_fails += 1
                dl_logger.error("Embedding failed for chunk #%s '%s': %s",
                                i, chunk_label, e)
                continue

            if vector_size is None:
                vector_size = len(embedding)
                qdrant.create_collection(
                    collection_name=collection_name,
                    vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
                )
                dl_logger.debug("Created collection %s (dim=%s)", collection_name, vector_size)

            point_id = str(uuid.uuid4())
            points.append(PointStruct(
                id=point_id,
                vector=embedding,
                payload={
                    "type": chunk.get("type", ""),
                    "name": chunk.get("name", ""),
                    "class": chunk.get("class", ""),
                    "file": chunk.get("file", ""),
                    "start_line": chunk.get("start_line", 0),
                    "end_line": chunk.get("end_line", 0),
                    "modifiers": chunk.get("modifiers", []),
                    "parameters": chunk.get("parameters", []),
                    "return_type": chunk.get("return_type"),
                    "code": chunk_text,
                    "docblock": chunk.get("docblock"),
                    "calls": chunk.get("calls", {}),
                    "collection": collection_name,
                    "indexed_at": datetime.now(timezone.utc).isoformat(),
                },
            ))

            write_status(download_id, "embedding", len(chunks_list),
                         len(points), collection_name)

        if points:
            write_status(download_id, "upserting", len(chunks_list),
                         len(points), collection_name)
            qdrant.upsert(collection_name=collection_name, wait=True, points=points)

        dl_logger.info("Background indexing complete: collection=%s indexed=%s embed_fails=%s",
                       collection_name, len(points), embed_fails)
        write_status(download_id, "done", len(chunks_list),
                     len(points), collection_name)

    except Exception as e:
        dl_logger.error("Background indexing failed for '%s': %s",
                        collection_name, e, exc_info=True)
        write_status(download_id, "error", len(chunks_list), 0, collection_name)


def embed_text(text: str) -> list[float]:
    """Get embedding vector from Ollama."""
    body = json.dumps({
        "model": settings.ollama_embedding_model,
        "prompt": text,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{settings.ollama_base_url}/api/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.load(resp)
    embedding = result.get("embedding")
    if not embedding or not isinstance(embedding, list):
        raise RuntimeError("Ollama did not return a valid embedding vector")
    return embedding


service = CodeIntelligenceIndex()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        if Path(settings.report_path).is_file():
            service.rebuild(settings.report_path)
    except Exception:
        pass  # First startup or empty report — skip rebuild
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

    # Index chunks into Qdrant in a background thread if provided
    if chunks_payload and isinstance(chunks_payload, dict):
        chunks_list = chunks_payload.get("chunks", [])
        if chunks_list:
            collection_name = sanitize_collection_name(filename)

            response["collection_name"] = collection_name
            response["chunk_count"] = len(chunks_list)

            # Check if already indexed — skip re-indexing
            try:
                qdrant = QdrantClient(url=settings.qdrant_url)
                existing = qdrant.get_collections().collections
                already = any(c.name == collection_name for c in existing)
            except Exception:
                already = False

            if already:
                dl_logger.info("Collection already indexed — skipping: %s", collection_name)
                response["already_indexed"] = True
            else:
                dl_logger.info("Launching background indexing: collection=%s count=%s",
                               collection_name, len(chunks_list))

                thread = threading.Thread(
                    target=index_chunks_background,
                    args=(download_id, filename, chunks_list, collection_name),
                    daemon=True,
                )
                thread.start()

    # No structured code chunks — treat raw text, split & index
    elif text and isinstance(text, str) and len(text.strip()) > 0:
        collection_name = sanitize_collection_name(filename)
        chunks = split_text_into_chunks(
            text, int(settings.chunk_size), int(settings.chunk_overlap)
        )
        if not chunks:
            chunks = [text.strip()]

        text_chunks = [{
            "type": "document",
            "name": f"chunk_{i}",
            "code": c,
            "file": filename,
        } for i, c in enumerate(chunks)]

        response["collection_name"] = collection_name
        response["chunk_count"] = len(text_chunks)

        try:
            qdrant = QdrantClient(url=settings.qdrant_url)
            existing = qdrant.get_collections().collections
            already = any(c.name == collection_name for c in existing)
        except Exception:
            already = False

        if already:
            dl_logger.info("Collection already indexed — skipping: %s", collection_name)
            response["already_indexed"] = True
        else:
            dl_logger.info("Indexing text file: collection=%s paragraphs=%s",
                           collection_name, len(text_chunks))
            thread = threading.Thread(
                target=index_chunks_background,
                args=(download_id, filename, text_chunks, collection_name),
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


@app.post(
    "/answer",
    tags=["Code Intelligence"],
    summary="Generate Answer",
    description=(
        "Retrieves relevant code context from Qdrant/Neo4j and generates a final "
        "LLM answer using the configured local chat model. Accepts optional "
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

    from llamaindex_layer import CodePropertyGraph
    from llamaindex_layer import record_retrieval_trace
    from answer import generate_answer

    # When an explicit tool context is provided (e.g. the project structure from
    # the `knowledge` route), use it as the grounding context instead of searching
    # Qdrant for code chunks.
    if extra_context and isinstance(extra_context, str) and extra_context.strip():
        results = []
        contexts = []
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
    else:
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

    gen = generate_answer(
        query,
        contexts=contexts,
        entities=entities,
        temperature=temperature,
        top_p=top_p,
        extra_context=extra_context if isinstance(extra_context, str) else None,
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
        "llmUsed": f"Local LLM ({gen['model']})",
        "durationMs": gen["duration_ms"],
    }


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
