"""Tests for the read-only code navigation tools against a small fixture project."""

import io
import os
import zipfile

import pytest

import agent_tools.source as source_module
from agent_tools.executor import execute_call
from agent_tools.limits import ToolLimits
from agent_tools.navigation import NAVIGATION_TOOLS
from agent_tools.registry import ToolRegistry
from agent_tools.source import is_secret_path, resolve_source
from agent_tools.types import ToolCallRejected, ToolContext

TOOLS = {t.name: t for t in NAVIGATION_TOOLS}

USER_PHP = """<?php
namespace App\\Models;

class User extends Model
{
    public function invoices(): HasMany
    {
        return $this->hasMany(Invoice::class)->where('paid', false);
    }

    public function sendInvoiceMail(Invoice $invoice): void
    {
        InvoiceMailer::send($invoice);
    }
}
"""


@pytest.fixture
def project(tmp_path):
    files = {
        "app/Models/User.php": USER_PHP,
        "app/Mail/InvoiceMailer.php": "<?php\nclass InvoiceMailer { public static function send($i) {} }\n",
        "routes/web.php": "<?php\nRoute::get('/invoices', [InvoiceController::class, 'index']);\n",
        "vendor/laravel/framework/Mailer.php": "<?php\nclass InvoiceMailer {}\n",
        ".env": "DB_PASSWORD=InvoiceMailer-secret\n",
        "config/keys/private.pem": "InvoiceMailer\n",
        "notes.txt": "\n".join(f"line {i}" for i in range(1, 501)),
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (tmp_path / "app/logo.bin").write_bytes(b"\x89PNG\0\0\0")
    return tmp_path


def _run(name, args, project, **ctx):
    context = ToolContext(workflow="search", workspace_path=str(project), **ctx)
    registry = ToolRegistry(managed_loader=lambda: [])
    definition, validated = registry.resolve_call("search", name, args)
    return execute_call(definition, validated, context, ToolLimits(max_output_bytes=200_000))


def _output(result):
    import json

    assert result["ok"] is True, result
    return json.loads(result["output"])


def test_all_navigation_tools_are_read_only_builtins():
    assert set(TOOLS) == {"code_text_search", "code_structural_search", "code_read", "code_outline",
                          "code_symbol_lookup", "code_stats"}
    assert {t.effect for t in NAVIGATION_TOOLS} == {"read_only"}


def test_text_search_finds_sorted_matches_and_skips_vendor_and_secrets(project):
    out = _output(_run("code_text_search", {"pattern": "InvoiceMailer"}, project))

    paths = [i["path"] for i in out["items"]]
    assert paths == sorted(paths)
    assert "app/Mail/InvoiceMailer.php" in paths and "app/Models/User.php" in paths
    assert not any(p.startswith("vendor/") or p == ".env" or p.endswith(".pem") for p in paths)
    assert out["source"] == "workspace" and out["truncated"] is False


def test_text_search_reports_truncation(project):
    out = _output(_run("code_text_search", {"pattern": "line", "max_results": 3}, project))

    assert len(out["items"]) == 3 and out["truncated"] is True


def test_text_search_regex_and_invalid_regex(project):
    out = _output(_run("code_text_search", {"pattern": r"function \w+\(", "fixed_string": False}, project))
    assert {i["line"] for i in out["items"] if i["path"] == "app/Models/User.php"} == {6, 11}

    bad = _run("code_text_search", {"pattern": "(unclosed", "fixed_string": False}, project)
    assert bad["ok"] is False and bad["error"]["code"] == "tool_failed"


def test_pattern_starting_with_dash_is_not_an_option(project):
    out = _output(_run("code_text_search", {"pattern": "--files"}, project))

    assert out["items"] == []


def test_structural_search_matches_php_calls(project):
    out = _output(_run("code_structural_search", {"pattern": "$A->where($$$)", "language": "php"}, project))

    assert [(i["path"], i["line"]) for i in out["items"]] == [("app/Models/User.php", 8)]


def test_read_returns_numbered_window(project):
    out = _output(_run("code_read", {"path": "notes.txt", "start_line": 10, "end_line": 12}, project))
    assert out["content"] == "10: line 10\n11: line 11\n12: line 12"
    assert out["total_lines"] == 500 and out["truncated"] is True

    default = _output(_run("code_read", {"path": "./notes.txt"}, project))
    assert default["end_line"] == 120

    capped = _output(_run("code_read", {"path": "notes.txt", "start_line": 1, "end_line": 900}, project))
    assert capped["end_line"] == 200


@pytest.mark.parametrize("path,code", [
    ("../etc/passwd", "invalid_path"), ("/etc/passwd", "invalid_path"), (".env", "excluded_path"),
    ("config/keys/private.pem", "excluded_path"), ("vendor/laravel/framework/Mailer.php", "excluded_path"),
    ("app/missing.php", "not_found"), ("app/logo.bin", "binary_file"),
])
def test_read_refuses_unsafe_paths(project, path, code):
    result = _run("code_read", {"path": path}, project)

    assert result["ok"] is False and result["error"]["code"] == code


def test_read_refuses_symlinks_out_of_the_project(project, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.php"
    outside.write_text("<?php // secret")
    os.symlink(outside, project / "app" / "link.php")

    result = _run("code_read", {"path": "app/link.php"}, project)

    assert result["ok"] is False and result["error"]["code"] == "invalid_path"


def test_outline_lists_classes_and_methods(project):
    out = _output(_run("code_outline", {"path": "app/Models/User.php"}, project))

    assert out["supported"] is True and out["language"] == "php"
    summary = [(i["kind"], i["name"], i["line"], i["depth"]) for i in out["items"]]
    assert ("Class", "User", 4, 0) in summary
    assert ("Method", "invoices", 6, 1) in summary and ("Method", "sendInvoiceMail", 11, 1) in summary
    assert any("HasMany" in i["signature"] for i in out["items"])


def test_outline_reports_unsupported_files(project):
    out = _output(_run("code_outline", {"path": "notes.txt"}, project))

    assert out["supported"] is False


def test_stats_counts_php_without_vendor(project):
    out = _output(_run("code_stats", {}, project))

    php = next(r for r in out["languages"] if r["language"] == "PHP")
    assert php["files"] == 3


def test_tools_need_a_source(project):
    context = ToolContext(workflow="search")
    result = execute_call(TOOLS["code_text_search"], {"pattern": "x"}, context, ToolLimits())

    assert result["ok"] is False and result["error"]["code"] == "no_source"


class _FakeInspection:
    def __init__(self, root):
        self.root = root

    def list_projects(self):
        return [{"collection": "sym_armaiti_api_develop", "project_id": None, "commit_sha": None}]

    def resolve_symbol(self, collection, symbol):
        return [{"fqn": "App\\Models\\User", "kind": "class", "file": f"{self.root}/app/Models/User.php", "line": 4}]

    def find_references(self, collection, fqn, name=None):
        return [{"ref": {"file": "/app/workspaces/abc123/app/Http/Controllers/UserController.php", "line": 20}},
                {"ref": {"file": f"{self.root}/vendor/x/Y.php", "line": 1}}]

    def call_hierarchy(self, collection, fqn, name=None):
        return {"callers": [{"file": f"{self.root}/routes/web.php", "line": 2}], "callees": []}


def test_symbol_lookup_relativizes_paths_and_skips_excluded(monkeypatch, project):
    import inspection.service as inspection

    monkeypatch.setattr(inspection, "inspection_service", lambda: _FakeInspection(project))
    kwargs = {"filename": "armaiti-api-develop.zip"}

    definition = _output(_run("code_symbol_lookup", {"symbol": "User"}, project, **kwargs))
    assert definition["items"] == [{"path": "app/Models/User.php", "line": 4, "kind": "class",
                                    "symbol": "App\\Models\\User", "origin": "php-parser"}]

    refs = _output(_run("code_symbol_lookup", {"symbol": "User", "relation": "references"}, project, **kwargs))
    assert [i["path"] for i in refs["items"]] == ["app/Http/Controllers/UserController.php"]

    callers = _output(_run("code_symbol_lookup", {"symbol": "User", "relation": "callers"}, project, **kwargs))
    assert callers["items"][0]["path"] == "routes/web.php"


def test_symbol_lookup_reports_missing_graph(monkeypatch, project):
    import inspection.service as inspection

    monkeypatch.setattr(inspection, "inspection_service", lambda: _FakeInspection(project))
    out = _output(_run("code_symbol_lookup", {"symbol": "User"}, project, filename="other.zip"))
    assert out["coverage"] == "unavailable"

    def broken():
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(inspection, "inspection_service", broken)
    out = _output(_run("code_symbol_lookup", {"symbol": "User"}, project))
    assert out["coverage"] == "unavailable" and "neo4j down" in out["detail"]


@pytest.mark.parametrize("path,secret", [
    (".env", True), (".env.production", True), ("config/.env", True), (".env.example", False),
    ("storage/oauth-private.key", True), ("auth.json", True), (".git/config", True),
    ("app/Models/User.php", False), ("docs/environment.md", False),
    ("storage/firebase-service-account.json", True), ("config/google-credentials.json", True),
    ("deploy/app-secrets.yaml", True), ("app/Services/SecretService.php", False), ("config/services.php", False),
])
def test_secret_paths(path, secret):
    assert is_secret_path(path) is secret


def test_revision_source_is_extracted_once_read_only_and_reused(monkeypatch, tmp_path):
    checksum = "a" * 64
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("app/Models/User.php", USER_PHP)
    downloads = []

    class Registry:
        def get_by_revision_id(self, project_id, revision_id):
            return {"state": "ready", "source_archive_sha256": checksum, "revision_id": revision_id,
                    "commit_sha": "c" * 40}

        def archive_key(self, project_id, reference):
            return "archives/key.zip"

    class Storage:
        def get_snapshot_archive(self, key):
            downloads.append(key)
            return buffer.getvalue()

    class Store:
        def exclusions(self, project_id):
            return ["app/Legacy/**"]

    import code_quality.service as cq
    import index.storage as storage_module
    import telemetry.registry as registry_module
    import telemetry.snapshot as snapshot_module

    monkeypatch.setattr(source_module, "CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(registry_module, "default_registry", lambda: Registry())
    monkeypatch.setattr(storage_module, "storage", Storage())
    monkeypatch.setattr(cq, "ReportStore", Store)
    monkeypatch.setattr(snapshot_module, "source_reference", lambda meta: "ref")
    context = ToolContext(workflow="search", project_id="pizza", revision_id="rev_1")

    first = resolve_source(context)
    second = resolve_source(context)

    assert downloads == ["archives/key.zip"]
    assert first.path == second.path == tmp_path / "cache" / checksum
    assert first.origin == "revision" and first.is_excluded("app/Legacy/Old.php")
    file = first.path / "app/Models/User.php"
    assert file.read_text() == USER_PHP
    assert file.stat().st_mode & 0o222 == 0  # no write bits (root ignores them, so check bits)


def test_revision_that_is_not_ready_is_rejected(monkeypatch):
    import telemetry.registry as registry_module

    class Registry:
        def get_by_revision_id(self, project_id, revision_id):
            return {"state": "indexing"}

    monkeypatch.setattr(registry_module, "default_registry", lambda: Registry())

    with pytest.raises(ToolCallRejected) as exc:
        resolve_source(ToolContext(workflow="search", project_id="pizza", revision_id="rev_1"))
    assert exc.value.code == "no_source"


def test_archive_wrapper_folder_becomes_the_project_root(tmp_path):
    project = tmp_path / "my-project"
    (project / "routes").mkdir(parents=True)
    (project / "routes" / "auth.php").write_text("<?php\nRoute::post('login');\n")
    (tmp_path / "__MACOSX").mkdir()

    root = resolve_source(ToolContext(workflow="search", workspace_path=str(tmp_path)))
    out = _output(_run("code_text_search", {"pattern": "login", "path_glob": "routes/*.php"}, tmp_path))

    assert root.path == project and root.wrapper_prefix == "my-project"
    assert [i["path"] for i in out["items"]] == ["routes/auth.php"]
    assert root.resolve_file("my-project/routes/auth.php") == root.resolve_file("routes/auth.php")


def test_overview_lists_layout_without_dependencies_secrets_or_junk(project):
    from agent_tools.overview import build_overview

    (project / "storage").mkdir()
    (project / "storage" / "firebase-service-account.json").write_text("{}")
    (project / "app" / ".DS_Store").write_text("x")
    text = build_overview(resolve_source(ToolContext(workflow="search", workspace_path=str(project))))

    assert text.startswith("Project layout")
    assert "routes/ (1 files): web.php" in text
    assert "app/ (" in text and "Models/ (1)" in text
    assert "vendor" not in text and ".env" not in text and "firebase" not in text and ".DS_Store" not in text
    assert "Languages: PHP 3" in text
    assert len(text) <= 2_400


def test_agent_prompt_includes_the_overview(monkeypatch, project):
    import agent_tools.runtime as runtime

    context = ToolContext(workflow="search", workspace_path=str(project))
    assert runtime._overview_for(context).startswith("Project layout")
    assert runtime._overview_for(ToolContext(workflow="search")) == ""


@pytest.mark.parametrize("layout,expected_prefix", [
    (["wrapper/app/Models/User.php"], "wrapper"),
    (["app/Models/User.php"], ""),
    (["outer/inner/composer.json", "outer/inner/app/x.php"], "outer/inner"),
    (["outer/src/main.py"], "outer"),
    (["a.php", "b/c.php"], ""),
])
def test_wrapper_detection_never_skips_project_folders(tmp_path, layout, expected_prefix):
    from agent_tools.source import project_root

    for rel in layout:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")

    root, prefix = project_root(tmp_path)

    assert prefix == expected_prefix and root == tmp_path / expected_prefix if expected_prefix else root == tmp_path
