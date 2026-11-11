"""Repository map, language dispatch, edge origin, and scc census (navigation plan tasks 7–10, 15)."""

from pathlib import Path

import pytest

import agent_tools.repo_map as repo_map
import index.indexing_service as indexing
import index.llamaindex_layer as layer
import inspection.queries as queries
from agent_tools.navigation import NAVIGATION_TOOLS, _repo_map, _symbol_row
from agent_tools.source import SourceRoot
from agent_tools.types import ToolContext
from projects import structure_profile


def _chunk(name, cls=None, calls=(), file="a.go", line=1, ns="p"):
    if cls:
        return {"type": "method", "name": name, "class": cls, "class_fqn": f"{ns}\\{cls}", "namespace": ns,
                "file": file, "start_line": line, "calls": list(calls)}
    return {"type": "function", "name": name, "class_fqn": f"{ns}\\{name}", "namespace": ns,
            "file": file, "start_line": line, "calls": list(calls)}


@pytest.fixture(autouse=True)
def map_store(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_MAP_DIR", str(tmp_path / "maps"))


def test_callers_in_other_files_outrank_a_class_calling_its_own_helpers():
    chunks = [
        _chunk("helper", "Lister", file="lister.go"),
        _chunk("run", "Lister", calls=["p\\Lister::helper"], file="lister.go"),
        _chunk("run2", "Lister", calls=["p\\Lister::helper"], file="lister.go"),
        _chunk("isAdmin", "User", file="user.go"),
        _chunk("a", calls=["p\\User::isAdmin"], file="x.go"),
        _chunk("missing", calls=["p\\Nope::gone", "fmt\\Println"], file="x.go"),
    ]
    built = repo_map.build_repo_map(chunks)

    assert built["edges"] == 3 and built["symbols"] == 6
    assert [f["path"] for f in built["files"]][:2] == ["user.go", "lister.go"]
    user = built["files"][0]["symbols"][0]
    assert (user["owner"], user["name"], user["callers"], user["callers_elsewhere"]) == ("User", "isAdmin", 1, 1)
    lister = built["files"][1]
    assert lister["score"] == 0 and lister["callers"] == 2


def test_maps_are_stored_per_revision_and_rebuilt_from_source_when_missing(tmp_path, monkeypatch):
    sha = "a" * 64
    built = []
    monkeypatch.setattr("projects.lang_index.produce_chunks",
                        lambda path: built.append(path) or {"chunks": [_chunk("f")], "producer": "tree-sitter-go"})

    first = repo_map.map_for_source(tmp_path, sha)
    second = repo_map.map_for_source(tmp_path, sha)

    assert first["built"] == "from_source" and second["built"] == "stored" and len(built) == 1
    assert (tmp_path / "maps" / f"{sha}.json").exists()
    # Workspaces without a verified archive are built each time and never stored.
    repo_map.map_for_source(tmp_path, None)
    assert len(built) == 2


def test_repo_map_tool_filters_excluded_paths_and_globs(tmp_path, monkeypatch):
    root = SourceRoot(path=tmp_path, origin="revision", archive_sha256="b" * 64)
    monkeypatch.setattr("agent_tools.navigation.resolve_source", lambda context: root)
    repo_map.store_repo_map("b" * 64, repo_map.build_repo_map([
        _chunk("f", file="app/a.go"), _chunk("g", file="tests/b.go"), _chunk("h", file=".env.go")]))

    result = _repo_map({"path_glob": "app/*", "max_files": 5}, ToolContext(workflow="search"))
    assert [f["path"] for f in result["files"]] == ["app/a.go"]
    names = {d.name for d in NAVIGATION_TOOLS}
    assert "code_repo_map" in names


def test_snapshot_indexing_keeps_php_projects_on_the_php_parser(tmp_path, monkeypatch):
    (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
    (tmp_path / "index.php").write_text("<?php function f() {}\n")
    monkeypatch.setattr(indexing, "run_code_parse", lambda path: [{"name": "php"}])
    monkeypatch.setattr("inspection.cli.inspect_index", lambda path: {"definitions": [], "references": []})

    chunks, _index, error = indexing.IndexingService._parse_source(str(tmp_path), "p.zip")
    assert chunks == [{"name": "php"}] and error is None


def test_snapshot_indexing_uses_tree_sitter_for_go(tmp_path):
    (tmp_path / "main.go").write_text("package main\nfunc helper() {}\nfunc main() { helper() }\n")

    chunks, index, error = indexing.IndexingService._parse_source(str(tmp_path), "p.zip")
    assert error is None and {c["name"] for c in chunks} == {"helper", "main"}
    assert index["references"][0]["origin"] == "tree-sitter"


def test_call_edges_record_their_extractor():
    assert layer._edge_origin({"origin": "tree-sitter"}) == "tree-sitter"
    assert layer._edge_origin({"language": "javascript"}) == "tree-sitter"
    assert layer._edge_origin({"language": "python"}) == "python-ast"
    assert layer._edge_origin({}) == "php-parser"
    assert queries._edge_ref({"rkind": "method_call"})["origin"] == "php-parser"  # edges stored before origins
    assert queries._edge_ref({"rkind": "method_call", "origin": "tree-sitter"})["origin"] == "tree-sitter"
    row = _symbol_row({"kind": "method", "fqn": "p\\A::f", "file": "a.go", "line": 3,
                       "ref": {"file": "b.go", "line": 9, "origin": "tree-sitter"}}, None)
    assert row["origin"] == "tree-sitter" and row["line"] == 9


def test_census_adds_scc_line_counts_only_when_asked(tmp_path, monkeypatch):
    (tmp_path / "a.go").write_text("package a\n// c\nfunc F() {}\n")
    monkeypatch.setattr(structure_profile, "_scc_line_counts",
                        lambda root: {"go": {"lines": 3, "code": 2, "comments": 1, "blanks": 0}})

    plain = structure_profile.language_census(tmp_path)
    counted = structure_profile.language_census(tmp_path, line_counts=True)
    assert plain["languages"] == [{"language": "go", "files": 1}] and "line_counts_source" not in plain
    assert counted["languages"] == [{"language": "go", "files": 1, "lines": 3, "code": 2, "comments": 1, "blanks": 0}]
    assert counted["line_counts_source"] == "scc"


def test_census_without_scc_keeps_file_counts(tmp_path, monkeypatch):
    (tmp_path / "a.go").write_text("package a\n")
    monkeypatch.setattr("shutil.which", lambda name: None)

    census = structure_profile.language_census(tmp_path, line_counts=True)
    assert census["languages"] == [{"language": "go", "files": 1}] and "line_counts_source" not in census
