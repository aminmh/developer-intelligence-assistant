"""TestFrameworkPlugin — the extensibility proof (spec §25).

A deliberately fake framework that has NOTHING to do with PHP or Laravel.
It proves the core registry can load and drive any framework plugin without
framework-specific code: register it, detect its marker, get its capabilities
and validators — with zero references to Laravel in the core path.
"""

from __future__ import annotations

import json

from agent.core.capability import Capability
from agent.core.types import (
    AnalysisFinding,
    AnalysisResult,
    AnalysisScope,
    DetectionResult,
    Project,
    RuntimeInspection,
    Tool,
    ValidationContext,
    ValidationResult,
    Workspace,
)
from agent.interfaces.framework import FrameworkPlugin
from agent.interfaces.validator import Validator

_MARKER = ".testframework.json"


class _MarkerValidator(Validator):
    """Validates that the marker file is well-formed JSON."""

    name = "testframework-marker"

    async def validate(self, context: ValidationContext) -> ValidationResult:
        if not context.workspace.exists(_MARKER):
            return ValidationResult(
                valid=False, detail=".testframework.json marker missing"
            )
        try:
            json.loads(context.workspace.read(_MARKER))
        except (ValueError, OSError) as exc:
            return ValidationResult(
                valid=False, detail=f"marker not valid JSON: {exc}"
            )
        return ValidationResult(valid=True, detail="marker OK")


class TestFrameworkPlugin(FrameworkPlugin):
    name = "testframework"
    language = "fixture"

    capabilities = (Capability.LINTING, Capability.CODE_FORMATTING)

    def detect(self, project: Project) -> DetectionResult:
        marker = project.root / _MARKER
        if not marker.exists():
            return DetectionResult(name="testframework", confidence=0.0)
        version = None
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            version = str(data.get("version", "")) or None
        except (json.JSONDecodeError, OSError):
            version = None
        return DetectionResult(
            name="testframework", confidence=1.0, version=version
        )

    def analyze(self, workspace: Workspace) -> AnalysisResult:
        scope = AnalysisScope(kind="marker", detail="fixture marker analysis")
        try:
            data = json.loads(workspace.read(_MARKER))
        except (ValueError, OSError):
            return AnalysisResult(
                scope=scope,
                summary="marker unreadable",
                findings=[AnalysisFinding(message="marker unreadable", kind="error")],
            )
        return AnalysisResult(
            scope=scope,
            summary=f"marker parsed: {sorted(data.keys())}",
            findings=[AnalysisFinding(message="marker valid", kind="info")],
        )

    def inspect_runtime(self, runtime: object) -> RuntimeInspection:
        return RuntimeInspection(
            name="fixture", version=None, capabilities=self.capabilities, detail=""
        )

    def get_tools(self) -> list[Tool]:
        return []

    def get_validators(self) -> list[Validator]:
        return [_MarkerValidator()]
