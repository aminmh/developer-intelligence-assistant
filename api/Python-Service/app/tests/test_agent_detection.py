"""Project detection tests (spec §7) — profile shape, confidence, broken input.

Runs against fixture projects; also covers the capability aggregation and the
profile JSON contract consumed by future endpoints.
"""

from __future__ import annotations

from pathlib import Path

from agent.core.registry import PluginRegistry
from agent.detection import detect_project
from agent.plugins import register_builtins

FIXTURES = Path(__file__).parent / "fixtures"


def profile_of(fixture: str) -> dict:
    return detect_project(FIXTURES / fixture).to_dict()


class TestLaravelDetection:
    def test_detects_php_and_laravel(self):
        profile = detect_project(FIXTURES / "laravel_valid")
        lang_names = [l.name for l in profile.languages]
        fw_names = [f.name for f in profile.frameworks]
        assert "php" in lang_names
        assert "laravel" in fw_names

    def test_laravel_confidence_and_version(self):
        profile = detect_project(FIXTURES / "laravel_valid")
        laravel = next(f for f in profile.frameworks if f.name == "laravel")
        assert laravel.confidence == 0.99  # composer.json + artisan
        assert laravel.version == "11"

    def test_runtime_resolved_from_framework_language(self):
        profile = detect_project(FIXTURES / "laravel_valid")
        assert profile.runtime is not None
        assert profile.runtime.name == "php"

    def test_capabilities_aggregated(self):
        profile = detect_project(FIXTURES / "laravel_valid")
        caps = {c.value for c in profile.capabilities}
        assert "route_analysis" in caps
        assert "runtime_inspection" in caps
        assert "test_execution" in caps

    def test_profile_json_shape_matches_spec(self):
        profile = profile_of("laravel_valid")
        assert set(profile) == {"languages", "frameworks", "runtime"}
        php = profile["languages"][0]
        assert set(php) == {"name", "confidence"}
        laravel = profile["frameworks"][0]
        assert set(laravel) == {"name", "version", "confidence"}
        assert set(profile["runtime"]) == {"name", "version"}

    def test_routes_analyzed(self):
        from agent.core.workspace import Workspace

        registry = register_builtins(PluginRegistry())
        result = registry.framework("laravel").analyze(
            Workspace(FIXTURES / "laravel_valid")
        )
        assert any("route file" in f.message for f in result.findings)
        assert any("Eloquent model" in f.message for f in result.findings)

    def test_validators_exposed(self):
        registry = register_builtins(PluginRegistry())
        names = [v.name for v in registry.framework("laravel").get_validators()]
        assert names == ["artisan", "phpunit"]


class TestBrokenProject:
    def test_broken_composer_json_not_detected_as_laravel(self):
        profile = detect_project(FIXTURES / "laravel_broken")
        fw_names = [f.name for f in profile.frameworks]
        assert "laravel" not in fw_names
        # artisan file still marks it PHP.
        assert "php" in [l.name for l in profile.languages]


class TestPlainPhp:
    def test_detected_as_php_without_framework(self):
        profile = detect_project(FIXTURES / "plain_php")
        assert "php" in [l.name for l in profile.languages]
        assert profile.frameworks == []
        assert profile.runtime is not None
        assert profile.runtime.name == "php"


class TestMultiLanguage:
    def test_project_with_js_and_php_reports_php_only_with_builtins(self):
        """Multi-language support is plugin-driven: only registered language
        plugins answer. PHP wins here because no JS plugin is registered."""
        profile = detect_project(FIXTURES / "laravel_valid")
        names = [l.name for l in profile.languages]
        assert "php" in names


class TestPhpAnalyze:
    def test_ast_analysis_via_php_tools(self):
        from agent.core.workspace import Workspace

        registry = register_builtins(PluginRegistry())
        result = registry.language("php").analyze(
            Workspace(FIXTURES / "plain_php")
        )
        assert result.scope.kind == "ast"
        assert "chunk" in result.summary
