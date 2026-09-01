"""Laravel framework plugin — the first implementation of the generic
framework adapter (spec §1, §5, §25).

Contains Laravel-specific knowledge ONLY: marker detection, version parsing,
route/module structure analysis, artisan-aware validators. The core agent
never references Laravel directly.
"""

from __future__ import annotations

import json
import re

from agent.core.capability import Capability
from agent.core.types import (
    AnalysisFinding,
    AnalysisResult,
    AnalysisScope,
    DetectionResult,
    Project,
    RuntimeInspection,
    Tool,
    Workspace,
)
from agent.interfaces.framework import FrameworkPlugin
from agent.interfaces.validator import Validator
from agent.plugins.validators import ArtisanValidator, PhpUnitValidator

_LARAVEL_REQUIRE = "laravel/framework"
_VERSION_RE = re.compile(r"\^?v?(\d+)")


class LaravelFrameworkPlugin(FrameworkPlugin):
    name = "laravel"
    language = "php"

    capabilities = (
        Capability.AST_ANALYSIS,
        Capability.DEPENDENCY_ANALYSIS,
        Capability.ROUTE_ANALYSIS,
        Capability.RUNTIME_INSPECTION,
        Capability.TEST_EXECUTION,
    )

    def detect(self, project: Project) -> DetectionResult:
        composer = project.root / "composer.json"
        if not composer.exists():
            return DetectionResult(name="laravel", confidence=0.0)
        try:
            data = json.loads(composer.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return DetectionResult(name="laravel", confidence=0.0)

        requires = data.get("require", {})
        if _LARAVEL_REQUIRE not in requires:
            return DetectionResult(name="laravel", confidence=0.0)

        version = None
        match = _VERSION_RE.search(str(requires[_LARAVEL_REQUIRE]))
        if match:
            version = match.group(1)

        confidence = 0.95
        if project.has_any(("artisan",)):
            confidence = 0.99
        return DetectionResult(name="laravel", confidence=confidence, version=version)

    def analyze(self, workspace: Workspace) -> AnalysisResult:
        """Structural Laravel analysis: routes, controllers, models, config.

        TODO(scope-B): run ``php artisan route:list --json`` for live routes
        and container/eloquent analyzers (spec §5).
        """
        scope = AnalysisScope(kind="routes", detail="Laravel route/module structure")

        findings: list[AnalysisFinding] = []
        routes = list(workspace.root.rglob("routes/*.php")) if workspace.exists("routes") else []
        if routes:
            findings.append(
                AnalysisFinding(
                    message=f"{len(routes)} route file(s): "
                    + ", ".join(p.name for p in routes),
                    kind="info",
                )
            )
        else:
            findings.append(
                AnalysisFinding(message="No routes/ directory found", kind="warning")
            )

        if workspace.exists("app", "Models"):
            models = list((workspace.root / "app" / "Models").rglob("*.php"))
            findings.append(
                AnalysisFinding(
                    message=f"{len(models)} Eloquent model(s)", kind="info"
                )
            )

        summary = "; ".join(f.message for f in findings)
        return AnalysisResult(scope=scope, findings=findings, summary=summary)

    def inspect_runtime(self, runtime: object) -> RuntimeInspection:
        # TODO(scope-B): run `php artisan --version` against the injected
        # Runtime (never direct subprocess/docker here).
        return RuntimeInspection(
            name="artisan",
            version=None,
            capabilities=self.capabilities,
            detail="artisan CLI runtime (version probe deferred to scope-B)",
        )

    def get_tools(self) -> list[Tool]:
        return []

    def get_validators(self) -> list[Validator]:
        return [ArtisanValidator(), PhpUnitValidator()]
