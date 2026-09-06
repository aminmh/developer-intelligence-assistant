"""Deterministic offline tests for projects.structure_profile.

Covers the review-flagged guarantees so they cannot regress: bounded reads,
pruned plugin ``Project``s (never vendor/node_modules), the laravel
``runtime_routes=False`` offline contract, comment-safe DB inference, laravel
``.env`` override precedence, case-insensitive skip dirs, line-anchored python
import markers, bounded-depth entrypoint discovery, and truthful ASCII
summaries. No network, no subprocess, no docker: all tmp_path fixtures.
"""

from __future__ import annotations

import json

from agent.core.types import AnalysisResult, AnalysisScope
from projects import structure_profile as sp


def _write(root, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _composer_laravel(version: str = "^11.0") -> str:
    return json.dumps({"require": {"laravel/framework": version}})


class TestBoundedReads:
    def test_read_text_bounded_caps_oversized_sparse_file(self, tmp_path):
        path = tmp_path / "big.txt"
        limit = 4096
        with path.open("wb") as handle:
            handle.seek(limit * 8)
            handle.write(b"tail")
        text = sp._read_text_bounded(path, limit=limit)
        # decoded slice is capped at limit characters and the read never
        # ballooned into the file's full sparse tail.
        assert len(text) <= limit
        assert text == "\x00" * limit

    def test_read_text_bounded_returns_small_files_whole(self, tmp_path):
        path = tmp_path / "small.txt"
        path.write_text("hello world", encoding="utf-8")
        assert sp._read_text_bounded(path, limit=4096) == "hello world"

    def test_read_text_bounded_missing_file_empty(self, tmp_path):
        assert sp._read_text_bounded(tmp_path / "nope.txt") == ""


class TestOfflineRuntimeRoutes:
    def test_build_disables_runtime_routes_for_laravel_plugin(
        self, tmp_path, monkeypatch
    ):
        _write(tmp_path, "composer.json", _composer_laravel())
        _write(tmp_path, "routes/web.php", "<?php Route::get('/', fn () => 'x');")
        captured = {}

        from agent.plugins.frameworks import laravel as laravel_module

        def fake_analyze(self, workspace, *, runtime_routes=True):
            captured["runtime_routes"] = runtime_routes
            return AnalysisResult(
                scope=AnalysisScope(kind="routes"), summary="static", data={}
            )

        monkeypatch.setattr(
            laravel_module.LaravelFrameworkPlugin, "analyze", fake_analyze
        )
        profile = sp.build_structure_profile(tmp_path)
        assert captured.get("runtime_routes") is False
        assert profile["framework"] == "laravel"
        assert profile["structure_source"] == "plugin"

    def test_analyze_default_still_enables_runtime_routes(self, tmp_path, monkeypatch):
        # Default True preserves project_profile/sandbox behaviour.
        from agent.plugins.frameworks import laravel as laravel_module

        _write(tmp_path, "composer.json", _composer_laravel())
        captured = {}

        def fake_analyze(self, workspace, *, runtime_routes=True):
            captured["runtime_routes"] = runtime_routes
            return AnalysisResult(
                scope=AnalysisScope(kind="routes"), summary="static", data={}
            )

        monkeypatch.setattr(
            laravel_module.LaravelFrameworkPlugin, "analyze", fake_analyze
        )
        plugin = laravel_module.LaravelFrameworkPlugin()
        from agent.core.workspace import Workspace

        plugin.analyze(Workspace(tmp_path))
        assert captured["runtime_routes"] is True


class TestPrunedProject:
    def test_project_built_from_pruned_files_excludes_vendor(
        self, tmp_path, monkeypatch
    ):
        _write(tmp_path, "composer.json", _composer_laravel())
        _write(tmp_path, "artisan", "<?php\n")
        _write(tmp_path, "app/Models/User.php", "<?php // model")
        _write(tmp_path, "src/tool.py", "print('x')")
        _write(tmp_path, "vendor/pkg/Package.php", "<?php // vendored")
        _write(tmp_path, "vendor/deep/nested/More.php", "<?php // vendored")

        captured = {}
        from agent.core import registry as registry_module
        from agent.plugins.frameworks import laravel as laravel_module

        original_detect = registry_module.PluginRegistry.detect

        def spy_detect(self, project):
            captured["project"] = project
            return original_detect(self, project)

        monkeypatch.setattr(registry_module.PluginRegistry, "detect", spy_detect)
        monkeypatch.setattr(
            laravel_module.LaravelFrameworkPlugin,
            "analyze",
            lambda self, workspace, *, runtime_routes=True: AnalysisResult(
                scope=AnalysisScope(kind="routes"), summary="static", data={}
            ),
        )

        profile = sp.build_structure_profile(tmp_path)
        assert profile["framework"] == "laravel"
        project = captured["project"]
        assert project is not None
        rels = project.rel_names()
        assert rels
        assert not any("vendor" in r for r in rels)
        assert any(r == "artisan" for r in rels)
        assert any(r == "composer.json" for r in rels)
        assert any(r == "src/tool.py" for r in rels)


class TestDbCommentSafety:
    def test_commented_env_db_connection_ignored(self, tmp_path):
        _write(
            tmp_path,
            ".env",
            "# DB_CONNECTION=postgres\n"
            "# DATABASE_URL=postgres://u:p@h/db\n"
            "APP_NAME=demo\n",
        )
        result = sp.infer_database(tmp_path, "python", None)
        assert result["database"] is None
        assert result["confidence"] == "low"

    def test_slash_slash_comment_env_line_ignored(self, tmp_path):
        _write(
            tmp_path,
            ".env",
            "// DB_CONNECTION=mysql\nAPP_NAME=demo\n",
        )
        result = sp.infer_database(tmp_path, "python", None)
        assert result["database"] is None
        assert result["confidence"] == "low"

    def test_active_database_url_detected_high(self, tmp_path):
        _write(
            tmp_path,
            ".env",
            "DATABASE_URL=postgres://user:pass@db:5432/app\n",
        )
        result = sp.infer_database(tmp_path, "python", None)
        assert result["database"] == "PostgreSQL"
        assert result["confidence"] == "high"
        assert result["evidence"] == ".env"


class TestLaravelEnvOverride:
    def test_env_override_beats_config_database_fallback(self, tmp_path):
        _write(
            tmp_path,
            "config/database.php",
            "<?php\n"
            "return [\n"
            "    'default' => env('DB_CONNECTION', 'sqlite'),\n"
            "    'connections' => [\n"
            "        'sqlite' => ['driver' => 'sqlite'],\n"
            "        'mysql' => ['driver' => 'mysql'],\n"
            "    ],\n"
            "];\n",
        )
        _write(tmp_path, ".env", "DB_CONNECTION=mysql\n")
        result = sp.infer_database(tmp_path, "php", "laravel")
        assert result["database"] == "MySQL"
        assert result["confidence"] == "high"
        assert result["evidence"] == ".env"

    def test_config_default_reported_when_no_env(self, tmp_path):
        _write(
            tmp_path,
            "config/database.php",
            "<?php\n"
            "return [\n"
            "    'default' => env('DB_CONNECTION', 'sqlite'),\n"
            "    'connections' => ['sqlite' => ['driver' => 'sqlite']],\n"
            "];\n",
        )
        result = sp.infer_database(tmp_path, "php", "laravel")
        assert result["database"] == "SQLite"
        assert result["confidence"] == "high"
        assert result["evidence"].endswith("config/database.php (default)")


class TestSkipDirs:
    def test_skip_dirs_match_case_insensitively(self, tmp_path):
        _write(tmp_path, "Vendor/Package.php", "<?php // vendored")
        _write(tmp_path, "src/tool.py", "x = 1")
        _write(tmp_path, "README.md", "# demo")
        census = sp.language_census(tmp_path)
        assert census["total_files"] == 2  # src/tool.py + README.md
        assert census["primary"] == "python"
        rels = [rel for rel, _ in sp._pruned_files(tmp_path)]
        assert not any(rel.lower().startswith("vendor") for rel in rels)


class TestPythonImportMarkers:
    def test_commented_import_django_not_detected(self, tmp_path):
        _write(
            tmp_path,
            "app.py",
            "# import django\n# from django import http\nprint('ok')\n",
        )
        result = sp.detect_framework(tmp_path, "python", [])
        assert result["framework"] is None

    def test_django_core_import_not_detected_as_django(self, tmp_path):
        _write(tmp_path, "app.py", "import django_core\n")
        result = sp.detect_framework(tmp_path, "python", [])
        assert result["framework"] is None

    def test_from_fastapi_import_detected(self, tmp_path):
        _write(tmp_path, "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
        result = sp.detect_framework(tmp_path, "python", [])
        assert result["framework"] == "FastAPI"


class TestEntrypoints:
    def test_conventional_entrypoints_discovered_to_depth_two(self, tmp_path):
        _write(tmp_path, "main.go", "package main\n")
        _write(tmp_path, "src/main/Application.java", "class Application {}\n")
        _write(tmp_path, "cmd/api/server.go", "package main\n")
        arch = sp.architecture_summary(tmp_path, None, None, None, None)
        assert "main.go" in arch["entrypoints"]
        assert "Application.java" in arch["entrypoints"]
        assert "server.go" in arch["entrypoints"]


class TestSummaryAndErrors:
    def test_summary_ascii_unclassified_and_total_counts_all_files(self, tmp_path):
        _write(tmp_path, "app.py", "print('hi')\n")
        _write(tmp_path, "README.md", "# readme\n")
        profile = sp.build_structure_profile(tmp_path)
        assert profile["total_files"] == 2  # includes README.md (non-source)
        assert "\u00b7" not in profile["summary"]
        assert "Unclassified project" in profile["summary"]
        assert profile["error"] is None

    def test_unreadable_root_summary(self, tmp_path):
        profile = sp.build_structure_profile(tmp_path / "does-not-exist")
        assert profile["summary"] == "No readable workspace"
        assert profile["error"] is None

    def test_mid_build_failure_degrades_honestly(self, tmp_path, monkeypatch):
        _write(tmp_path, "app.py", "print('hi')\n")

        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(sp, "_collect", boom)
        profile = sp.build_structure_profile(tmp_path)
        assert profile["summary"].startswith("analysis degraded: ")
        assert profile["error"]
