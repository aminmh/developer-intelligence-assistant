from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ParameterModel(BaseModel):
    name: Optional[str] = Field(None, description="Parameter name.")
    type: Optional[str] = Field(None, description="Parameter type hint.")
    default: Optional[str] = Field(None, description="Default value, if any.")


class SymbolModel(BaseModel):
    symbol_id: str = Field(description="Unique identifier for the symbol (class_name::name@file:line).")
    kind: str = Field(description="Symbol kind (e.g. function, method, class).")
    name: str = Field(description="Method or function name.")
    class_name: Optional[str] = Field(None, description="Fully-qualified class name, if the symbol belongs to a class.")
    file: str = Field(description="Source file path relative to the project root.")
    start_line: Optional[int] = Field(None, description="Starting line of the symbol in the source file.")
    end_line: Optional[int] = Field(None, description="Ending line of the symbol in the source file.")
    visibility: Optional[str] = Field(None, description="Visibility modifier: public, protected, or private.")
    return_type: Optional[str] = Field(None, description="Return type hint.")
    parameters: List[ParameterModel] = Field(default_factory=list, description="List of function/method parameters.")
    code: str = Field(default="", description="The full source code of the symbol.")
    docblock: Optional[str] = Field(None, description="PHPDoc / docblock comment string.")
    raw: Dict[str, Any] = Field(default_factory=dict, description="Raw JSON chunk from the report payload.")
    schema_version: str = "ast-chunk-v1"
    chunk_id: Optional[str] = None
    language: str = "php"
    node_type: Optional[str] = None
    symbol_name: Optional[str] = None
    fq_symbol_name: Optional[str] = None
    signature: Optional[str] = None
    parent_symbol: Optional[str] = None
    method_name: Optional[str] = None
    function_name: Optional[str] = None
    is_static: bool = False
    imports: List[str] = Field(default_factory=list)
    extends: List[str] = Field(default_factory=list)
    implements: List[str] = Field(default_factory=list)
    traits: List[str] = Field(default_factory=list)
    referenced_symbols: List[str] = Field(default_factory=list)
    literals: List[str] = Field(default_factory=list)
    annotations: List[str] = Field(default_factory=list)
    framework_hints: List[str] = Field(default_factory=list)
    domain_tags: List[str] = Field(default_factory=list)
    low_information: bool = False


class EdgeModel(BaseModel):
    source_id: str = Field(description="Symbol ID of the caller.")
    relation: str = Field(description="Type of relation (always 'CALLS').")
    target_id: Optional[str] = Field(None, description="Symbol ID of the callee, if resolved.")
    raw_target: str = Field(description="Unresolved call target name as extracted from the source code.")
    resolved: bool = Field(False, description="Whether the call was successfully resolved to a known symbol.")
    meta: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata about the call site.")


class SearchResponseItem(BaseModel):
    symbol_id: Optional[str] = Field(None, description="Unique symbol identifier.")
    file: Optional[str] = Field(None, description="Source file path.")
    class_name: Optional[str] = Field(None, description="Class name the symbol belongs to, if any.")
    name: Optional[str] = Field(None, description="Method or function name.")
    score: Optional[float] = Field(None, description="Cosine similarity score (0–1).")
    snippet: str = Field(description="Truncated code snippet (first 400 characters).")


class SymbolLookupResponse(BaseModel):
    symbol: SymbolModel = Field(description="The requested code symbol.")
    outgoing_calls: List[EdgeModel] = Field(description="List of calls made by this symbol (callees).")
    incoming_calls: List[EdgeModel] = Field(description="List of calls made to this symbol (callers).")


class RebuildResponse(BaseModel):
    symbols: int = Field(description="Total number of symbols indexed.")
    edges: int = Field(description="Total number of call-graph edges found.")
    unresolved_edges: int = Field(description="Number of call edges that could not be resolved.")
    collection: str = Field(description="Name of the Qdrant collection used for vector storage.")


class HealthResponse(BaseModel):
    status: str = Field(description="Service health status (always 'ok' if reachable).")
    symbols: int = Field(description="Total number of indexed code symbols.")
    edges: int = Field(description="Total number of call-graph edges.")
    unresolved_edges: int = Field(description="Number of unresolved call edges.")
    collection: str = Field(description="Active Qdrant collection name.")


