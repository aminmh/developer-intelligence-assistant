You are a senior LlamaIndex and Knowledge Graph architect. Your task is to implement the indexing and retrieval pipeline in `llamaindex_layer.py` that processes MCP tool results into a Property Graph Index, stores vectors in Qdrant, and retrieves them using a hybrid Vector + Graph approach.

### Requirements

1. **Document Conversion & Metadata Cleanup:**
   - Convert the JSON-RPC/MCP tool output into a LlamaIndex `Document` or `TextNode`.
   - Extract key metadata: `class_fqn`, `namespace`, `filename`, and the lists/dicts of `calls` and `callers`.
   - Clean/exclude heavy raw attributes like `calls`, `callers`, and `project_id` from the LLM-visible metadata using `excluded_llm_metadata_keys` to avoid token bloat, while keeping them accessible for relationship generation.

2. **Property Graph Construction:**
   - Use LlamaIndex's `PropertyGraphIndex` (or Custom Graph/Node parsers) to map code entities and their dependencies.
   - Define Nodes as code symbols (methods/classes).
   - Define Relations using the `calls` array (e.g., Node `A` -[:CALLS]-> Node `B`).
   - Derive deterministic node IDs (SHA-1 of class_fqn + method name) to ensure indexing runs are idempotent and retries do not duplicate nodes/edges.

3. **Storage Configuration (Qdrant & Graph Store):**
   - Configure the `PropertyGraphIndex` to use `QdrantVectorStore` as its vector store backend.
   - Use a compatible graph store (e.g., `SimpleGraphStore` persisted to disk/MinIO, or integrated database) to persist the edges and property relationships.

4. **Hybrid Retrieval Pipeline:**
   - Implement a retriever that:
     a) Queries `QdrantVectorStore` for semantic similarity to find the target code context.
     b) Traverses the Property Graph Index to fetch direct dependencies (callee/caller nodes linked via the `calls` relation).
   - Merge these retrieved nodes, apply a `NodePostprocessor` for deduplication and reranking, and synthesize the final context.

### Hard Rules
- Failures in the graph indexing or traversal must degrade gracefully to a standard vector-only search, without crashing the job status lifecycle.
