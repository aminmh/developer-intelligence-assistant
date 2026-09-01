"""Shared concrete validators behind the Validator protocol.

These are the deterministic checks the agent uses to answer "is the generated
solution actually valid?" (spec §30). TODO(scope-B): wire real LSP/artisan
execution here — current implementations are structural (file-level) so tests
stay hermetic and fast.
"""

from __future__ import annotations

from typing import Any

from agent.core.types import (
    ValidationContext,
    ValidationIssue,
    ValidationResult,
)
from agent.interfaces.validator import Validator


class PhpLspValidator(Validator):
    """PHP syntax/diagnostic validation.

    With an LspRegistry injected this would run the real PHP LSP
    (TODO(scope-B)). The structural fallback below catches gross breakage
    (missing or empty entry files) without spawning servers.
    """

    name = "php-lsp"

    def __init__(self, lsp_registry: Any | None = None) -> None:
        self._lsp_registry = lsp_registry

    async def validate(self, context: ValidationContext) -> ValidationResult:
        if self._lsp_registry is not None:  # pragma: no cover - scope-B path
            return await self._validate_with_lsp(context)
        return self._validate_structure(context)

    async def _validate_with_lsp(self, context: ValidationContext) -> ValidationResult:  # pragma: no cover - scope-B
        assert self._lsp_registry is not None
        issues = []
        for path in context.workspace.iter_files(".php"):
            try:
                code = context.workspace.read(
                    str(path.relative_to(context.workspace.root))
                )
            except OSError:
                continue
            result = self._lsp_registry.validate("php", code, str(path))
            for diag in result.get("diagnostics", []):
                issues.append(
                    ValidationIssue(
                        message=diag.get("message", "diagnostic"),
                        severity=diag.get("severity", "error"),
                        location=str(path),
                    )
                )
        return ValidationResult(valid=not issues, issues=issues)

    @staticmethod
    def _validate_structure(context: ValidationContext) -> ValidationResult:
        php_files = list(context.workspace.iter_files(".php"))
        if not php_files:
            return ValidationResult(
                valid=False, detail="No .php files found in workspace"
            )
        empty = [p for p in php_files if p.stat().st_size == 0]
        if empty:
            return ValidationResult(
                valid=False,
                detail=f"Empty PHP file(s): {[p.name for p in empty]}",
            )
        return ValidationResult(valid=True, detail=f"{len(php_files)} PHP file(s)")


class ArtisanValidator(Validator):
    """Laravel artisan sanity check.

    Structural for now: requires an ``artisan`` entry file. TODO(scope-B):
    execute ``php artisan --version`` and surface its exit code.
    """

    name = "artisan"

    async def validate(self, context: ValidationContext) -> ValidationResult:
        if not context.workspace.exists("artisan"):
            return ValidationResult(valid=False, detail="artisan entry file missing")
        return ValidationResult(valid=True, detail="artisan present")


class PhpUnitValidator(Validator):
    """PHPUnit availability check (Laravel ships vendor/bin/phpunit)."""

    name = "phpunit"

    async def validate(self, context: ValidationContext) -> ValidationResult:
        if not context.workspace.exists("vendor", "bin", "phpunit"):
            return ValidationResult(
                valid=False, detail="vendor/bin/phpunit missing — run composer install"
            )
        return ValidationResult(valid=True, detail="phpunit available")
