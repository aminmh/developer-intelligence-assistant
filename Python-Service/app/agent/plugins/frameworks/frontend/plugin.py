"""Angular, React, and Vue framework plugins.

Plan: @.archcore/frontend-assist/frontend-developer-assistance.plan.md (Phase 2).

The three frameworks differ in how they declare routes, components, and stores,
but the result has one shape, so one extractor (``projects.frontend.ui_map``)
does the work and each plugin only names its framework. Detection reads
``package.json``; confidence stays below the PHP framework plugins so a Laravel
or Symfony project that ships a front end is still profiled as PHP.
"""

from __future__ import annotations

from agent.core.capability import Capability
from agent.core.types import AnalysisFinding, AnalysisResult, AnalysisScope, DetectionResult, Project, RuntimeInspection, Tool, Workspace
from agent.interfaces.validator import Validator

FRONTEND_CONFIDENCE = 0.8


class _FrontendFrameworkPlugin:
    name = ""
    language = "typescript"
    capabilities = (Capability.AST_ANALYSIS, Capability.DEPENDENCY_ANALYSIS)

    def _app(self, root) -> dict | None:
        from projects.frontend.ui_map import find_apps

        return next((app for app in find_apps(root) if app["framework"] == self.name), None)

    def detect(self, project: Project) -> DetectionResult:
        try:
            app = self._app(project.root)
        except OSError:
            app = None
        if app is None:
            return DetectionResult(name=self.name, confidence=0.0)
        return DetectionResult(name=self.name, confidence=FRONTEND_CONFIDENCE, version=app.get("version"))

    def analyze(self, workspace: Workspace, runtime_routes: bool = False) -> AnalysisResult:
        """Static only; ``runtime_routes`` exists so offline callers can state that no runtime is used."""
        from projects.frontend.ui_map import build_ui_map

        scope = AnalysisScope(kind="ui", detail=f"{self.name} routes, components, stores, and HTTP calls")
        app = self._app(workspace.root)
        if app is None:
            return AnalysisResult(scope=scope, summary=f"No {self.name} application found.")
        ui_map = build_ui_map(workspace.root, app)
        routes = [{"method": "PAGE", "uri": route["path"], "action": route["component"] or route["file"] or "?",
                   "name": None, "middleware": [], "file": route["source_file"]}
                  for route in ui_map["routes"] if not route["redirect"]]
        components = [{"name": component["name"], "kind": component["kind"], "file": component["file"]}
                      for component in ui_map["components"]]
        label = ui_map.get("meta_framework") or self.name
        summary = (f"{label} application: {len(routes)} routes, {len(components)} components, "
                   f"{len(ui_map['stores'])} stores, {len(ui_map['http_calls'])} HTTP calls.")
        return AnalysisResult(
            scope=scope, summary=summary,
            findings=[AnalysisFinding(message=note, kind="info") for note in ui_map["notes"]],
            data={"routes": routes, "frontend": {"views": [], "components": components, "route_view_links": []},
                  "ui_map": ui_map},
        )

    def inspect_runtime(self, runtime: object) -> RuntimeInspection:
        return RuntimeInspection(name=self.name, version=None, capabilities=self.capabilities, detail="")

    def get_tools(self) -> list[Tool]:
        return []

    def get_validators(self) -> list[Validator]:
        return []


class AngularFrameworkPlugin(_FrontendFrameworkPlugin):
    name = "angular"


class ReactFrameworkPlugin(_FrontendFrameworkPlugin):
    name = "react"
    language = "javascript"


class VueFrameworkPlugin(_FrontendFrameworkPlugin):
    name = "vue"
    language = "javascript"
