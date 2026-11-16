"""Index-time LSP symbol driver (lsp/indexer.py) with a scripted fake server.

Contract: external-analyzer spec clauses 32-37 and failure clause 8;
decision: language-servers-feed-the-symbol-graph-at-index-time.adr.md.
"""

from __future__ import annotations

import os
from pathlib import Path

from lsp.adapters.php import Adapter
from lsp.client import LspConnectionError, LspTimeoutError
from lsp.indexer import IndexLimits, index_workspace, isolated_env, source_files

ACTION = """<?php

namespace App\\Actions;

class GreetAction
{
    public function execute(string $name): string
    {
        return $this->format($name);
    }

    private function format(string $name): string
    {
        return "Hi " . $name;
    }
}
"""

CONTROLLER = """<?php

namespace App\\Http;

use App\\Actions\\GreetAction;

class GreetController
{
    use Responds;

    public function show(GreetAction $action): string
    {
        return $action->execute('x') . OtherAction::execute();
    }
}
"""

TRAIT = """<?php

namespace App\\Http;

trait Responds
{
}
"""


def _rng(line, start, end):
    return {"start": {"line": line, "character": start}, "end": {"line": line, "character": end}}


def _sym(name, kind, line, start, end, first, last, children=()):
    return {"name": name, "kind": kind, "range": {"start": {"line": first, "character": 0},
                                                  "end": {"line": last, "character": 1}},
            "selectionRange": _rng(line, start, end), "children": list(children)}


def _write_project(root: Path) -> None:
    (root / "app/Actions").mkdir(parents=True)
    (root / "app/Http").mkdir(parents=True)
    (root / "vendor/pkg").mkdir(parents=True)
    (root / "app/Actions/GreetAction.php").write_text(ACTION)
    (root / "app/Http/GreetController.php").write_text(CONTROLLER)
    (root / "app/Http/Responds.php").write_text(TRAIT)
    (root / "vendor/pkg/Lib.php").write_text("<?php class Lib {}\n")


class FakeServer:
    """Scripted language server; one instance per index run."""

    instances: list["FakeServer"] = []

    def __init__(self, root: Path, *, references=None, implementations=None, capabilities=None,
                 fail_start=False, **kwargs):
        self.root = root
        self.kwargs = kwargs
        self.opened: list[str] = []
        self.open_now: set[str] = set()
        self.ref_calls: list[tuple] = []
        self._references = references or {}
        self._implementations = implementations or {}
        self._capabilities = capabilities if capabilities is not None else {
            "documentSymbolProvider": True, "referencesProvider": True, "implementationProvider": True}
        self.fail_start = fail_start
        self.shut = False
        FakeServer.instances.append(self)

    def uri(self, rel):
        return (self.root / rel).as_uri()

    def start(self):
        if self.fail_start:
            raise LspConnectionError("boom")
        return self._capabilities

    def supports(self, name):
        return bool(self._capabilities.get(f"{name}Provider"))

    def open_document(self, uri, text):
        self.opened.append(uri)
        self.open_now.add(uri)

    def close_document(self, uri):
        self.open_now.discard(uri)

    def document_symbols(self, uri, timeout=None):
        if uri.endswith("GreetAction.php"):
            return [_sym("GreetAction", 5, 4, 6, 17, 4, 15, [
                _sym("execute", 6, 6, 20, 27, 6, 9),
                _sym("format", 6, 11, 21, 27, 11, 14),
            ])]
        if uri.endswith("GreetController.php"):
            return [_sym("GreetController", 5, 6, 6, 21, 6, 14, [
                _sym("show", 6, 10, 20, 24, 10, 13),
            ])]
        if uri.endswith("Responds.php"):
            return [_sym("Responds", 5, 4, 6, 14, 4, 6)]
        return []

    def references(self, uri, line, character, timeout=None):
        assert uri in self.open_now, "references asked for a document that is not open"
        self.ref_calls.append((uri.rsplit("/", 1)[-1], line))
        found = self._references.get((uri.rsplit("/", 1)[-1], line))
        if isinstance(found, Exception):
            raise found
        return found or []

    def implementations(self, uri, line, character, timeout=None):
        assert uri in self.open_now
        return self._implementations.get((uri.rsplit("/", 1)[-1], line), [])

    def shutdown(self):
        self.shut = True


class NoPrepare(Adapter):
    def prepare_argv(self, server_argv, settings):
        return None


def _run(root: Path, limits=None, adapter=None, **server):
    FakeServer.instances.clear()
    factory = lambda **kw: FakeServer(root, **server, **kw)  # noqa: E731
    return index_workspace(str(root), adapter or NoPrepare(), ["php", "phpactor", "language-server"], "php",
                           limits or IndexLimits(), client_factory=factory)


