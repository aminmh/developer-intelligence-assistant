"""Framework plugin interface (spec §5)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.core.capability import Capability
from agent.core.types import (
    AnalysisResult,
    DetectionResult,
    Project,
    RuntimeInspection,
    Tool,
    Workspace,
)
from agent.interfaces.validator import Validator


@runtime_checkable
class FrameworkPlugin(Protocol):
    """Framework-level capabilities layered on top of a language plugin."""

    @property
    def name(self) -> str: ...

    @property
    def language(self) -> str: ...

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        """Capabilities this framework advertises (spec §9)."""

    def detect(self, project: Project) -> DetectionResult: ...

    def analyze(self, workspace: Workspace) -> AnalysisResult: ...

    def inspect_runtime(self, runtime: object) -> RuntimeInspection: ...

    def get_tools(self) -> list[Tool]: ...

    def get_validators(self) -> list[Validator]: ...
