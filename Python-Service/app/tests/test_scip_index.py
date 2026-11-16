"""SCIP output -> symbol-graph export (navigation plan tasks 22-23)."""

import pytest

from code_quality import scip_index
from code_quality.scip_index import convert, symbol_identity

TS = "scip-typescript npm shop 1.0.0 "
PY = "scip-python python shop 0 "


@pytest.mark.parametrize("symbol, expected", [
    (TS + "src/`auth.ts`/Server#", {"kind": "class", "fqn": "src\\auth\\Server", "name": "Server"}),
    (TS + "src/`auth.ts`/Server#login().",
     {"kind": "method", "fqn": "src\\auth\\Server::login", "name": "login", "owner": "src\\auth\\Server"}),
    (TS + "src/`util.ts`/hash().", {"kind": "function", "fqn": "src\\util\\hash", "name": "src\\util\\hash"}),
    (TS + "src/`auth.ts`/Server#locked.",
     {"kind": "property", "fqn": "src\\auth\\Server::locked", "name": "locked", "owner": "src\\auth\\Server"}),
    (PY + "`auth.login`/Server#login().",
     {"kind": "method", "fqn": "auth\\login\\Server::login", "name": "login", "owner": "auth\\login\\Server"}),
    (PY + "`auth.login`/check_password().", {"kind": "function", "fqn": "auth\\login\\check_password",
                                             "name": "auth\\login\\check_password"}),
    (TS + "src/`auth.ts`/Server#login(+1).",
     {"kind": "method", "fqn": "src\\auth\\Server::login", "name": "login", "owner": "src\\auth\\Server"}),
])
def test_symbols_map_to_graph_names(symbol, expected):
    assert symbol_identity(symbol) == expected


@pytest.mark.parametrize("symbol", ["local 3", "", TS + "src/`auth.ts`/", TS + "src/`auth.ts`/LIMIT."])
def test_locals_modules_and_module_variables_are_not_definitions(symbol):
    assert symbol_identity(symbol) is None


def test_convert_links_references_to_their_enclosing_definition():
    server, login, refuse, hash_ = (TS + "src/`auth.ts`/Server#", TS + "src/`auth.ts`/Server#login().",
                                    TS + "src/`auth.ts`/Server#refuse().", TS + "src/`util.ts`/hash().")
    printed = {"documents": [
        {"relativePath": "src/auth.ts", "occurrences": [
            {"range": [2, 13, 19], "symbol": server, "symbolRoles": 1, "enclosingRange": [2, 0, 20, 1]},
            {"range": [4, 2, 7], "symbol": login, "symbolRoles": 1, "enclosingRange": [4, 2, 10, 3]},
            {"range": [6, 13, 19], "symbol": refuse, "symbolRoles": 0},
            {"range": [8, 10, 14], "symbol": hash_, "symbolRoles": 0},
            {"range": [9, 4, 9], "symbol": "local 1", "symbolRoles": 0},
            {"range": [12, 2, 8], "symbol": refuse, "symbolRoles": 1, "enclosingRange": [12, 2, 14, 3]},
            {"range": [22, 0, 6], "symbol": server, "symbolRoles": 0},  # module level: no scope
        ]},
        {"relative_path": "src/util.ts", "occurrences": [
            {"range": [0, 16, 20], "symbol": hash_, "symbol_roles": 1, "enclosing_range": [0, 0, 3, 1]},
        ]},
    ]}
    out = convert(printed)

    by_fqn = {d["fqn"]: d for d in out["definitions"]}
    assert set(by_fqn) == {"src\\auth\\Server", "src\\auth\\Server::login", "src\\auth\\Server::refuse", "src\\util\\hash"}
    assert (by_fqn["src\\auth\\Server::login"]["line"], by_fqn["src\\auth\\Server::login"]["end_line"]) == (5, 11)
    assert all(d["origin"] == "scip" for d in out["definitions"])
    refs = {(r["scope"], r["kind"], r["to_fqn"], r["to_member"], r["line"]) for r in out["references"]}
    assert refs == {("src\\auth\\Server::login", "method_call", "src\\auth\\Server", "refuse", 7),
                    ("src\\auth\\Server::login", "function_call", "src\\util\\hash", None, 9)}


def test_limits_mark_the_export_truncated(monkeypatch):
    monkeypatch.setattr(scip_index, "MAX_DEFINITIONS", 1)
    printed = {"documents": [{"relativePath": "a.ts", "occurrences": [
        {"range": [0, 0, 1], "symbol": TS + "`a.ts`/f().", "symbolRoles": 1},
        {"range": [1, 0, 1], "symbol": TS + "`a.ts`/g().", "symbolRoles": 1}]}]}
    out = convert(printed)
    assert len(out["definitions"]) == 1 and out["truncated"] is True


