import json
from typing import Any, Dict, List, Optional, Tuple

from llama_index.core import Document, Settings, VectorStoreIndex
from llama_index.vector_stores.qdrant import QdrantVectorStore
from qdrant_client import QdrantClient

from embeddings import get_llamaindex_embed_model
from graph_store import InMemoryCodeGraph
from models import EdgeModel, ParameterModel, SymbolModel
from settings import settings

# Embeddings come from the configured embedding server (Ollama by default, or
# an OpenAI-compatible endpoint like LM Studio when embeddingBaseUrl is set).
Settings.embed_model = get_llamaindex_embed_model()


def build_symbol_id(chunk: Dict[str, Any]) -> str:
    class_name = chunk.get("class") or "__global__"
    name = chunk.get("name") or "__unknown__"
    file = chunk.get("file") or "__unknown_file__"
    start_line = chunk.get("start_line") or 0
    return f"{class_name}::{name}@{file}:{start_line}"


def normalize_parameters(chunk: Dict[str, Any]) -> List[ParameterModel]:
    params = []
    for p in chunk.get("parameters", []) or []:
        params.append(
            ParameterModel(
                name=p.get("name"),
                type=p.get("type"),
                default=p.get("default"),
            )
        )
    return params


def chunk_to_symbol(chunk: Dict[str, Any]) -> SymbolModel:
    modifiers = chunk.get("modifiers", []) or []
    visibility = None
    for candidate in ["public", "protected", "private"]:
        if candidate in modifiers:
            visibility = candidate
            break

    return SymbolModel(
        symbol_id=build_symbol_id(chunk),
        kind=chunk.get("type", "unknown"),
        name=chunk.get("name", "__unknown__"),
        class_name=chunk.get("class"),
        file=chunk.get("file", "__unknown_file__"),
        start_line=chunk.get("start_line"),
        end_line=chunk.get("end_line"),
        visibility=visibility,
        return_type=chunk.get("return_type"),
        parameters=normalize_parameters(chunk),
        code=chunk.get("code", ""),
        docblock=chunk.get("docblock"),
        raw=chunk,
    )


def build_resolution_maps(symbols: List[SymbolModel]) -> Tuple[Dict[Tuple[Optional[str], str], str], Dict[str, List[str]]]:
    by_class_and_name: Dict[Tuple[Optional[str], str], str] = {}
    by_name: Dict[str, List[str]] = {}

    for symbol in symbols:
        by_class_and_name[(symbol.class_name, symbol.name)] = symbol.symbol_id
        by_name.setdefault(symbol.name, []).append(symbol.symbol_id)

    return by_class_and_name, by_name


def resolve_call_target(
    source_symbol: SymbolModel,
    raw_target: str,
    by_class_and_name: Dict[Tuple[Optional[str], str], str],
    by_name: Dict[str, List[str]],
) -> Optional[str]:
    # 1) same class resolution
    same_class = by_class_and_name.get((source_symbol.class_name, raw_target))
    if same_class:
        return same_class

    # 2) unique global name match
    candidates = by_name.get(raw_target, [])
    if len(candidates) == 1:
        return candidates[0]

    return None


def extract_call_names(raw_calls: Any) -> List[str]:
    if raw_calls is None:
        return []

    if isinstance(raw_calls, list):
        values = []
        for item in raw_calls:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict):
                if "name" in item and isinstance(item["name"], str):
                    values.append(item["name"])
        return values

    if isinstance(raw_calls, dict):
        values = []
        for key, value in raw_calls.items():
            if isinstance(key, str):
                values.append(key)
            if isinstance(value, list):
                for inner in value:
                    if isinstance(inner, str):
                        values.append(inner)
                    elif isinstance(inner, dict) and isinstance(inner.get("name"), str):
                        values.append(inner["name"])
        return values

    return []


