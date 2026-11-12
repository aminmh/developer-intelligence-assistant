"""PHP request-to-sink flows from Joern (navigation plan task 25, analyzer spec clause 31)."""

import base64

import pytest

from code_quality.external_analyzers import _PHP_FLOW_SCRIPT, _php_flow_findings
from code_quality.remote_worker import RemoteWorkerError, _validate_trace

FILES = ["app/Http/Controllers/SearchController.php", "app/Support/Query.php"]


def _line(kind, *steps):
    enc = lambda value: base64.b64encode(value.encode()).decode()  # noqa: E731
    return "\t".join([enc(kind)] + [enc(f"{path}\x1f{line}\x1f{code}") for path, line, code in steps]) + "\n"


def test_flows_become_findings_at_the_sink_with_their_trace(tmp_path):
    out = tmp_path / "flows.tsv"
    out.write_text(
        _line("sql", ("/tmp/x/languages/php/app/Http/Controllers/SearchController.php", 12, '$request->input("q")'),
              ("app/Http/Controllers/SearchController.php", 12, "$q"),
              ("app/Support/Query.php", 30, "DB::raw($sql)"))
        # the same source and sink again by another path: reported once
        + _line("sql", ("app/Http/Controllers/SearchController.php", 12, '$request->input("q")'),
                ("app/Support/Query.php", 30, "DB::raw($sql)"))
        + _line("command", ("app/Http/Controllers/SearchController.php", 40, '$_GET["host"]'),
                ("vendor/elsewhere.php", 3, "outside"))  # one step outside the revision: too short to report
        + _line("unknown", ("app/Support/Query.php", 1, "x"), ("app/Support/Query.php", 2, "y"))
        + "not-base64!\n")

    findings = _php_flow_findings(str(out), FILES, "4.0.664")

    assert len(findings) == 1
    finding = findings[0]
    assert (finding["rule"], finding["severity"], finding["file"], finding["line"]) == \
        ("joern:php-request-to-sql", "error", "app/Support/Query.php", 30)
    assert "SearchController.php:12" in finding["message"] and finding["tool"] == "joern"
    assert [(s["file"], s["line"]) for s in finding["trace"]] == [
        ("app/Http/Controllers/SearchController.php", 12), ("app/Http/Controllers/SearchController.php", 12),
        ("app/Support/Query.php", 30)]


def test_flow_script_is_application_owned_and_covers_the_spec_sinks():
    for sink in ("orderByRaw", "shell_exec", "eval", "unserialize", "file_get_contents", "away"):
        assert sink in _PHP_FLOW_SCRIPT
    assert "_(GET|POST|REQUEST|COOKIE|FILES|SERVER)" in _PHP_FLOW_SCRIPT
    assert "path.elements.take(15) :+ last" in _PHP_FLOW_SCRIPT  # long paths keep their sink


def test_worker_traces_are_checked_against_the_revision():
    counts = {"a.php": 40}
    good = [{"file": "a.php", "line": 3, "code": "$x"}, {"file": "a.php", "line": 9, "code": "DB::raw($x)"}]
    assert _validate_trace(good, {"a.php"}, counts) == good
    for bad in ([good[0]], [good[0], {"file": "b.php", "line": 1, "code": ""}],
                [good[0], {"file": "a.php", "line": 99, "code": ""}],
                [good[0], {"file": "a.php", "line": 2, "code": "x" * 161}], "trace"):
        with pytest.raises(RemoteWorkerError):
            _validate_trace(bad, {"a.php"}, counts)
