"""Symfony/Doctrine framework integration."""

from .plugin import SymfonyFrameworkPlugin
from .schema_visualization import build_schema_visualization

__all__ = ["SymfonyFrameworkPlugin", "build_schema_visualization"]
