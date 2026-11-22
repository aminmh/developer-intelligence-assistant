"""Shared concrete validators behind the Validator protocol.

These are the deterministic checks the agent uses to answer "is the generated
solution actually valid?" (spec §30). ``LspValidator`` runs the language's
server through the live ``LspRegistry`` when one is injected (lsp-first plan,
tasks 19-20) and falls back to a structural file check otherwise.
TODO(scope-B): artisan execution in ``ArtisanValidator``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent.core.types import (
    ValidationContext,
    ValidationIssue,
    ValidationResult,
)
from agent.interfaces.validator import Validator

MAX_LSP_FILES = 200


class LspValidator(Validator):
    """Language-server diagnostics for one language's files in the workspace.

    With an injected ``LspRegistry`` that has the language enabled, each file
    (sorted, at most ``MAX_LSP_FILES``) is checked by the server off the event
    loop; errors fail validation, warnings do not. Without one, a structural
    check catches gross breakage (no files, empty files) without spawning servers.
    """

    def __init__(self, language: str, extensions: tuple[str, ...], lsp_registry: Any | None = None) -> None:
        self.language = language
        self.extensions = extensions
        self._lsp_registry = lsp_registry

    @property
    def name(self) -> str:
        return f"{self.language}-lsp"

    async def validate(self, context: ValidationContext) -> ValidationResult:
        files = self._files(context)
        if not files:
            return ValidationResult(valid=False, detail=f"No {self.language} files found in workspace")
        if self._lsp_registry is not None and self._enabled():
            return await self._validate_with_lsp(context, files)
        return self._validate_structure(files)

    def _files(self, context: ValidationContext) -> list[Any]:
        found: list[Any] = []
        for suffix in self.extensions:
            found.extend(context.workspace.iter_files(suffix))
        return sorted(set(found))

    def _enabled(self) -> bool:
        try:
            return bool(self._lsp_registry.is_enabled(self.language))
        except Exception:  # noqa: BLE001 — a broken registry means structural checks
            return False

    async def _validate_with_lsp(self, context: ValidationContext, files: list[Any]) -> ValidationResult:
        issues: list[ValidationIssue] = []
        sources: set[str] = set()
        for path in files[:MAX_LSP_FILES]:
            relative = str(path.relative_to(context.workspace.root))
            try:
                code = context.workspace.read(relative)
            except OSError:
                continue
            result = await asyncio.to_thread(self._lsp_registry.validate, self.language, code, relative)
            sources.add(str(result.get("source") or "lsp"))
            for severity, key in (("error", "errors"), ("warning", "warnings")):
                for diagnostic in result.get(key) or []:
                    line = diagnostic.get("line")
                    issues.append(ValidationIssue(
                        message=str(diagnostic.get("message") or "diagnostic")[:500],
                        severity=severity,
                        location=f"{relative}:{line}" if line else relative,
                    ))
        errors = sum(1 for issue in issues if issue.severity == "error")
        checked = min(len(files), MAX_LSP_FILES)
        detail = (f"{checked} {self.language} file(s) checked by {', '.join(sorted(sources)) or 'lsp'}; "
                  f"{errors} error(s), {len(issues) - errors} warning(s)")
        if len(files) > MAX_LSP_FILES:
            detail += f"; {len(files) - MAX_LSP_FILES} file(s) not checked (limit {MAX_LSP_FILES})"
        return ValidationResult(valid=errors == 0, issues=issues, detail=detail)

    def _validate_structure(self, files: list[Any]) -> ValidationResult:
        empty = [p for p in files if p.stat().st_size == 0]
        if empty:
            return ValidationResult(valid=False, detail=f"Empty {self.language} file(s): {[p.name for p in empty]}")
        return ValidationResult(valid=True, detail=f"{len(files)} {self.language} file(s); structural check only")


class PhpLspValidator(LspValidator):
    """PHP files checked by phpactor through the LSP registry (``php-lsp``)."""

    def __init__(self, lsp_registry: Any | None = None) -> None:
        super().__init__("php", (".php",), lsp_registry)


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
