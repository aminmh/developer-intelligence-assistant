from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ParameterModel(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    default: Optional[str] = None


class SymbolModel(BaseModel):
    symbol_id: str
    kind: str
    name: str
    class_name: Optional[str] = None
    file: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    visibility: Optional[str] = None
    return_type: Optional[str] = None
    parameters: List[ParameterModel] = Field(default_factory=list)
    code: str = ""
    docblock: Optional[str] = None
    raw: Dict[str, Any] = Field(default_factory=dict)


class EdgeModel(BaseModel):
    source_id: str
    relation: str
    target_id: Optional[str] = None
    raw_target: str
    resolved: bool = False
    meta: Dict[str, Any] = Field(default_factory=dict)


class SearchResponseItem(BaseModel):
    symbol_id: str
    file: str
    class_name: Optional[str] = None
    name: str
    score: Optional[float] = None
    snippet: str


class SymbolLookupResponse(BaseModel):
    symbol: SymbolModel
    outgoing_calls: List[EdgeModel]
    incoming_calls: List[EdgeModel]


class RebuildResponse(BaseModel):
    symbols: int
    edges: int
    unresolved_edges: int
    collection: str

