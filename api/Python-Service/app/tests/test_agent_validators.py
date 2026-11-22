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


# ─── LSP validation through the injected registry (lsp-first plan, tasks 19-20) ─

class _FakeLspRegistry:
    def __init__(self, enabled=True, results=None):
        self.enabled = enabled
        self.results = results or {}
        self.calls = []

    def is_enabled(self, language):
        return self.enabled

    def validate(self, language, code, filename=None):
        self.calls.append((language, filename))
        return self.results.get(filename, {"valid": True, "errors": [], "warnings": [], "source": "lsp"})


def _php_project(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    return _ctx(tmp_path)


def test_lsp_errors_fail_validation_and_warnings_do_not(tmp_path):
    registry = _FakeLspRegistry(results={
        "src/Bad.php": {"valid": False, "source": "lsp", "warnings": [],
                        "errors": [{"message": "Syntax error, unexpected '}'", "line": 4}]},
        "src/Ok.php": {"valid": True, "source": "lsp", "errors": [],
                       "warnings": [{"message": "Class \"Foo\" not found", "line": 2}]},
    })
    ctx = _php_project(tmp_path, {"src/Bad.php": "<?php }", "src/Ok.php": "<?php new Foo;"})
    result = asyncio.run(PhpLspValidator(registry).validate(ctx))
    assert result.valid is False
    assert [(i.severity, i.location) for i in result.issues] == [("error", "src/Bad.php:4"), ("warning", "src/Ok.php:2")]
    assert "1 error(s), 1 warning(s)" in result.detail
    assert registry.calls == [("php", "src/Bad.php"), ("php", "src/Ok.php")]


def test_warnings_alone_pass(tmp_path):
    registry = _FakeLspRegistry(results={"a.php": {"valid": True, "errors": [], "source": "lsp",
                                                   "warnings": [{"message": "unused", "line": 1}]}})
    result = asyncio.run(PhpLspValidator(registry).validate(_php_project(tmp_path, {"a.php": "<?php"})))
    assert result.valid is True and len(result.issues) == 1


def test_disabled_language_falls_back_to_structural_check(tmp_path):
    registry = _FakeLspRegistry(enabled=False)
    result = asyncio.run(PhpLspValidator(registry).validate(_php_project(tmp_path, {"a.php": "<?php"})))
    assert result.valid is True and "structural" in result.detail
    assert registry.calls == []


def test_file_limit_is_reported(tmp_path, monkeypatch):
    from agent.plugins import validators

    monkeypatch.setattr(validators, "MAX_LSP_FILES", 2)
    registry = _FakeLspRegistry()
    ctx = _php_project(tmp_path, {f"f{i}.php": "<?php" for i in range(3)})
    result = asyncio.run(PhpLspValidator(registry).validate(ctx))
    assert len(registry.calls) == 2 and "1 file(s) not checked" in result.detail


def test_generic_validator_names_its_language(tmp_path):
    from agent.plugins.validators import LspValidator

    validator = LspValidator("python", (".py",), _FakeLspRegistry())
    assert validator.name == "python-lsp"
    result = asyncio.run(validator.validate(_php_project(tmp_path, {"m.py": "x = 1"})))
    assert result.valid is True


def test_builtins_get_the_registry_from_the_container():
    from agent import default_registry
    from agent.core.di import Container
    from agent.core.registry import PluginRegistry
    from agent.plugins import register_builtins

    fake = _FakeLspRegistry()
    container = Container()
    container.register("lsp_registry", lambda _c: fake)
    wired = register_builtins(PluginRegistry(), container)
    assert wired.language("php").get_validators()[0]._lsp_registry is fake
    hermetic = register_builtins(PluginRegistry())
    assert hermetic.language("php").get_validators()[0]._lsp_registry is None
    from lsp import registry as live

    assert default_registry().language("php").get_validators()[0]._lsp_registry is live
