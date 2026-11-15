"""Language plugin interface (spec §4)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.core.types import (
    AnalysisResult,
    DetectionResult,
    Project,
    Tool,
    Workspace,
)
from agent.interfaces.validator import Validator


@runtime_checkable
class LanguagePlugin(Protocol):
    """Language-level capabilities: parsing, LSP, toolchain integration."""

    @property
    def name(self) -> str: ...

    @property
    def extensions(self) -> list[str]: ...

    def detect(self, project: Project) -> DetectionResult: ...

    def analyze(self, workspace: Workspace) -> AnalysisResult: ...

    def get_tools(self) -> list[Tool]: ...

    def get_validators(self) -> list[Validator]: ...
