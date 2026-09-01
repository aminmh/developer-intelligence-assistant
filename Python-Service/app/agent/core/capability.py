"""Capability model — what a language/framework plugin can actually do.

The agent workflow adapts to the capabilities a detected project advertises
(spec §9). A framework that cannot inspect its runtime simply does not list
``RUNTIME_INSPECTION``.
"""

from __future__ import annotations

from enum import Enum


class Capability(str, Enum):
    ROUTE_ANALYSIS = "route_analysis"
    DEPENDENCY_ANALYSIS = "dependency_analysis"
    AST_ANALYSIS = "ast_analysis"
    TYPE_CHECKING = "type_checking"
    RUNTIME_INSPECTION = "runtime_inspection"
    TEST_EXECUTION = "test_execution"
    CODE_FORMATTING = "code_formatting"
    LINTING = "linting"
    DATABASE_INSPECTION = "database_inspection"
    CONTAINER_INSPECTION = "container_inspection"
