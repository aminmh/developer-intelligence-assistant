"""Plugin registry tests — registration, entry-point discovery, DI.

Verifies the core can load and drive plugins with zero framework-specific
logic (spec §25 acceptance: TestFrameworkPlugin).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.core.di import Container
from agent.core.registry import (
    FRAMEWORK_ENTRY_POINT_GROUP,
    LANGUAGE_ENTRY_POINT_GROUP,
    PluginRegistry,
    _load_entry_point_classes,
)
from agent.core.types import Project
from agent.plugins import register_builtins
from agent.plugins.frameworks.laravel import LaravelFrameworkPlugin
from agent.plugins.languages.php import PhpLanguagePlugin
from agent.plugins.test_framework import TestFrameworkPlugin

FIXTURES = Path(__file__).parent / "fixtures"


def make_registry() -> PluginRegistry:
    return register_builtins(PluginRegistry())


class TestRegistration:
    def test_register_language_enforces_protocol(self):
        registry = PluginRegistry()
        with pytest.raises(TypeError):
            registry.register_language(object())  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            registry.register_framework(object())  # type: ignore[arg-type]

    def test_builtins_registered(self):
        registry = make_registry()
        assert set(registry.languages) == {"php"}
        assert set(registry.frameworks) == {"laravel", "symfony", "testframework", "angular", "react", "vue"}
        assert isinstance(registry.language("php"), PhpLanguagePlugin)
        assert isinstance(registry.framework("laravel"), LaravelFrameworkPlugin)
        assert isinstance(registry.framework("testframework"), TestFrameworkPlugin)

    def test_duplicate_registration_overwrites(self):
        registry = make_registry()
        registry.register_framework(TestFrameworkPlugin())
        assert registry.framework("testframework").name == "testframework"


class TestEntryPointDiscovery:
    def test_loader_returns_class_not_instance(self, monkeypatch):
        class _Ep:
            name = "demo"

            @staticmethod
            def load():
                return TestFrameworkPlugin

        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda group: [_Ep()] if group == FRAMEWORK_ENTRY_POINT_GROUP else [],
        )
        discovered = _load_entry_point_classes(FRAMEWORK_ENTRY_POINT_GROUP)
        assert discovered["demo"] is TestFrameworkPlugin

    def test_discover_entry_points_registers_new_plugin(self, monkeypatch):
        class EpExtra(TestFrameworkPlugin):
            name = "ep-extra"

        class _Ep:
            name = "ep-extra"

            @staticmethod
            def load():
                return EpExtra

        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda group: [_Ep()] if group == FRAMEWORK_ENTRY_POINT_GROUP else [],
        )
        registry = make_registry()
        registry.discover_entry_points()
        assert "ep-extra" in registry.frameworks

    def test_loader_survives_broken_entry_points(self, monkeypatch):
        class _BrokenEp:
            name = "broken"

            @staticmethod
            def load():
                raise ImportError("plugin failed to import")

        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda group: [_BrokenEp()] if group == LANGUAGE_ENTRY_POINT_GROUP else [],
        )
        assert _load_entry_point_classes(LANGUAGE_ENTRY_POINT_GROUP) == {}

    def test_loader_survives_missing_metadata(self, monkeypatch):
        def boom(group):
            raise RuntimeError("metadata unavailable")

        monkeypatch.setattr("importlib.metadata.entry_points", boom)
        assert _load_entry_point_classes(FRAMEWORK_ENTRY_POINT_GROUP) == {}


class TestDi:
    def test_register_resolve_singleton(self):
        container = Container()
        container.register("x", lambda c: {"n": 1})
        assert container.resolve("x") is container.resolve("x")

    def test_register_resolve_fresh(self):
        container = Container()
        container.register("x", lambda c: {"n": 1}, singleton=False)
        assert container.resolve("x") is not container.resolve("x")

    def test_resolve_unknown_raises(self):
        with pytest.raises(KeyError):
            Container().resolve("nope")

    def test_get_unknown_returns_default(self):
        assert Container().get("nope", 42) == 42

    def test_factory_receives_container(self):
        container = Container()
        container.register("a", lambda c: {"v": 1})
        container.register("b", lambda c: {"a": c.resolve("a")})
        assert container.resolve("b")["a"] == {"v": 1}


class TestTestFrameworkPluginIndependence:
    """The extensibility proof: a fake framework must work with no PHP at all."""

    def test_detects_marker_project(self):
        plugin = TestFrameworkPlugin()
        project = Project.from_path(FIXTURES / "testframework_demo")
        result = plugin.detect(project)
        assert result.name == "testframework"
        assert result.confidence == 1.0
        assert result.version == "0.3.0"

    def test_registry_detects_it_without_php(self):
        registry = make_registry()
        profile = registry.detect(
            Project.from_path(FIXTURES / "testframework_demo")
        )
        names = [f.name for f in profile.frameworks]
        assert "testframework" in names
        # No PHP files anywhere in this fixture — language detection stays empty.
        assert profile.languages == []

    def test_validators_exposed(self):
        plugin = TestFrameworkPlugin()
        assert [v.name for v in plugin.get_validators()] == [
            "testframework-marker"
        ]
