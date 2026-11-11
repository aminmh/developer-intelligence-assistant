from pipelines.orchestrator import Orchestrator


def test_skip_index_probe_requires_existing_collection(monkeypatch):
    monkeypatch.setattr(
        "index.llamaindex_layer.qdrant_collection_has_points",
        lambda name: name == "chunk_code_other_project",
    )

    assert Orchestrator._qdrant_collection_exists("chunk_code_armaiti_api_develop") is False
    assert Orchestrator._qdrant_collection_exists("chunk_code_other_project") is True


def test_skip_index_probe_retries_when_qdrant_unavailable(monkeypatch):
    def unavailable(_name):
        raise RuntimeError("qdrant unavailable")

    monkeypatch.setattr("index.llamaindex_layer.qdrant_collection_has_points", unavailable)

    assert Orchestrator._qdrant_collection_exists("chunk_code_armaiti_api_develop") is False


def test_code_route_indexes_go_projects_without_the_php_parser(tmp_path, monkeypatch):
    (tmp_path / "main.go").write_text("package main\nfunc helper() {}\nfunc main() { helper() }\n")
    monkeypatch.setattr("infra.php_tools.run_code_index", lambda path: (_ for _ in ()).throw(AssertionError("PHP CLI used")))

    class Inspection:
        def index(self, path, stem, exported=None, snapshot=None):
            return {"indexed": True, "definitions": len(exported["definitions"]), "references": len(exported["references"])}

    monkeypatch.setattr("inspection.service.inspection_service", lambda: Inspection())
    import json

    result = Orchestrator.__new__(Orchestrator)._php_code_parse(str(tmp_path), {"filename": "go.zip"})
    payload = json.loads(result["stdout"])
    assert result["success"] is True and {c["name"] for c in payload["chunks"]} == {"helper", "main"}
    assert payload["symbol_index"] == {"indexed": True, "definitions": 2, "references": 1}


def test_code_route_keeps_php_projects_and_names_unindexable_languages(tmp_path):
    (tmp_path / "index.php").write_text("<?php\n")
    (tmp_path / "main.go").write_text("package main\n")
    assert Orchestrator._non_php_index(str(tmp_path)) is None

    other = tmp_path / "cpp"
    other.mkdir()
    (other / "a.cpp").write_text("int main() { return 0; }\n")
    assert Orchestrator._non_php_index(str(other))["error"].startswith("No code index for this cpp project")