def symbol_to_document(symbol: SymbolModel) -> Document:
    param_str = ", ".join(
        f"{p.type or 'mixed'} {p.name or ''}".strip()
        for p in symbol.parameters
    )

    text = "\n".join(
        [
            f"Symbol: {symbol.class_name or '__global__'}::{symbol.name}",
            f"Kind: {symbol.kind}",
            f"File: {symbol.file}",
            f"Lines: {symbol.start_line}-{symbol.end_line}",
            f"Visibility: {symbol.visibility}",
            f"Return Type: {symbol.return_type}",
            f"Parameters: {param_str}",
            "",
            "Code:",
            symbol.code or "",
        ]
    )

    metadata = {
        "symbol_id": symbol.symbol_id,
        "class_name": symbol.class_name,
        "name": symbol.name,
        "kind": symbol.kind,
        "file": symbol.file,
        "start_line": symbol.start_line,
        "end_line": symbol.end_line,
        "visibility": symbol.visibility,
        "return_type": symbol.return_type,
    }

    doc = Document(text=text)
    doc.metadata = metadata
    return doc


class CodeIntelligenceIndex:
    def __init__(self) -> None:
        self.graph = InMemoryCodeGraph()
        self.vector_index: Optional[VectorStoreIndex] = None
        self.retriever = None
        self.symbols_count = 0
        self.edges_count = 0
        self.unresolved_edges_count = 0

    def _configure_embedding_model(self) -> None:
        if settings.embedding_backend == "ollama":
            Settings.embed_model = get_llamaindex_embed_model()

    def rebuild(self, report_path: str) -> None:
        self.graph = InMemoryCodeGraph()
        self.vector_index = None
        self.retriever = None
        self.symbols_count = 0
        self.edges_count = 0
        self.unresolved_edges_count = 0

        self._configure_embedding_model()

        with open(report_path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        chunks = payload.get("chunks", [])
        symbols = [chunk_to_symbol(chunk) for chunk in chunks]

        for symbol in symbols:
            self.graph.add_symbol(symbol)

        by_class_and_name, by_name = build_resolution_maps(symbols)

        for symbol in symbols:
            raw_calls = symbol.raw.get("calls")
            call_names = extract_call_names(raw_calls)

            for raw_target in call_names:
                target_id = resolve_call_target(
                    source_symbol=symbol,
                    raw_target=raw_target,
                    by_class_and_name=by_class_and_name,
                    by_name=by_name,
                )
                resolved = target_id is not None
                edge = EdgeModel(
                    source_id=symbol.symbol_id,
                    relation="CALLS",
                    target_id=target_id,
                    raw_target=raw_target,
                    resolved=resolved,
                    meta={},
                )
                self.graph.add_edge(edge)
                self.edges_count += 1
                if not resolved:
                    self.unresolved_edges_count += 1

        documents = [symbol_to_document(s) for s in symbols]

        qdrant_client = QdrantClient(url=settings.qdrant_url)
        vector_store = QdrantVectorStore(
            client=qdrant_client,
            collection_name=settings.qdrant_collection,
        )

        self.vector_index = VectorStoreIndex.from_documents(
            documents,
            vector_store=vector_store,
            show_progress=True,
        )
        self.retriever = self.vector_index.as_retriever(similarity_top_k=5)
        self.symbols_count = len(symbols)

    def semantic_search(self, query: str, top_k: int = 5) -> List[dict]:
        if not self.retriever:
            return []

        self.retriever.similarity_top_k = top_k
        nodes = self.retriever.retrieve(query)

        results = []
        for node in nodes:
            metadata = node.metadata or {}
            results.append(
                {
                    "symbol_id": metadata.get("symbol_id"),
                    "file": metadata.get("file"),
                    "class_name": metadata.get("class_name"),
                    "name": metadata.get("name"),
                    "score": getattr(node, "score", None),
                    "snippet": node.text[:400],
                }
            )
        return results

    def get_symbol_context(self, class_name: str, method_name: str) -> Optional[dict]:
        matches = self.graph.find_exact(class_name, method_name)
        if not matches:
            return None

        symbol = matches[0]
        return {
            "symbol": symbol.model_dump(),
            "outgoing_calls": [e.model_dump() for e in self.graph.outgoing(symbol.symbol_id)],
            "incoming_calls": [e.model_dump() for e in self.graph.incoming(symbol.symbol_id)],
        }

    def get_callers_of(self, class_name: str, method_name: str) -> List[dict]:
        matches = self.graph.find_exact(class_name, method_name)
        if not matches:
            return []

        symbol = matches[0]
        incoming = self.graph.incoming(symbol.symbol_id)

        rows = []
        for edge in incoming:
            caller = self.graph.get_symbol(edge.source_id)
            if caller:
                rows.append(
                    {
                        "caller_symbol_id": caller.symbol_id,
                        "caller_class": caller.class_name,
                        "caller_name": caller.name,
                        "caller_file": caller.file,
                    }
                )
        return rows

    def get_callees_of(self, class_name: str, method_name: str) -> List[dict]:
        matches = self.graph.find_exact(class_name, method_name)
        if not matches:
            return []

        symbol = matches[0]
        outgoing = self.graph.outgoing(symbol.symbol_id)

        rows = []
        for edge in outgoing:
            callee = self.graph.get_symbol(edge.target_id) if edge.target_id else None
            rows.append(
                {
                    "raw_target": edge.raw_target,
                    "resolved": edge.resolved,
                    "target_symbol_id": callee.symbol_id if callee else None,
                    "target_class": callee.class_name if callee else None,
                    "target_name": callee.name if callee else None,
                    "target_file": callee.file if callee else None,
                }
            )
        return rows

    def get_retriever(self, collection_name: str, top_k: int = 10):
        """Create a LlamaIndex retriever for any Qdrant collection."""
        from llama_index.vector_stores.qdrant import QdrantVectorStore
        from qdrant_client import QdrantClient

        qdrant_client = QdrantClient(url=settings.qdrant_url)
        vector_store = QdrantVectorStore(
            client=qdrant_client,
            collection_name=collection_name,
        )
        index = VectorStoreIndex.from_vector_store(
            vector_store,
            embed_model=Settings.embed_model,
        )
        retriever = index.as_retriever(
            similarity_top_k=top_k,
            embed_model=Settings.embed_model,
        )
        return retriever

    def ingest_chunks(self, chunks: List[dict], collection_name: str) -> dict:
        """Ingest a list of chunk dicts into a named Qdrant collection synchronously."""
        from llama_index.vector_stores.qdrant import QdrantVectorStore
        from qdrant_client import QdrantClient

        documents = []
        for c in chunks:
            if not isinstance(c, dict):
                continue
            # Build embeddable text — same pattern as CodeChunk.toEmbeddableText()
            parts = []
            if c.get("docblock"):
                parts.append(str(c["docblock"]))
            if c.get("class"):
                parts.append(f"Class: {c['class']}")
            if c.get("name"):
                sig_parts = [str(c["name"])]
                params = c.get("parameters", [])
                if params:
                    param_strs = []
                    for p in params:
                        p_name = p.get("name", "") if isinstance(p, dict) else str(p)
                        p_type = p.get("type", "") if isinstance(p, dict) else ""
                        param_strs.append(f"{p_type} ${p_name}" if p_type else f"${p_name}")
                    sig_parts.append("(" + ", ".join(param_strs) + ")")
                if c.get("return_type"):
                    sig_parts.append(": " + str(c["return_type"]))
                parts.append("Signature: " + "".join(sig_parts))

            code = c.get("code", "")
            if code:
                parts.append(code)

            node_text = "\n".join(parts) if parts else (str(c.get("name", "")) or "unnamed")
            if not node_text.strip():
                continue

            metadata = {
                "type": c.get("type", ""),
                "name": c.get("name", ""),
                "class": c.get("class", ""),
                "file": c.get("file", ""),
                "start_line": c.get("start_line", 0),
                "end_line": c.get("end_line", 0),
                "code": code,
                "collection": collection_name,
            }

            documents.append(Document(text=node_text, metadata=metadata))

        if not documents:
            return {"indexed": 0, "total": 0, "collection_name": collection_name}

        qdrant_client = QdrantClient(url=settings.qdrant_url)
        vector_store = QdrantVectorStore(
            client=qdrant_client,
            collection_name=collection_name,
        )

        # Recreate collection to avoid stale data
        try:
            qdrant_client.delete_collection(collection_name)
        except Exception:
            pass

        index = VectorStoreIndex.from_documents(
            documents,
            vector_store=vector_store,
            embed_model=Settings.embed_model,
            show_progress=False,
        )

        return {
            "indexed": len(documents),
            "total": len(chunks),
            "collection_name": collection_name,
        }

