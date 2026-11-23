"""PHP language plugin — wraps existing LSP + PHP-CLI tooling.

Reuses the in-image PHP CLI (``app/php_tools.run_code_parse``) for AST
analysis and the LSP framework's PHP adapter (``app/lsp``) for validation.
No new PHP machinery — the plugin is a thin adapter behind the generic
LanguagePlugin interface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent.core.capability import Capability
from agent.core.types import (
    AnalysisFinding,
    AnalysisResult,
    AnalysisScope,
    DetectionResult,
    Project,
    Tool,
    Workspace,
)
from agent.interfaces.language import LanguagePlugin
from agent.interfaces.validator import Validator
from agent.plugins.validators import PhpLspValidator

if TYPE_CHECKING:
    from agent.core.revision import RevisionContext

_PHP_MARKERS = ("composer.json", "artisan")
_PHP_EXTENSIONS = (".php", ".php3", ".php4", ".php5", ".phtml")


class PhpLanguagePlugin(LanguagePlugin):
    name = "php"
    # Language tooling the framework-neutral Code Quality checks read (PHPUnit is PHP's test runner,
    # whatever the framework): test classes, and test configuration against the installed PHPUnit.
    capabilities = (Capability.TEST_CONVENTIONS, Capability.CONFIG_RISKS, Capability.INSTALLED_PACKAGES)

    def provide(self, capability: Capability, context: "RevisionContext") -> list:
        from agent.interfaces.capabilities import CapabilityUnavailable
        from agent.plugins.languages.php.testing import provide_test_config_risks, provide_tests

        if capability == Capability.INSTALLED_PACKAGES:  # composer.lock, read as data
            from agent.plugins.languages.php.packages import provide_installed_packages

            return provide_installed_packages(context)
        if capability == Capability.CONFIG_RISKS:
            return provide_test_config_risks(context)
        if capability == Capability.TEST_CONVENTIONS:
            facts = context.facts("php")
            if facts is None:
                raise CapabilityUnavailable("PHP facts are unavailable for this revision, so PHPUnit tests cannot be listed.")
            return provide_tests(context, facts)
        return []

    @property
    def extensions(self) -> list[str]:
        return list(_PHP_EXTENSIONS)

    def __init__(self, lsp_registry: object | None = None) -> None:
        # Injected by register_builtins from the DI container (``lsp_registry``);
        # None keeps validation structural (hermetic tests).
        self._lsp_registry = lsp_registry

    # -- detection ---------------------------------------------------------

    def detect(self, project: Project) -> DetectionResult:
        if project.has_any(("composer.json", "artisan")):
            return DetectionResult(name="php", confidence=0.95)
        if project.files:
            ratio = self._php_ratio(project)
            if ratio >= 0.5:
                return DetectionResult(name="php", confidence=0.6 + 0.3 * ratio)
        return DetectionResult(name="php", confidence=0.0)

    @staticmethod
    def _php_ratio(project: Project) -> float:
        if not project.files:
            return 0.0
        php = sum(1 for f in project.files if f.suffix in _PHP_EXTENSIONS)
        return php / len(project.files)

    # -- analysis ------------------------------------------------------------

    def analyze(self, workspace: Workspace) -> AnalysisResult:
        # Reuse the existing PHP AST extractor (php_tools.run_code_parse).
        from infra.php_tools import run_code_parse

        scope = AnalysisScope(kind="ast", detail="PHP AST chunks via php bin/code-parse")
        try:
            chunks = run_code_parse(str(workspace.root))
        except Exception as exc:  # noqa: BLE001 - php binary failures surface as analysis findings
            return AnalysisResult(
                scope=scope,
                summary=f"AST analysis unavailable: {exc}",
                findings=[
                    AnalysisFinding(
                        message=str(exc), kind="warning", location=None
                    )
                ],
            )
        symbols = sum(len(chunk.get("symbols", [])) for chunk in chunks)
        return AnalysisResult(
            scope=scope,
            summary=f"Parsed {len(chunks)} AST chunk(s), {symbols} symbol(s)",
            findings=[
                AnalysisFinding(message=f"AST chunks: {len(chunks)}", kind="info"),
                AnalysisFinding(message=f"Symbols: {symbols}", kind="info"),
            ],
        )

    # -- tools / validators ---------------------------------------------------

    def get_tools(self) -> list[Tool]:
        return []

    def get_validators(self) -> list[Validator]:
        return [PhpLspValidator(self._lsp_registry)]
