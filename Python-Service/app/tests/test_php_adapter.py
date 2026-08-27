"""Tests for the PHP language adapter (lsp/adapters/php.py).

The language server and LLM are mocked so the suite runs offline.
"""

import llm
from lsp.adapters.php import Adapter


class FakeLspClient:
    def __init__(self, diagnostics):
        self._diagnostics = diagnostics

    def validate(self, code, filename=None):
        return {"diagnostics": self._diagnostics}


class FailOnceClient(FakeLspClient):
    def __init__(self, diag):
        self._diag = diag
        self._calls = 0

    def validate(self, code, filename=None):
        self._calls += 1
        if self._calls == 1:
            return {"diagnostics": [self._diag]}
        return {"diagnostics": []}


def _lsp_error(message, line, severity=1, source="phpactor", code=None):
    return {
        "message": message,
        "severity": severity,
        "source": source,
        "code": code,
        "range": {"start": {"line": line - 1, "character": 0}},
    }


def test_wrap_full_file_no_offset():
    adapter = Adapter()
    code, offset = adapter.wrap("<?php\necho 1;\n")
    assert offset == 0
    assert code == "<?php\necho 1;\n"


def test_wrap_class_adds_php_open():
    code, offset = Adapter().wrap("class Foo {}\n")
    assert offset == 1
    assert code.startswith("<?php\nclass Foo {}")


def test_wrap_method_in_class():
    code, offset = Adapter().wrap("public function x() {}\n")
    assert offset == 2
    assert "__Generated" in code


def test_wrap_statement_in_method():
    code, offset = Adapter().wrap("return 1;\n")
    assert offset == 3
    assert "__gen" in code


def test_validate_ok():
    result = Adapter().validate("<?php\nclass A {}\n", "A.php", FakeLspClient([]))
    assert result["valid"] is True
    assert result["source"] == "lsp"


def test_validate_semantic_error_and_line_shift():
    diag = _lsp_error("Undefined variable $x", line=4)  # line in wrapped doc
    result = Adapter().validate("public function a() {\n    return $x;\n}\n", "B.php", FakeLspClient([diag]))
    assert result["valid"] is False
    assert result["errors"][0]["line"] == 2


def test_validate_downgrades_external_refs():
    diag = _lsp_error('Class "Invoice" not found', line=5)
    result = Adapter().validate("<?php\nclass A {\n  public function f($i) { return $i; }\n}\n", "C.php", FakeLspClient([diag]))
    assert result["valid"] is True
    assert result["warnings"] and result["warnings"][0]["severity"] == 2


def test_validate_falls_back_without_client(monkeypatch):
    adapter = Adapter()

    def fake_lint(code):
        return {"valid": False, "errors": [{"message": "syntax error", "line": 2, "column": 1}], "php_version": "8.4"}

    monkeypatch.setattr(adapter, "fallback", fake_lint)
    result = adapter.validate("<?php\nclass A {\n  public function x( {\n}\n", "D.php", None)
    assert result["valid"] is False
    assert result["source"] == "lint"
    assert result["errors"][0]["message"] == "syntax error"


def test_extract_blocks():
    md = (
        "Intro\n\n"
        "```php\n<?php\necho 1;\n```\n\n"
        "```python\nprint(1)\n```\n\n"
        "```\n<?php\nclass Bare {}\n```\n"
    )
    blocks = Adapter().extract_blocks(md)
    assert len(blocks) == 2
    assert blocks[0]["lang"] == "php"
    assert blocks[1]["lang"] == "php"
    assert "echo 1" in blocks[0]["code"]
    assert "class Bare" in blocks[1]["code"]


def test_repair_loop_splices_corrected_block(monkeypatch):
    def fake_complete(prompt, system=None, temperature=None, top_p=None):
        return "<?php\nclass A {\n  public function x(): int {\n    return 1;\n  }\n}\n"

    monkeypatch.setattr(llm, "complete", fake_complete)

    from lsp.base import validate_and_repair_blocks

    answer = (
        "Here:\n\n```php\n<?php\nclass A {\n  public function x(): int {\n"
        "    return $missing;\n  }\n}\n```\n"
    )
    diag = _lsp_error("Undefined variable $missing", line=4)
    new_text, summary = validate_and_repair_blocks(
        Adapter(), answer, "write code", FailOnceClient(diag), max_retries=2
    )
    assert summary[0]["status"] == "repaired"
    assert summary[0]["language"] == "php"
    assert "return 1;" in new_text
    assert "return $missing;" not in new_text


def test_truncated_blocks_not_repaired(monkeypatch):
    calls = []

    def fake_complete(prompt, system=None, temperature=None, top_p=None):
        calls.append(prompt)
        return "x"

    monkeypatch.setattr(llm, "complete", fake_complete)

    from lsp.base import validate_and_repair_blocks

    md = "```php\n<?php\npublic function x() {\n    // ...\n}\n```\n"
    diag = _lsp_error("Undefined variable $x", line=3)
    _, summary = validate_and_repair_blocks(Adapter(), md, "q", FakeLspClient([diag]), max_retries=2)
    assert summary[0]["status"] == "warning"
    assert calls == []
