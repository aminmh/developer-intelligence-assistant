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


def test_vue_components_tsx_and_angular_templates_are_indexed(tmp_path):
    (tmp_path / "OrderCard.vue").write_text(
        "<template>\n  <article>{{ total() }}</article>\n</template>\n\n"
        "<script setup lang=\"ts\">\nfunction total(): number {\n  return 1;\n}\n</script>\n")
    (tmp_path / "Page.tsx").write_text("export function Page() {\n  return <main><p>hi</p></main>;\n}\n")
    (tmp_path / "home.component.html").write_text("<main><app-list></app-list></main>\n")
    (tmp_path / "index.html").write_text("<html></html>\n")

    result = produce_chunks(tmp_path, "typescript")
    chunks = {chunk["name"]: chunk for chunk in result["chunks"]}

    assert result["malformed_files"] == []  # .tsx parses with the TSX grammar
    assert chunks["total"]["file"] == "OrderCard.vue" and chunks["total"]["start_line"] == 6  # line in the .vue file
    assert chunks["OrderCard"]["origin"] == "markup" and "<article>" in chunks["OrderCard"]["code"]
    assert chunks["Page"]["type"] == "function"
    assert "<app-list>" in chunks["HomeComponentTemplate"]["code"]
    assert not any(chunk["file"] == "index.html" for chunk in result["chunks"])
    assert all(from_chunk(chunk) for chunk in result["chunks"] if chunk["type"] == "function")


def test_file_limit_allows_real_front_end_projects():
    from projects import js_ts_index

    assert js_ts_index._MAX_FILES >= 4000
