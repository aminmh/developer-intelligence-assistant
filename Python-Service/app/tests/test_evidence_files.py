"""Non-source evidence inventory and reader tools (gap-closing plan, tasks 13-14)."""

import io
import json
import zipfile

import pytest

from agent_tools.executor import execute_call
from agent_tools.limits import ToolLimits
from agent_tools.registry import ToolRegistry
from agent_tools.types import ToolContext
from code_quality import evidence_files


@pytest.mark.parametrize("path, kind", [
    (".github/workflows/ci.yml", "ci"), (".gitlab-ci.yml", "ci"), ("Jenkinsfile", "ci"),
    ("services/api/.gitlab/ci/test.yml", "ci"),
    ("Dockerfile", "container"), ("docker/php/Dockerfile.prod", "container"), ("compose.yaml", "container"),
    ("docker-compose.override.yml", "container"), (".dockerignore", "container"),
    ("composer.json", "manifest"), ("composer.lock", "manifest"), ("web/package-lock.json", "manifest"),
    ("requirements-dev.txt", "manifest"), ("go.mod", "manifest"), (".nvmrc", "manifest"), ("App.csproj", "manifest"),
    ("Makefile", "build"),
    ("phpunit.xml.dist", "config"), ("phpstan.neon", "config"), ("tsconfig.app.json", "config"),
    ("config/packages/security.yaml", "config"), (".env.example", "config"), ("deploy/nginx/site.conf", "config"),
    ("README.md", "docs"), ("docs/setup.md", "docs"), ("CLAUDE.md", "docs"), ("AGENTS.md", "docs"),
    (".archcore/decisions/use-x.adr.md", "docs"), ("LICENSE", "docs"), ("src/Module/README.md", "docs"),
    ("translations/messages.nl_NL.yml", "translations"), ("lang/en/auth.php", "translations"),
    ("src/locales/de.json", "translations"), ("locale/fr/LC_MESSAGES/app.po", "translations"),
    ("templates/base.html.twig", "templates"), ("resources/views/home.blade.php", "templates"),
    ("public/index.html", "templates"),
    (".env", "secret"), (".env.local", "secret"), ("auth.json", "secret"), ("certs/site/privkey.pem", "secret"),
    ("src/Controller/HomeController.php", None), ("src/app.ts", None), ("notes/todo.txt", None),
    ("public/logo.png", None), ("src/data.json", None),
])
def test_classify(path, kind):
    assert evidence_files.classify(path) == kind


def _zip(files: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_archive_inventory_lists_names_and_sizes_with_source_rules(monkeypatch):
    import code_quality.service as service

    monkeypatch.setattr(service, "MAX_FILE_BYTES", 10)
    archive = _zip({
        "shop/README.md": "# Shop\n", "shop/.gitlab-ci.yml": "test:\n", "shop/composer.lock": "x" * 50,
        "shop/vendor/acme/README.md": "# vendored\n", "shop/legacy/docs/old.md": "# old\n",
        "shop/.env": "DB_PASSWORD=never-read\n", "shop/src/Kernel.php": "<?php\n",
    })
    inventory = evidence_files.inventory_archive(archive, ["shop/legacy"])

    rows = {row["path"]: row for row in inventory["files"]}
    assert set(rows) == {"shop/README.md", "shop/.gitlab-ci.yml", "shop/composer.lock", "shop/.env"}  # wrapper kept
    assert inventory["by_kind"] == {"ci": 1, "manifest": 1, "docs": 1, "secret": 1}
    assert inventory["excluded_files"] == 2  # vendor and the project's exclusion pattern
    assert rows["shop/.env"]["readable"] is False and "name only" in rows["shop/.env"]["reason"]
    assert rows["shop/composer.lock"]["readable"] is False and rows["shop/composer.lock"]["bytes"] == 50
    assert [row["kind"] for row in inventory["files"]] == ["ci", "manifest", "docs", "secret"]  # ordered by kind
    assert "never-read" not in json.dumps(inventory)


def test_report_summary_is_bounded(monkeypatch):
    monkeypatch.setattr(evidence_files, "MAX_LISTED", 40)
    monkeypatch.setattr(evidence_files, "MAX_REPORTED", 30)
    names = {f"docs/page{i:03}.md": "x" for i in range(60)} | {f"translations/m{i}.en.yml": "a: b" for i in range(5)}
    inventory = evidence_files.inventory_archive(_zip(names))
    assert len(inventory["files"]) == 40 and inventory["unlisted_files"] == 25
    assert inventory["by_kind"] == {"docs": 60, "translations": 5}
    summary = evidence_files.report_summary(inventory)
    assert summary["reported_files"] == len(summary["files"]) <= 30 and summary["listed_files"] == 40


@pytest.fixture
def project(tmp_path):
    files = {
        "README.md": "# Shop\nRun `make test`.\n", "Dockerfile": "FROM php:8.4\n", "composer.json": "{}\n",
        "translations/messages.en.yml": "hello: Hello\n", "src/Kernel.php": "<?php\nclass Kernel {}\n",
        "vendor/acme/README.md": "# vendored\n", ".env": "DB_PASSWORD=never-read\n",
        "legacy/docs/old.md": "# old\n",
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    return tmp_path


def _run(name, args, project):
    registry = ToolRegistry(managed_loader=lambda: [])
    definition, validated = registry.resolve_call("search", name, args)
    return execute_call(definition, validated, ToolContext(workflow="search", workspace_path=str(project)),
                        ToolLimits(max_output_bytes=200_000))


def test_evidence_files_tool_lists_kinds_and_secrets_by_name(project):
    result = _run("evidence_files", {}, project)
    assert result["ok"] is True, result
    out = json.loads(result["output"])
    assert [(row["path"], row["kind"], row["readable"]) for row in out["items"]] == [
        ("Dockerfile", "container", True), ("composer.json", "manifest", True), ("README.md", "docs", True),
        ("legacy/docs/old.md", "docs", True), ("translations/messages.en.yml", "translations", True), (".env", "secret", False)]
    docs = json.loads(_run("evidence_files", {"kind": "docs", "path_glob": "legacy/*"}, project)["output"])
    assert [row["path"] for row in docs["items"]] == ["legacy/docs/old.md"]
    assert "never-read" not in result["output"]


def test_evidence_read_reads_evidence_only(project):
    out = json.loads(_run("evidence_read", {"path": "README.md"}, project)["output"])
    assert out["kind"] == "docs" and out["content"].startswith("1: # Shop")
    nested = json.loads(_run("evidence_read", {"path": "legacy/docs/old.md"}, project)["output"])
    assert nested["kind"] == "docs"  # docs/ is left out of source scoring but readable as evidence
    assert _run("code_read", {"path": "legacy/docs/old.md"}, project)["error"]["code"] == "excluded_path"
    for path, code in [("src/Kernel.php", "not_evidence"), (".env", "excluded_path"),
                       ("vendor/acme/README.md", "excluded_path"), ("../outside.md", "invalid_path")]:
        result = _run("evidence_read", {"path": path}, project)
        assert result["ok"] is False and result["error"]["code"] == code, (path, result)
        assert "never-read" not in json.dumps(result)


def test_the_inventory_never_names_a_framework():
    import inspect

    source = inspect.getsource(evidence_files).lower()
    assert not any(name in source for name in ("laravel", "symfony", "angular", "nuxt", "next.config", "svelte", "django"))
