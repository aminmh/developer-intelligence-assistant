"""Tests for LspRegistry: config loading, dispatch, multi-language markdown."""

import tempfile
from pathlib import Path

from lsp.adapters.php import Adapter as PhpAdapter
from lsp.registry import LspRegistry


class FakeClient:
    def __init__(self, diagnostics):
        self._diagnostics = diagnostics

    def validate(self, code, filename=None):
        return {"diagnostics": self._diagnostics}


class StubAdapter:
    """Minimal second-language adapter for the multi-language tests."""

    name = "stub"
    fence_tags = ["stub"]

    def wrap(self, code):
        return code, 0

    def map_diagnostics(self, diagnostics, line_offset):
        out = []
        for d in diagnostics or []:
            start = (d.get("range") or {}).get("start") or {}
            out.append(
                {
                    "message": d.get("message", ""),
                    "line": start.get("line", 0) + 1 - line_offset,
                    "column": 1,
                    "severity": d.get("severity", 1),
                    "source": d.get("source"),
                }
            )
        return out

    def fallback(self, code):
        return {"valid": True, "errors": [], "php_version": ""}

    def validate(self, code, filename=None, client=None):
        from lsp.base import validate_with_lsp

        return validate_with_lsp(self, code, filename, client)

    def is_truncated(self, code):
        return False

    def extract_blocks(self, text):
        from lsp.base import extract_fenced_blocks

        return extract_fenced_blocks(text, self.fence_tags)


def _two_lang_config() -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as handle:
        handle.write(
            "php:\n"
            "  enabled: true\n"
            "  adapter: php\n"
            "  fence_tags: [php]\n"
            "  lsp: {binary: /fake/php, args: [language-server], language_id: php}\n"
            "stub:\n"
            "  enabled: true\n"
            "  adapter: stub\n"
            "  fence_tags: [stub]\n"
            "  lsp: {binary: /fake/stub, args: [--stdio], language_id: stub}\n"
        )
        return handle.name


def test_loads_real_config():
    from lsp import registry as live

    assert "php" in live.languages()
    assert live.is_enabled("php")


def test_validate_dispatch_with_mock_client():
    reg = LspRegistry(_two_lang_config())
    calls = []
    adapter = PhpAdapter()

    class Tracking(PhpAdapter):
        def validate(self, code, filename=None, client=None):
            calls.append((code, client))
            return {"valid": True, "errors": [], "warnings": [], "source": "lsp"}

    reg._adapters["php"] = Tracking()
    reg._clients["php"] = FakeClient([])

    result = reg.validate("php", "<?php echo 1;", "x.php")
    assert result["valid"] is True
    assert calls and calls[0][1] is reg._clients["php"]


def test_validate_markdown_multilanguage():
    reg = LspRegistry(_two_lang_config())
    reg._adapters["php"] = PhpAdapter()
    reg._adapters["stub"] = StubAdapter()
    reg._clients["php"] = FakeClient([])
    reg._clients["stub"] = FakeClient([])

    text = (
        "```php\n<?php\nclass A {}\n```\n\n"
        "```stub\nfoo bar\n```\n"
    )
    new_text, summary = reg.validate_markdown(text, question="q", max_retries=1)
    assert len(summary) == 2
    langs = {s["language"] for s in summary}
    assert langs == {"php", "stub"}
    assert all(s["status"] == "ok" for s in summary)
    assert new_text == text


def test_disabled_language_skipped():
    reg = LspRegistry(_two_lang_config())
    reg._config["stub"]["enabled"] = False
    result = reg.validate("stub", "x")
    assert result["source"] == "disabled"


def test_resolve_env():
    import os

    os.environ["TEST_LSP_BIN"] = "/somewhere/bin"
    from lsp.registry import resolve_env

    assert resolve_env("${TEST_LSP_BIN}") == "/somewhere/bin"
    assert resolve_env("${UNSET_VAR_XYZ:-/default/bin}") == "/default/bin"
    assert resolve_env("plain") == "plain"