def _default_refs(root: Path):
    controller = (root / "app/Http/GreetController.php").as_uri()
    action = (root / "app/Actions/GreetAction.php").as_uri()
    return {
        # GreetAction::execute (0-based line 6): one real call; one hit on `OtherAction` (deep search
        # returning another symbol); one location outside the revision.
        ("GreetAction.php", 6): [{"uri": controller, "range": _rng(12, 24, 31)},
                                 {"uri": controller, "range": _rng(12, 39, 50)},
                                 {"uri": "file:///elsewhere/Lib.php", "range": _rng(1, 0, 7)}],
        # GreetAction::format: called from execute.
        ("GreetAction.php", 11): [{"uri": action, "range": _rng(8, 22, 28)}],
        # GreetAction class: the `use` import (no enclosing method) and the type hint in show().
        ("GreetAction.php", 4): [{"uri": controller, "range": _rng(4, 16, 27)},
                                 {"uri": controller, "range": _rng(10, 25, 36)}],
    }


def test_definitions_are_qualified_with_namespace_and_origin(tmp_path):
    _write_project(tmp_path)
    result = _run(tmp_path, references=_default_refs(tmp_path))
    defs = {d["fqn"]: d for d in result["definitions"]}
    assert result["status"] == "complete"
    assert defs["App\\Actions\\GreetAction::execute"] == {
        "kind": "method", "name": "execute", "fqn": "App\\Actions\\GreetAction::execute",
        "owner": "App\\Actions\\GreetAction", "file": "app/Actions/GreetAction.php",
        "line": 7, "end_line": 10, "origin": "lsp"}
    assert defs["App\\Http\\Responds"]["kind"] == "trait"  # declared as trait, reported as class


def test_references_become_call_edges_from_enclosing_method(tmp_path):
    _write_project(tmp_path)
    result = _run(tmp_path, references=_default_refs(tmp_path))
    edges = {(r["scope"], r["kind"], r["to_fqn"], r["to_member"], r["line"]) for r in result["references"]}
    assert ("App\\Http\\GreetController::show", "method_call", "App\\Actions\\GreetAction", "execute", 13) in edges
    assert ("App\\Actions\\GreetAction::execute", "method_call", "App\\Actions\\GreetAction", "format", 9) in edges
    assert ("App\\Http\\GreetController::show", "class_reference", "App\\Actions\\GreetAction", None, 11) in edges
    assert all(r["origin"] == "lsp" for r in result["references"])


def test_use_sites_that_name_another_symbol_are_dropped(tmp_path):
    _write_project(tmp_path)
    result = _run(tmp_path, references=_default_refs(tmp_path))
    lines = [r["line"] for r in result["references"] if r["to_member"] == "execute"]
    assert lines == [13]
    assert result["stats"]["name_mismatch"] == 1


def test_locations_outside_root_and_outside_methods_are_dropped(tmp_path):
    _write_project(tmp_path)
    result = _run(tmp_path, references=_default_refs(tmp_path))
    assert result["stats"]["outside_root"] == 1
    assert result["stats"]["unscoped"] == 1  # the `use` import line
    assert not any(r["line"] == 5 for r in result["references"])


def test_vendor_is_never_read(tmp_path):
    _write_project(tmp_path)
    files, truncated = source_files(tmp_path, [".php"], 100)
    assert not truncated
    assert [f.relative_to(tmp_path).as_posix() for f in files] == [
        "app/Actions/GreetAction.php", "app/Http/GreetController.php", "app/Http/Responds.php"]


def test_documents_are_open_during_requests_and_closed_after(tmp_path):
    _write_project(tmp_path)
    _run(tmp_path, references=_default_refs(tmp_path))
    server = FakeServer.instances[-1]
    assert server.open_now == set()
    assert server.shut


def test_skip_reference_names_are_not_requested(tmp_path):
    _write_project(tmp_path)
    result = _run(tmp_path, IndexLimits(skip_reference_names=frozenset({"execute"})),
                  references=_default_refs(tmp_path))
    assert ("GreetAction.php", 6) not in FakeServer.instances[-1].ref_calls
    assert result["stats"]["skipped_by_name"] == 1
    assert result["status"] == "complete"


def test_request_timeout_marks_coverage_partial(tmp_path):
    _write_project(tmp_path)
    refs = _default_refs(tmp_path)
    refs[("GreetAction.php", 11)] = LspTimeoutError("slow")
    result = _run(tmp_path, references=refs)
    assert result["status"] == "partial"
    assert "request_timeout" in result["limits_reached"]
    assert result["stats"]["request_timeouts"] == 1
    assert any(r["to_member"] == "execute" for r in result["references"])  # other edges kept


def test_reference_limit_marks_coverage_partial(tmp_path):
    _write_project(tmp_path)
    result = _run(tmp_path, IndexLimits(max_references=1), references=_default_refs(tmp_path))
    assert result["status"] == "partial"
    assert "max_references" in result["limits_reached"]
    assert len(result["references"]) == 1