class SearchResponse(BaseModel):
    query: str = Field(description="The original search query.")
    results: List[dict] = Field(description="List of search result items with symbol_id, file, class_name, name, score, and snippet.")


class CallerItem(BaseModel):
    caller_symbol_id: str = Field(description="Symbol ID of the caller.")
    caller_class: Optional[str] = Field(None, description="Class name of the caller.")
    caller_name: str = Field(description="Method or function name of the caller.")
    caller_file: str = Field(description="Source file path of the caller.")


class CallersResponse(BaseModel):
    target: str = Field(description="The target symbol in class_name::method_name format.")
    count: int = Field(description="Number of callers found.")
    items: List[CallerItem] = Field(description="List of caller symbols.")


class CalleeItem(BaseModel):
    raw_target: str = Field(description="Unresolved call target name as written in the source code.")
    resolved: bool = Field(description="Whether the target was resolved to a known symbol.")
    target_symbol_id: Optional[str] = Field(None, description="Symbol ID of the callee, if resolved.")
    target_class: Optional[str] = Field(None, description="Class name of the callee, if resolved.")
    target_name: Optional[str] = Field(None, description="Method or function name of the callee, if resolved.")
    target_file: Optional[str] = Field(None, description="Source file path of the callee, if resolved.")


class CalleesResponse(BaseModel):
    source: str = Field(description="The source symbol in class_name::method_name format.")
    count: int = Field(description="Number of callees found.")
    items: List[CalleeItem] = Field(description="List of callee symbols.")


class DownloadJobResponse(BaseModel):
    download_id: str = Field(description="Unique job identifier for status polling.")
    download_token: str = Field(description="JWT token for downloading the parsed result.")
    download_url: str = Field(description="Full URL to download the result file.")
    expires_at: str = Field(description="ISO 8601 expiration timestamp of the download token.")
    collection_name: Optional[str] = Field(None, description="Qdrant collection name if chunks were indexed.")
    chunk_count: Optional[int] = Field(None, description="Number of chunks indexed into the vector DB.")
    already_indexed: Optional[bool] = Field(False, description="True if collection already existed — no re-indexing needed.")


class CodeChunkModel(BaseModel):
    schema_version: Optional[str] = None
    chunk_id: Optional[str] = None
    language: Optional[str] = None
    node_type: Optional[str] = None
    file_path: Optional[str] = None
    symbol_name: Optional[str] = None
    fq_symbol_name: Optional[str] = None
    signature: Optional[str] = None
    parent_symbol: Optional[str] = None
    method_name: Optional[str] = None
    function_name: Optional[str] = None
    visibility: Optional[str] = None
    is_static: Optional[bool] = None
    imports: Optional[List[str]] = None
    extends: Optional[List[str]] = None
    implements: Optional[List[str]] = None
    traits: Optional[List[str]] = None
    loc: Optional[int] = None
    called_symbols: Optional[List[str]] = None
    referenced_symbols: Optional[List[str]] = None
    literals: Optional[List[str]] = None
    annotations: Optional[List[str]] = None
    framework_hints: Optional[List[str]] = None
    domain_tags: Optional[List[str]] = None
    low_information: Optional[bool] = None
    summary: Optional[str] = None
    type: str = Field(description="Chunk type: function, method, closure.")
    name: str = Field(description="Function or method name.")
    class_: Optional[str] = Field(None, alias="class", description="Fully-qualified class name.")
    file: Optional[str] = Field(None, description="Source file path.")
    start_line: Optional[int] = Field(None, description="Start line in source.")
    end_line: Optional[int] = Field(None, description="End line in source.")
    modifiers: Optional[List[str]] = Field(None, description="Visibility/static modifiers.")
    parameters: Optional[List[dict]] = Field(None, description="Function parameter list.")
    return_type: Optional[str] = Field(None, description="Return type hint.")
    code: str = Field(default="", description="Source code of the symbol.")
    docblock: Optional[str] = Field(None, description="PHPDoc / docblock string.")
    calls: Optional[Dict[str, Any]] = Field(None, description="Call graph data.")
    has_docblock: Optional[bool] = Field(None, description="Whether docblock exists.")

    class Config:
        populate_by_name = True
