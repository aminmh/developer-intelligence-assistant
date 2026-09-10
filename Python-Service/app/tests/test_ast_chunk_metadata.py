from __future__ import annotations

from index.llamaindex_layer import (
    CodePropertyGraph,
    _graph_properties,
    _payload,
    build_embeddable_text,
    enrich_chunks_with_symbol_index,
)


def test_payload_preserves_normalized_ast_metadata_without_raw_ast():
    chunk = {
        "schema_version": "ast-chunk-v1",
        "chunk_id": "chunk-1",
        "language": "php",
        "node_type": "method",
        "symbol_name": "charge",
        "fq_symbol_name": "App\\Billing\\Gateway::charge",
        "class_fqn": "App\\Billing\\Gateway",
        "name": "charge",
        "class": "Gateway",
        "namespace": "App\\Billing",
        "file": "src/Gateway.php",
        "method_name": "charge",
        "signature": "charge(int $amount): PaymentResult",
        "parent_symbol": "App\\Billing\\Gateway",
        "imports": ["App\\Payment\\PaymentResult"],
        "extends": [],
        "implements": ["App\\Contracts\\Gateway"],
        "traits": [],
        "called_symbols": ["App\\Payment\\PaymentResult::from"],
        "referenced_symbols": ["App\\Payment\\PaymentResult"],
        "framework_hints": ["laravel"],
        "domain_tags": ["payment"],
        "code": "return PaymentResult::from($amount);",
    }

    payload = _payload(chunk, "chunk_code_demo")

    assert payload["schema_version"] == "ast-chunk-v1"
    assert payload["node_type"] == "method"
    assert payload["fq_symbol_name"].endswith("::charge")
    assert payload["called_symbols"] == ["App\\Payment\\PaymentResult::from"]
    assert payload["domain_tags"] == ["payment"]
    assert "raw" not in payload


def test_embeddable_text_has_contextual_header_and_source():
    text = build_embeddable_text(
        {
            "file": "src/Gateway.php",
            "namespace": "App\\Billing",
            "class": "Gateway",
            "method_name": "charge",
            "signature": "charge(int $amount): PaymentResult",
            "summary": "Handles payment authorization.",
            "code": "return $this->authorize($amount);",
        }
    )

    assert "File: src/Gateway.php" in text
    assert "Namespace: App\\Billing" in text
    assert "Class: Gateway" in text
    assert "Method: charge" in text
    assert "Summary: Handles payment authorization." in text
    assert "return $this->authorize($amount);" in text


def test_symbol_export_facts_attach_to_qdrant_chunk_metadata():
    chunks = [{
        "fq_symbol_name": "App\\Services\\CommentService::create",
        "class_fqn": "App\\Services\\CommentService",
        "name": "create",
        "code": "return $this->repo->save($comment);",
    }]
    definitions = [{
        "kind": "method",
        "fqn": "App\\Services\\CommentService::create",
    }]
    references = [
        {
            "kind": "new",
            "scope": "App\\Services\\CommentService::create",
            "to_fqn": "App\\Models\\Comment",
            "to_member": None,
            "unresolved": False,
        },
        {
            "kind": "method_call",
            "scope": "App\\Services\\CommentService::create",
            "to_fqn": None,
            "to_member": "save",
            "unresolved": True,
        },
    ]

    enriched = enrich_chunks_with_symbol_index(chunks, definitions, references)
    payload = _payload(enriched[0], "chunk_code_comments")

    assert payload["symbol_identity"].endswith("::create")
    assert payload["definition_kind"] == "method"
    assert payload["reference_kinds"] == ["method_call", "new"]
    assert payload["referenced_symbols"] == ["App\\Models\\Comment"]
    assert payload["unresolved_reference_count"] == 1
    assert payload["reference_evidence"][1]["unresolved"] is True
    assert "References: App\\Models\\Comment" in build_embeddable_text(enriched[0])


def test_graph_properties_do_not_send_nested_reference_maps_to_neo4j():
    props = _graph_properties(
        {
            "name": "create",
            "reference_kinds": ["method_call"],
            "reference_evidence": [{"kind": "method_call", "line": 12}],
        },
        "chunk_code_demo",
    )

    assert "reference_evidence" not in props
    assert props["reference_kinds"] == ["method_call"]


class _BatchQdrant:
    def __init__(self):
        self.upserts = []

    def delete_collection(self, _collection):
        return None

    def create_collection(self, **_kwargs):
        return None

    def upsert(self, *, collection_name, wait, points):
        self.upserts.append((collection_name, wait, list(points)))


class _Embedder:
    def get_text_embedding(self, _text):
        return [0.1, 0.2, 0.3]


def test_qdrant_ingest_batches_large_projects():
    graph = CodePropertyGraph()
    qdrant = _BatchQdrant()
    graph._get_qdrant = lambda: qdrant  # type: ignore[method-assign]
    graph._get_embed_model = lambda: _Embedder()  # type: ignore[method-assign]
    chunks = [
        {
            "class_fqn": "App\\Service",
            "name": f"method_{index}",
            "code": "return true;",
        }
        for index in range(130)
    ]

    ingested, embed_fails = graph._ingest_vectors(chunks, "chunk_code_demo")

    assert ingested == 130
    assert embed_fails == 0
    assert [len(points) for _, _, points in qdrant.upserts] == [64, 64, 2]