def test_run_timeout_marks_coverage_partial(tmp_path):
    _write_project(tmp_path)
    result = _run(tmp_path, IndexLimits(run_timeout=0), references=_default_refs(tmp_path))
    assert result["status"] == "partial"
    assert "run_timeout" in result["limits_reached"]


def test_implementation_becomes_relation(tmp_path):
    _write_project(tmp_path)
    controller = (tmp_path / "app/Http/GreetController.php").as_uri()
    result = _run(tmp_path, references=_default_refs(tmp_path),
                  implementations={("Responds.php", 4): [{"uri": controller, "range": _rng(6, 6, 21)}]})
    defs = {d["fqn"]: d for d in result["definitions"]}
    assert defs["App\\Http\\GreetController"]["relations"] == [
        {"kind": "uses_trait", "target": "App\\Http\\Responds"}]


def test_server_that_cannot_start_reports_unavailable(tmp_path):
    _write_project(tmp_path)
    result = _run(tmp_path, fail_start=True)
    assert result["status"] == "unavailable"
    assert "could not start" in result["reason"]
    assert result["definitions"] == [] and result["references"] == []


def test_server_without_references_reports_unavailable(tmp_path):
    _write_project(tmp_path)
    result = _run(tmp_path, capabilities={"documentSymbolProvider": True})
    assert result["status"] == "unavailable"
    assert "references" in result["reason"]


def test_failed_prepare_step_reports_unavailable(tmp_path):
    _write_project(tmp_path)

    class FailingPrepare(Adapter):
        def prepare_argv(self, server_argv, settings):
            return ["false"]

    result = _run(tmp_path, adapter=FailingPrepare())
    assert result["status"] == "unavailable"
    assert result["reason"].startswith("prepare step exited")


def test_server_gets_isolated_env_and_index_settings(tmp_path, monkeypatch):
    _write_project(tmp_path)
    monkeypatch.setenv("SECRET_TOKEN", "x")
    _run(tmp_path, references=_default_refs(tmp_path))
    kwargs = FakeServer.instances[-1].kwargs
    env = kwargs["env"]
    assert "SECRET_TOKEN" not in env
    assert env["HOME"] != os.environ.get("HOME")
    assert env["XDG_DATA_HOME"].startswith(env["HOME"])  # phpactor trust list lives here: nothing trusted
    assert kwargs["initialization_options"]["composer.enable"] is False
    assert kwargs["root_uri"] == tmp_path.resolve().as_uri()
    assert not os.path.exists(env["HOME"])  # removed after the run


def test_isolated_env_creates_xdg_dirs(tmp_path):
    env = isolated_env(str(tmp_path))
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        assert Path(env[var]).is_dir()
    assert set(env) == {"PATH", "HOME", "LANG", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"}


def test_missing_root_reports_unavailable(tmp_path):
    result = index_workspace(str(tmp_path / "nope"), NoPrepare(), ["php"], "php")
    assert result["status"] == "unavailable"


def test_property_uses_become_property_fetch_edges_that_link_to_property_nodes():
    from inspection.service import InspectionService
    from lsp.indexer import _Symbol, _names_target, _reference

    prop = _Symbol(kind="property", name="$items", fqn="App\\Cart::$items", owner="App\\Cart",
                   file="app/Cart.php", line=5, character=14, start=5, end=5)
    scope = _Symbol(kind="method", name="count", fqn="App\\Cart::count", owner="App\\Cart",
                    file="app/Cart.php", line=7, character=20, start=7, end=10)
    assert _names_target("items", prop)       # $this->items
    assert _names_target("$items", prop)      # self::$items
    assert not _names_target("total", prop)
    ref = _reference(prop, scope, "app/Cart.php", 9)
    assert ref["kind"] == "property_fetch" and ref["to_member"] == "$items"

    class CountingGraph:
        def upsert_nodes(self, rows): return len(rows)
        def upsert_declaration_relations(self, rows): return len(rows)
        def upsert_references(self, rows):
            self.rows = rows
            return len(rows)

    graph = CountingGraph()
    defs = [{"kind": "class", "name": "Cart", "fqn": "App\\Cart", "file": "app/Cart.php", "line": 3},
            {"kind": "property", "name": "$items", "fqn": "App\\Cart::$items", "owner": "App\\Cart",
             "file": "app/Cart.php", "line": 5},
            {"kind": "method", "name": "count", "fqn": "App\\Cart::count", "owner": "App\\Cart",
             "file": "app/Cart.php", "line": 7}]
    out = InspectionService(graph=graph).index("/unused", "proptest", exported={"definitions": defs, "references": [ref]})
    assert out["links"] == 1 and out["unlinked"] == 0
    from inspection.graph import node_key
    assert graph.rows[0]["target_id"] == node_key("property", "App\\Cart::$items", "$items")
