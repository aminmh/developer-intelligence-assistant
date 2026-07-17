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


class UploadSessionResponse(BaseModel):
    upload_session_id: str = Field(description="Unique identifier for the upload session.")
    tus_endpoint: str = Field(description="TUS server endpoint URL where the client should upload the file.")
    upload_token: str = Field(description="Signed JWT token for authenticating the upload session.")
    expires_at: str = Field(description="ISO 8601 timestamp indicating when the token expires.")


class TUSHookResponse(BaseModel):
    ok: bool = Field(description="Indicates whether the hook was processed successfully.")
    forwarded_to_php: Optional[bool] = Field(None, description="Whether the job was forwarded to the PHP AST analysis service.")
    php_status: Optional[int] = Field(None, description="HTTP status code returned by the PHP service.")
    forwarded_to_node: Optional[bool] = Field(None, description="Whether extracted text was forwarded to the web app's RAG indexing callback.")
    node_status: Optional[int] = Field(None, description="HTTP status code returned by the web app's callback.")