def test_unsupported_languages_and_missing_indexers_are_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(scip_index.shutil, "which", lambda name: None)
    out = scip_index.index_source(str(tmp_path), ["python", "php", "go"])
    assert out["languages"]["python"] == {"status": "unavailable", "reason": "scip-python is not installed."}
    assert out["languages"]["php"]["reason"].startswith("No SCIP indexer is supported")
    assert out["definitions"] == [] and out["references"] == []


def test_lookup_prefers_scip_rows_and_falls_back_to_parser_rows():
    from agent_tools.navigation import _prefer_precise

    rows = [{"path": "a.ts", "line": 3, "symbol": "A::f", "origin": "tree-sitter"},
            {"path": "a.ts", "line": 3, "symbol": "A::f", "origin": "scip"},
            {"path": "a.ts", "line": 9, "symbol": "A::g", "origin": "tree-sitter"}]
    assert sorted((r["line"], r["origin"]) for r in _prefer_precise(rows)) == [(3, "scip"), (9, "tree-sitter")]


def test_worker_scip_output_is_validated_as_untrusted(monkeypatch):
    from code_quality import remote_worker

    def fake_request(target, method, path, payload, timeout, max_bytes):
        return {"job_id": payload["job_id"], "revision_id": payload["revision_id"],
                "bundle_sha256": payload["bundle_sha256"], "analyzers": {"scip": {
                    "definitions": [{"kind": "method", "fqn": "src\\\\a\\\\A::f", "name": "f", "owner": "src\\\\a\\\\A",
                                     "file": "src/a.ts", "line": 1, "end_line": 3, "extra": "dropped"},
                                    {"kind": "method", "fqn": "x", "name": "x", "file": "/etc/passwd", "line": 1},
                                    {"kind": "shell", "fqn": "y", "name": "y", "file": "src/a.ts", "line": 1}],
                    "references": [{"scope": "src\\\\a\\\\A::f", "kind": "function_call", "to_fqn": "src\\\\b\\\\g",
                                    "file": "src/a.ts", "line": 2, "origin": "tree-sitter"},
                                   {"scope": "s", "kind": "method_call", "to_fqn": 7, "file": "src/a.ts", "line": 2}],
                    "languages": {"typescript": {"status": "complete"}}}}}

    monkeypatch.setattr(remote_worker, "_request", fake_request)
    monkeypatch.setattr(remote_worker, "_filtered_source_bundle", lambda data, files: (b"zip", {"src/a.ts": 3}))
    out = remote_worker.run_scip_job({"source_transfer_consent": True}, b"zip", ["src/a.ts"], {"typescript": 1},
                                     "rev_" + "0" * 32, None)

    assert [d["fqn"] for d in out["definitions"]] == ["src\\\\a\\\\A::f"] and "extra" not in out["definitions"][0]
    assert len(out["references"]) == 1 and out["references"][0]["origin"] == "scip"
    with pytest.raises(remote_worker.RemoteWorkerError):
        remote_worker.run_scip_job({}, b"zip", ["src/a.ts"], {"typescript": 1}, "rev_" + "0" * 32, None)


def test_references_to_library_symbols_are_dropped_and_repeats_collapse():
    f, g = TS + "`a.ts`/f().", TS + "`a.ts`/g()."
    printed = {"documents": [{"relativePath": "a.ts", "occurrences": [
        {"range": [0, 9, 10], "symbol": f, "symbolRoles": 1, "enclosingRange": [0, 0, 5, 1]},
        {"range": [1, 2, 3], "symbol": g, "symbolRoles": 0},
        {"range": [1, 8, 9], "symbol": g, "symbolRoles": 0},
        {"range": [2, 2, 5], "symbol": "scip-typescript npm typescript 5.0.0 lib/`lib.es5.d.ts`/Map#", "symbolRoles": 0},
        {"range": [6, 9, 10], "symbol": g, "symbolRoles": 1, "enclosingRange": [6, 0, 7, 1]},
    ]}]}
    refs = convert(printed)["references"]
    assert [(r["scope"], r["to_fqn"], r["line"]) for r in refs] == [("a\\f", "a\\g", 2)]


def test_short_and_partial_function_names_resolve_by_full_name_suffix():
    from inspection.service import InspectionService

    class Query:
        def resolve_by_fqn(self, collection, fqn):
            return []

        def resolve_by_name(self, collection, name):
            return []

        def resolve_function_by_suffix(self, collection, name):
            rows = [{"fqn": "agent_tools\\source\\resolve_source", "kind": "function"},
                    {"fqn": "other\\resolve_source", "kind": "function"}]
            return rows if name == "resolve_source" else []

    service = InspectionService.__new__(InspectionService)
    service._query = Query()
    assert len(service.resolve_symbol("c", "resolve_source")) == 2
    assert [r["fqn"] for r in service.resolve_symbol("c", "source\\resolve_source")] == ["agent_tools\\source\\resolve_source"]
