"""Workspace + validator tests — the sandbox boundary and deterministic checks."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.core.types import Project, ValidationContext
from agent.core.workspace import Workspace
from agent.plugins.validators import ArtisanValidator, PhpLspValidator, PhpUnitValidator

FIXTURES = Path(__file__).parent / "fixtures"


class TestWorkspaceBoundary:
    def test_path_within_root_resolves(self):
        ws = Workspace(FIXTURES / "plain_php")
        p = ws.path("index.php")
        assert p == (FIXTURES / "plain_php" / "index.php").resolve()

    def test_path_escape_refused(self):
        ws = Workspace(FIXTURES / "plain_php")
        with pytest.raises(ValueError):
            ws.path("..", "laravel_valid", "composer.json")

    def test_absolute_escape_refused(self):
        ws = Workspace(FIXTURES / "plain_php")
        with pytest.raises(ValueError):
            ws.path(str(FIXTURES / "laravel_valid" / "composer.json"))

    def test_iter_files_filters_suffix(self):
        ws = Workspace(FIXTURES / "plain_php")
        assert {p.suffix for p in ws.iter_files(".php")} == {".php"}


def _ctx(path: Path) -> ValidationContext:

    return ValidationContext(workspace=Workspace(path), project=Project.from_path(path))


class TestValidators:
    def test_php_validator_accepts_nonempty_php(self):
        result = asyncio.run(PhpLspValidator().validate(_ctx(FIXTURES / "plain_php")))
        assert result.valid is True

    def test_php_validator_rejects_empty_project(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "empty.php").write_text("")
            result = asyncio.run(PhpLspValidator().validate(_ctx(Path(tmp))))
        assert result.valid is False

    def test_artisan_validator(self):
        assert asyncio.run(ArtisanValidator().validate(_ctx(FIXTURES / "laravel_valid"))).valid
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(ArtisanValidator().validate(_ctx(Path(tmp))))
        assert result.valid is False

    def test_phpunit_validator_requires_vendor(self):
        result = asyncio.run(PhpUnitValidator().validate(_ctx(FIXTURES / "laravel_valid")))
        assert result.valid is False  # fixture has no vendor/bin/phpunit
        assert "composer install" in result.detail
