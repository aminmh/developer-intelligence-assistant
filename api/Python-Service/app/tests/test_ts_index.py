"""Tree-sitter producer for Go, Java, C#, Ruby, and Rust (navigation plan tasks 6–8)."""

from pathlib import Path

import pytest

from projects import ts_index
from telemetry.snapshot import Snapshot, prepare_chunks
from telemetry.symbols import from_chunk


def _project(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def _by_name(result, name, kind=None):
    return next(c for c in result["chunks"] if c["name"] == name and (kind is None or c["type"] == kind))


def _refs(result):
    return {(r["scope"], r["kind"], r["to_fqn"], r["to_member"]) for r in result["symbol_index"]["references"]}


def test_java_methods_calls_and_static_calls_across_files(tmp_path):
    root = _project(tmp_path, {
        "src/com/acme/auth/Login.java": (
            "package com.acme.auth;\n"
            "/** Logs users in. */\n"
            "public class Login extends Base {\n"
            "  public Login() { check(); }\n"
            "  public void run(int a) { check(); this.check(); Hasher.hash(a); unknown.call(); }\n"
            "  public void run(String s) { check(); }\n"
            "  private void check() {}\n"
            "}\n"),
        "src/com/acme/util/Hasher.java": (
            "package com.acme.util;\npublic class Hasher { public static int hash(int a) { return a; } }\n"),
        "src/com/acme/auth/Base.java": "package com.acme.auth;\npublic class Base {}\n",
    })
    result = ts_index.produce_chunks(root, "java")

    assert result["error"] is None and result["files"] == 3
    login = _by_name(result, "Login", "class")
    assert login["class_fqn"] == "com\\acme\\auth\\Login" and login["docblock"].startswith("/** Logs")
    run = _by_name(result, "run")
    # Both overloads merge into one chunk with the union of their calls.
    assert run["code"].count("public void run") == 2
    assert set(run["calls"]) == {"com\\acme\\auth\\Login::check", "com\\acme\\util\\Hasher::hash"}
    assert run["unresolved_reference_count"] == 1 and run["origin"] == "tree-sitter"
    assert _by_name(result, "constructor")["calls"] == ["com\\acme\\auth\\Login::check"]
    refs = _refs(result)
    assert ("com\\acme\\auth\\Login::run", "static_call", "com\\acme\\util\\Hasher", "hash") in refs
    assert ("com\\acme\\auth\\Login::run", "method_call", "com\\acme\\auth\\Login", "check") in refs
    login_def = next(d for d in result["symbol_index"]["definitions"] if d["fqn"] == "com\\acme\\auth\\Login")
    assert login_def["relations"] == [{"kind": "extends", "target": "com\\acme\\auth\\Base"}]


def test_go_receivers_package_calls_and_types(tmp_path):
    root = _project(tmp_path, {
        "internal/auth/server.go": (
            "package auth\nimport \"example.com/app/internal/util\"\n"
            "type Server struct{ n int }\n"
            "func (s *Server) Login(u string) error { s.check(); return util.Hash(u) }\n"
            "func (s *Server) check() {}\n"
            "func New() *Server { helper(); return &Server{} }\n"
            "func helper() {}\n"),
        "internal/util/hash.go": "package util\nfunc Hash(s string) error { return nil }\n",
    })
    result = ts_index.produce_chunks(root, "go")

    assert _by_name(result, "Server", "class")["class_fqn"] == "internal\\auth\\Server"
    login = _by_name(result, "Login")
    assert login["type"] == "method" and login["class_fqn"] == "internal\\auth\\Server"
    assert set(login["calls"]) == {"internal\\auth\\Server::check", "internal\\util\\Hash"}
    assert _by_name(result, "New")["calls"] == ["internal\\auth\\helper"]
    assert ("internal\\auth\\New", "function_call", "internal\\auth\\helper", None) in _refs(result)


def test_csharp_file_scoped_namespace_and_member_calls(tmp_path):
    root = _project(tmp_path, {"Auth/Login.cs": (
        "namespace Acme.Auth;\n"
        "public class Login { public void Run() { Check(); this.Check(); Util.Hash(1); } void Check() {} }\n"
        "public static class Util { public static int Hash(int a) => a; }\n")})
    result = ts_index.produce_chunks(root, "csharp")

    run = _by_name(result, "Run")
    assert run["class_fqn"] == "Acme\\Auth\\Login"
    assert set(run["calls"]) == {"Acme\\Auth\\Login::Check", "Acme\\Auth\\Util::Hash"}


def test_ruby_modules_bare_calls_and_unsupported_names(tmp_path):
    root = _project(tmp_path, {"app/login.rb": (
        "module Acme\n  class Login < Base\n    def run(a)\n      check\n      self.check\n    end\n"
        "    def check; end\n    def valid?; true; end\n  end\nend\n")})
    result = ts_index.produce_chunks(root, "ruby")

    run = _by_name(result, "run")
    assert run["class_fqn"] == "Acme\\Login" and run["calls"] == ["Acme\\Login::check"]
    assert not [c for c in result["chunks"] if c["name"] == "valid?"]
    assert {"construct": "unsupported_name", "name": "valid?"}.items() <= result["unsupported_constructs"][0].items()
    assert result["coverage"] == "partial"


def test_rust_impl_blocks_paths_and_trait_signatures(tmp_path):
    root = _project(tmp_path, {
        "src/server.rs": (
            "struct Server { n: i32 }\n"
            "impl Server { fn login(&self) { self.check(); helper(); util::hash(1); } fn check(&self) {} }\n"
            "fn helper() {}\ntrait T { fn go(&self); fn ready(&self) -> bool { true } }\n"),
        "src/util.rs": "pub fn hash(a: i32) -> i32 { a }\n",
    })
    result = ts_index.produce_chunks(root, "rust")

    login = _by_name(result, "login")
    assert login["class_fqn"] == "crate\\server\\Server"
    assert set(login["calls"]) == {"crate\\server\\Server::check", "crate\\server\\helper", "crate\\util\\hash"}
    names = {c["name"] for c in result["chunks"]}
    assert "ready" in names and "go" not in names  # signatures without bodies are not definitions


@pytest.mark.parametrize("language", sorted(ts_index.LANGS))
def test_chunks_have_valid_unique_snapshot_identities(tmp_path, language):
    samples = {
        "go": ("a.go", "package a\nfunc F() {}\nfunc G() { F() }\n"),
        "java": ("A.java", "package p;\nclass A { void f(){} void f(int x){} A(){} A(int y){} }\n"),
        "csharp": ("A.cs", "namespace P { class A { void F(){} void F(int x){} } }\n"),
        "ruby": ("a.rb", "class A\n  def f; end\n  def self.g; f; end\nend\n"),
        "rust": ("src/lib.rs", "struct A;\nimpl A { fn f(&self) {} }\nfn g() {}\n"),
    }
    name, text = samples[language]
    result = ts_index.produce_chunks(_project(tmp_path, {name: text}), language)

    assert result["chunks"]
    for chunk in result["chunks"]:
        from_chunk(chunk)  # raises on an invalid identity
    prepare_chunks(result["chunks"], Snapshot("p", "a" * 40, "b" * 64))  # raises on a duplicate identity


def test_duplicate_definitions_in_other_files_are_reported(tmp_path):
    root = _project(tmp_path, {"a/x_linux.go": "package a\nfunc Open() {}\n",
                               "a/x_darwin.go": "package a\nfunc Open() {}\n"})
    result = ts_index.produce_chunks(root, "go")

    assert len([c for c in result["chunks"] if c["name"] == "Open"]) == 1
    assert any(u["construct"] == "duplicate_symbol" for u in result["unsupported_constructs"])


def test_missing_workspace_and_unknown_language_never_raise(tmp_path):
    assert ts_index.produce_chunks(tmp_path / "missing", "go")["error"].startswith("workspace not found")
    assert ts_index.produce_chunks(tmp_path, "cobol")["error"] == "no tree-sitter indexer for cobol"


def test_file_limit_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(ts_index, "_MAX_FILES", 2)
    root = _project(tmp_path, {f"p/f{i}.go": f"package p\nfunc F{i}() {{}}\n" for i in range(4)})
    result = ts_index.produce_chunks(root, "go")

    assert result["files"] == 2 and result["coverage"] == "partial"
    assert any(u["construct"] == "file_count_limit" for u in result["unsupported_constructs"])
