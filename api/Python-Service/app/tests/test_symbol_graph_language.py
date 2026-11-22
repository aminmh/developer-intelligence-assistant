"""Language-neutral symbol graph (lsp-first-symbol-navigation plan, Phase 2).

The label is `Symbol` (was `PhpSymbol`), every node carries `language` and `origin`,
legacy nodes are relabelled once, and lookups prefer `scip` > `lsp` > parser edges.
"""

from __future__ import annotations

from inspection import graph as graph_module
from inspection.graph import (
    InspectionGraph,
    default_origin,
    language_for_path,
    migrate_legacy_labels,
    node_properties,
    stamp_origin,
)


def test_language_for_path_uses_suffix():
    assert language_for_path("app/Models/User.php") == "php"
    assert language_for_path("/abs/src/auth.TS") == "typescript"
    assert language_for_path("main.go") == "go"
    assert language_for_path("README") is None
    assert language_for_path(None) is None


def test_default_origin_only_assumes_the_php_parser_for_php():
    assert default_origin("php") == "php-parser"
    assert default_origin(None) == "php-parser"  # graphs from before language/origin existed
    assert default_origin("go") == "unknown"


def test_node_properties_carry_language_and_producer_origin():
    lsp = node_properties({"kind": "method", "name": "f", "fqn": "A::f", "file": "a.php", "origin": "lsp"}, "sym_x")
    assert lsp["language"] == "php" and lsp["origin"] == "lsp"
    legacy = node_properties({"kind": "class", "name": "A", "fqn": "A", "file": "a.php"}, "sym_x")
    assert legacy["origin"] == "php-parser"
    go = node_properties({"kind": "function", "name": "main", "fqn": "main", "file": "main.go"}, "sym_x")
    assert go["language"] == "go" and go["origin"] == "unknown"
    explicit = node_properties({"kind": "class", "name": "A", "fqn": "A", "file": "x", "language": "kotlin",
                                "origin": "lsp"}, "sym_x")
    assert explicit["language"] == "kotlin"


def test_stamp_origin_fills_missing_fields_only():
    payload = {"definitions": [{"file": "/w/a.php"}, {"file": "b.go", "origin": "tree-sitter"}],
               "references": [{"file": "/w/a.php"}]}
    stamp_origin(payload, "php-parser")
    assert payload["definitions"][0] == {"file": "/w/a.php", "origin": "php-parser", "language": "php"}
    assert payload["definitions"][1]["origin"] == "tree-sitter"
    assert payload["references"][0]["origin"] == "php-parser"


class _LegacyStore:
    """Fake Neo4j store holding `legacy` PhpSymbol nodes; each batch relabels up to the batch size."""

    def __init__(self, legacy: int):
        self.legacy = legacy
        self.statements: list[str] = []

    def structured_query(self, statement, param_map=None):
        self.statements.append(statement)
        moved = min(self.legacy, graph_module._MIGRATION_BATCH)
        self.legacy -= moved
        return [{"count": moved}]


def test_migration_relabels_in_batches_until_none_left(monkeypatch):
    monkeypatch.setattr(graph_module, "_MIGRATION_BATCH", 3)
    store = _LegacyStore(7)
    assert migrate_legacy_labels(store) == 7
    assert store.legacy == 0
    assert len(store.statements) == 3  # 3 + 3 + 1
    cypher = store.statements[0]
    assert "MATCH (s:PhpSymbol)" in cypher and "SET s:Symbol" in cypher and "REMOVE s:PhpSymbol" in cypher
    assert "ENDS WITH '.php' THEN 'php'" in cypher and "coalesce(s.language" in cypher


def test_migration_on_empty_graph_costs_one_query():
    store = _LegacyStore(0)
    assert migrate_legacy_labels(store) == 0
    assert len(store.statements) == 1


def test_migration_runs_once_per_process_and_retries_after_failure(monkeypatch):
    calls = []

    def flaky(store):
        calls.append(store)
        if len(calls) == 1:
            raise RuntimeError("neo4j busy")
        return 0

    monkeypatch.setattr(graph_module, "migrate_legacy_labels", flaky)
    monkeypatch.setattr(InspectionGraph, "_legacy_checked", False)
    InspectionGraph._migrate_legacy("store")
    assert InspectionGraph._legacy_checked is False  # failed: try again on the next connection
    InspectionGraph._migrate_legacy("store")
    InspectionGraph._migrate_legacy("store")
    assert len(calls) == 2


def test_all_symbol_cypher_uses_the_neutral_label():
    from inspection import queries

    texts = [v for v in vars(graph_module).values() if isinstance(v, str) and "MATCH" in v]
    texts += [v for v in vars(queries).values() if isinstance(v, str) and "MATCH" in v]
    assert texts
    assert not any(":PhpSymbol" in text for text in texts)


def test_query_rows_report_language_and_legacy_origin():
    from inspection.queries import _node_row

    row = _node_row({"kind": "method", "name": "f", "fqn": "A::f", "file": "src/a.py", "origin": "scip"})
    assert row["language"] == "python" and row["origin"] == "scip"
    legacy = _node_row({"kind": "method", "name": "f", "fqn": "A::f", "file": "/w/A.php"})
    assert legacy["language"] == "php" and legacy["origin"] == "php-parser"


def test_lookup_prefers_scip_then_lsp_then_parser():
    from agent_tools.navigation import _prefer_precise

    rows = [{"path": "a.php", "line": 3, "symbol": "A::f", "origin": "php-parser"},
            {"path": "a.php", "line": 3, "symbol": "A::f", "origin": "lsp"},
            {"path": "a.php", "line": 5, "symbol": "A::g", "origin": "lsp"},
            {"path": "a.php", "line": 5, "symbol": "A::g", "origin": "scip"},
            {"path": "a.php", "line": 9, "symbol": "A::h", "origin": "php-parser"}]
    chosen = sorted((r["line"], r["origin"]) for r in _prefer_precise(rows))
    assert chosen == [(3, "lsp"), (5, "scip"), (9, "php-parser")]


def test_chunk_edge_origin_by_language():
    from index.llamaindex_layer import _edge_origin

    assert _edge_origin({"language": "php"}) == "php-parser"
    assert _edge_origin({"language": "go", "origin": "tree-sitter"}) == "tree-sitter"
    assert _edge_origin({"language": "kotlin"}) == "unknown"
    assert _edge_origin({}) == "php-parser"  # chunks from before multi-language indexing


def test_cli_index_export_is_stamped_php_parser(monkeypatch):
    from inspection import cli

    monkeypatch.setattr(cli, "run_inspect", lambda *a, **k: {
        "definitions": [{"kind": "class", "fqn": "A", "file": "/w/A.php"}],
        "references": [{"kind": "new", "to_fqn": "A", "file": "/w/B.php"}]})
    out = cli.inspect_index("/w")
    assert out["definitions"][0]["origin"] == "php-parser" and out["definitions"][0]["language"] == "php"
    assert out["references"][0]["origin"] == "php-parser"
