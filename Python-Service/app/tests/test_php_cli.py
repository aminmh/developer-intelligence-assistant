from __future__ import annotations

import subprocess

import pytest

from infra.php_cli import PhpCli, PhpCliError, _decode_json_array


def test_decode_json_array_accepts_clean_and_prefixed_output():
    assert _decode_json_array('[{"chunk_id":"1"}]') == [{"chunk_id": "1"}]
    assert _decode_json_array("warning\n[{\"chunk_id\":\"1\"}]\n") == [{"chunk_id": "1"}]
    assert _decode_json_array("not-json") is None


def test_code_parse_accepts_partial_exit_code(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0], returncode=2, stdout='[{"chunk_id":"1"}]', stderr="partial"
        )

    monkeypatch.setattr("infra.php_cli.subprocess.run", fake_run)
    result = PhpCli("/php").code_parse("/workspace")
    assert result == [{"chunk_id": "1"}]


def test_code_parse_reports_nonzero_process(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=7, stdout="", stderr="boom")

    monkeypatch.setattr("infra.php_cli.subprocess.run", fake_run)
    with pytest.raises(PhpCliError, match="boom"):
        PhpCli("/php").code_parse("/workspace")


def test_run_translates_start_and_timeout_errors(monkeypatch):
    def missing(*args, **kwargs):
        raise OSError("missing php")

    monkeypatch.setattr("infra.php_cli.subprocess.run", missing)
    with pytest.raises(PhpCliError, match="could not start"):
        PhpCli("/php").run("code-parse", [], timeout=1)

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr("infra.php_cli.subprocess.run", timed_out)
    with pytest.raises(PhpCliError, match="timed out"):
        PhpCli("/php").run("code-parse", [], timeout=1)
