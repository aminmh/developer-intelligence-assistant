"""Symfony/Doctrine framework plugin registration and detection."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agent.core.capability import Capability
from agent.core.types import AnalysisFinding, AnalysisResult, AnalysisScope, DetectionResult, Project, RuntimeInspection, Tool, Workspace
from agent.interfaces.framework import FrameworkPlugin
from agent.interfaces.validator import Validator

if TYPE_CHECKING:
    from agent.core.revision import RevisionContext


class SymfonyFrameworkPlugin(FrameworkPlugin):
    name = "symfony"
    language = "php"
    capabilities = (Capability.AST_ANALYSIS, Capability.DEPENDENCY_ANALYSIS,
                    Capability.ROUTE_ANALYSIS, Capability.ROUTE_AUTHORIZATION, Capability.TAINT_MODEL,
                    Capability.CONFIG_RISKS, Capability.TRANSLATIONS, Capability.TEMPLATES,
                    Capability.CHARACTERIZATION_TESTS, Capability.DOC_SOURCES)

    def provide(self, capability: Capability, context: "RevisionContext") -> list:
        """Routes, guards and access rules from PHP facts and Symfony configuration; the Psalm taint model.

        Decision: framework-knowledge-lives-in-plugins-over-language-facts. No PHP is parsed here.
        """
        from agent.interfaces.capabilities import CapabilityUnavailable
        from agent.plugins.frameworks.symfony.authorization import provide_access_rules, provide_guards
        from agent.plugins.frameworks.symfony.routes import provide_routes

        if capability == Capability.TAINT_MODEL:
            from pathlib import Path as _Path

            from agent.core.records import TaintStubRecord

            stub = _Path(__file__).with_name("psalm") / "taint.phpstub"
            return [TaintStubRecord(framework="symfony", analyzer="psalm", name="symfony.phpstub",
                                    content=stub.read_text(encoding="utf-8"))]
        if capability == Capability.DOC_SOURCES:  # a fixed documentation repository, no PHP facts
            from agent.plugins.frameworks.symfony.doc_sources import provide_doc_sources

            return provide_doc_sources(context)
        if capability == Capability.CHARACTERIZATION_TESTS:  # a fixed WebTestCase layout, no PHP facts
            from agent.plugins.frameworks.symfony.test_templates import provide_test_templates

            return provide_test_templates(context)
        if capability in (Capability.TRANSLATIONS, Capability.TEMPLATES):  # configuration files, no PHP facts
            from agent.plugins.frameworks.symfony.localization import provide_templates, provide_translations

            return provide_translations(context) if capability == Capability.TRANSLATIONS else provide_templates(context)
        facts = context.facts("php")
        if facts is None:
            raise CapabilityUnavailable("PHP facts are unavailable for this revision, so Symfony routes cannot be read.")
        if capability == Capability.ROUTE_ANALYSIS:
            return provide_routes(context, facts)
        if capability == Capability.ROUTE_AUTHORIZATION:
            return [*provide_guards(facts), *provide_access_rules(context)]
        if capability == Capability.CONFIG_RISKS:
            from agent.plugins.frameworks.symfony.config_risks import provide_config_risks

            return provide_config_risks(context, facts)
        return []

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
