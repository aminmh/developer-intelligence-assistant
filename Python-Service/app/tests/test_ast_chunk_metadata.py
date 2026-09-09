from __future__ import annotations

from index.llamaindex_layer import _payload, build_embeddable_text


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
