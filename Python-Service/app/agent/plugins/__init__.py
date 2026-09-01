"""Builtin plugin registration.

The core discovers plugins through the registry — never through hard-coded
framework logic. This module registers the builtins shipped with the repo;
external plugins arrive via entry points (``discover_entry_points``).
"""

from __future__ import annotations

from agent.core.registry import PluginRegistry
from agent.plugins.frameworks.laravel import LaravelFrameworkPlugin
from agent.plugins.languages.php import PhpLanguagePlugin
from agent.plugins.test_framework import TestFrameworkPlugin


def register_builtins(registry: PluginRegistry) -> PluginRegistry:
    registry.register_language(PhpLanguagePlugin())
    registry.register_framework(LaravelFrameworkPlugin())
    registry.register_framework(TestFrameworkPlugin())
    return registry
