from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_language_pack")

from projects.js_ts_index import produce_chunks
from telemetry.symbols import from_chunk


def test_typescript_adapter_emits_stable_symbols_locations_and_local_calls(tmp_path):
    source = """export function loadUser(id: string): User {\n  return normalize(id);\n}\nfunction normalize(value: string) { return value; }\n"""
    (tmp_path / "users.ts").write_text(source)

    first = produce_chunks(tmp_path, "typescript")
    second = produce_chunks(tmp_path, "typescript")

    assert first["coverage"] == "complete"
    assert first["chunks"] == second["chunks"]
    load_user = next(chunk for chunk in first["chunks"] if chunk["name"] == "loadUser")
    assert load_user["file"] == "users.ts"
    assert load_user["start_line"] == 1
    assert load_user["end_line"] == 3
    assert load_user["calls"] == [f"{load_user['namespace']}\\normalize"]
    assert from_chunk(load_user) == from_chunk(next(chunk for chunk in second["chunks"] if chunk["name"] == "loadUser"))
    assert from_chunk(load_user)


def test_adapter_reports_unsupported_and_malformed_coverage(tmp_path):
    (tmp_path / "types.ts").write_text("interface User { id: string }\nfunction broken( {\n")

    result = produce_chunks(tmp_path, "typescript")

    assert result["coverage"] == "partial"
    assert result["malformed_files"] == ["types.ts"]
    assert any(item["construct"] == "interface_declaration" for item in result["unsupported_constructs"])
