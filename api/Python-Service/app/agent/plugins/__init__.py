"""Builtin plugin registration.

The core discovers plugins through the registry — never through hard-coded
framework logic. This module registers the builtins shipped with the repo;
external plugins arrive via entry points (``discover_entry_points``).
"""

from __future__ import annotations

from agent.core.registry import PluginRegistry
from agent.plugins.frameworks.frontend import AngularFrameworkPlugin, ReactFrameworkPlugin, VueFrameworkPlugin
from agent.plugins.frameworks.laravel import LaravelFrameworkPlugin
from agent.plugins.frameworks.symfony import SymfonyFrameworkPlugin
from agent.plugins.languages.php import PhpLanguagePlugin
from agent.plugins.test_framework import TestFrameworkPlugin


def register_builtins(registry: PluginRegistry) -> PluginRegistry:
    from agent.migration.adapters import LaravelMigrationAdapter, DoctrineMigrationAdapter
    registry.register_language(PhpLanguagePlugin())
    registry.register_framework(LaravelFrameworkPlugin())
    registry.register_framework(SymfonyFrameworkPlugin())
    registry.register_framework(TestFrameworkPlugin())
    for frontend_plugin in (AngularFrameworkPlugin(), ReactFrameworkPlugin(), VueFrameworkPlugin()):
        registry.register_framework(frontend_plugin)
    registry.register_migration_adapter("laravel", LaravelMigrationAdapter())
    registry.register_migration_adapter("symfony", DoctrineMigrationAdapter())
    registry.register_migration_adapter("doctrine", DoctrineMigrationAdapter())
    return registry
