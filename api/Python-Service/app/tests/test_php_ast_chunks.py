from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path


def test_php_cli_emits_hierarchical_ast_chunks(tmp_path):
    source = tmp_path / "Gateway.php"
    source.write_text(
        "<?php\n"
        "namespace App\\Billing;\n"
        "use App\\Contracts\\GatewayContract;\n"
        "final class Gateway implements GatewayContract {\n"
        "    private string $provider;\n"
        "    public const VERSION = 1;\n"
        "    /** Authorize a payment. */\n"
        "    public static function authorize(string $id): bool {\n"
        "        return Payment::charge($id);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    service_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["php", "php/bin/code-parse", "--path", str(source), "--output", "json"],
        cwd=service_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    chunks = json.loads(result.stdout)
    types = {chunk["node_type"] for chunk in chunks}
    assert {"class", "method", "property_group", "constant_group"}.issubset(types)
    method = next(chunk for chunk in chunks if chunk["node_type"] == "method")
    assert method["file_path"] == source.name
    assert method["namespace"] == "App\\Billing"
    assert method["class_name"] == "Gateway"
    assert method["method_name"] == "authorize"
    assert method["parent_symbol"] == "App\\Billing\\Gateway"
    assert method["is_static"] is True
    assert method["called_symbols"] == ["App\\Billing\\Payment::charge"]
    class_chunk = next(chunk for chunk in chunks if chunk["node_type"] == "class")
    assert "Methods: authorize" in class_chunk["summary"]


def test_php_cli_emits_hand_computed_callable_metrics(tmp_path):
    source = tmp_path / "Metrics.php"
    source.write_text(
        "<?php\nnamespace App;\n"
        "use App\\Contracts\\Store;\n"
        "class Metrics {\n"
        "  private int $value;\n"
        "  public function __construct(Store $store) {}\n"
        "  public function inspect($a, $b) {\n"
        "    $this->value = 1;\n"
        "    if ($a && $b) {\n"
        "      while ($a) { $this->value += 1; break; }\n"
        "    }\n"
        "    if ($a instanceof Store) { throw new \\LogicException(); }\n"
        "    return new Store();\n"
        "  }\n"
        "}\n", encoding="utf-8",
    )
    service_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["php", "php/bin/code-parse", "--path", str(source), "--output", "json"],
        cwd=service_root, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    chunks = json.loads(result.stdout)
    method = next(chunk for chunk in chunks if chunk.get("method_name") == "inspect")
    assert method["metric_schema"] == "code-metrics-1"
    assert method["cyclomatic"] == 5
    assert method["max_nesting"] == 2
    assert method["param_count"] == 2
    assert method["reads_properties"] == ["value"]
    assert method["writes_properties"] == ["value"]
    assert method["type_checks"] == 1
    assert method["instantiates"] == ["App\\Contracts\\Store", "LogicException"]
    assert method["throws"] == ["LogicException"]
    assert method["content_hash"] == hashlib.sha256(method["code"].encode()).hexdigest()
    declaration = next(chunk for chunk in chunks if chunk["node_type"] == "class")
    assert declaration["constructor_dependencies"] == [{"name": "$store", "type": "App\\Contracts\\Store"}]
    assert all("content_hash" in chunk for chunk in chunks)
