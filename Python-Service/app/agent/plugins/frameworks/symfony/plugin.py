"""Symfony/Doctrine framework plugin registration and detection."""
from __future__ import annotations

import json

from agent.core.capability import Capability
from agent.core.types import AnalysisFinding, AnalysisResult, AnalysisScope, DetectionResult, Project, RuntimeInspection, Tool, Workspace
from agent.interfaces.framework import FrameworkPlugin
from agent.interfaces.validator import Validator


class SymfonyFrameworkPlugin(FrameworkPlugin):
    name = "symfony"
    language = "php"
    capabilities = (Capability.AST_ANALYSIS, Capability.DEPENDENCY_ANALYSIS)

    def detect(self, project: Project) -> DetectionResult:
        manifest = project.root / "composer.json"
        if not manifest.is_file():
            return DetectionResult(name=self.name, confidence=0.0)
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return DetectionResult(name=self.name, confidence=0.0)
        requirements = {**(data.get("require") or {}), **(data.get("require-dev") or {})}
        marker = any(str(name).startswith("symfony/") for name in requirements) or "doctrine/orm" in requirements
        if not marker:
            return DetectionResult(name=self.name, confidence=0.0)
        version = None
        for package in ("symfony/framework-bundle", "symfony/symfony", "doctrine/orm"):
            if package in requirements:
                version = str(requirements[package])
                break
        return DetectionResult(name=self.name, confidence=0.9, version=version)

    def analyze(self, workspace: Workspace) -> AnalysisResult:
        scope = AnalysisScope(kind="schema", detail="Symfony Doctrine metadata and migrations")
        return AnalysisResult(
            scope=scope,
            summary="Symfony/Doctrine detected; schema visualization uses saved Doctrine metadata and migration facts when available.",
            findings=[AnalysisFinding(message="Doctrine adapter available", kind="info")],
        )

    def inspect_runtime(self, runtime: object) -> RuntimeInspection:
        return RuntimeInspection(name=self.name, version=None, capabilities=self.capabilities, detail="")

    def get_tools(self) -> list[Tool]:
        return []

    def get_validators(self) -> list[Validator]:
        return []


__all__ = ["SymfonyFrameworkPlugin"]
