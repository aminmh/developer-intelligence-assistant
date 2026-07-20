from contextlib import asynccontextmanager
from typing import Annotated
from fastapi import FastAPI, HTTPException, Query
from indexer import CodeIntelligenceIndex
from settings import settings
from models import (
    HealthResponse,
    RebuildResponse,
    SearchResponse,
    SymbolLookupResponse,
    CallersResponse,
    CalleesResponse
)


service = CodeIntelligenceIndex()


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
