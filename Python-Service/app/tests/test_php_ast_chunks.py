from __future__ import annotations

import json
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
