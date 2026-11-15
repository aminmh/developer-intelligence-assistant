from __future__ import annotations

from projects.lang_index import produce_chunks


def test_python_index_reports_malformed_and_unhandled_definitions(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n    pass\n")
    (tmp_path / "nested.py").write_text(
        "def outer():\n    def inner():\n        return 1\n    return inner()\n"
    )

    result = produce_chunks(tmp_path, language="python")

    assert result["coverage"] == "partial"
    assert result["malformed_files"] == ["broken.py"]
    assert any(item["construct"] == "nested_definition" for item in result["unsupported_constructs"])


def test_python_methods_emit_relationships_and_canonical_source_ids(tmp_path):
    (tmp_path / "service.py").write_text(
        "class Service:\n"
        "    def run(self):\n"
        "        return self.save()\n"
        "    def save(self):\n"
        "        return 1\n"
    )

    result = produce_chunks(tmp_path, language="python")
    run_method = next(chunk for chunk in result["chunks"] if chunk["name"] == "run")
    save_method = next(chunk for chunk in result["chunks"] if chunk["name"] == "save")

    assert result["coverage"] == "complete"
    assert run_method["calls"] == [f"{run_method['class_fqn']}::save"]
    assert run_method["class_fqn"] == save_method["class_fqn"]
    assert result["chunks"][0]["namespace"] == "service"
