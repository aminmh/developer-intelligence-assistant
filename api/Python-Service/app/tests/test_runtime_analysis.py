import json
import zipfile
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from telemetry.analysis import AnalysisError, analyze_request, preview_diff


@pytest.fixture(autouse=True)
def _remote_source_allowed(monkeypatch):
    """These tests use the remote provider for a project that allows it (see test_remote_consent.py)."""
    import infra.remote_consent as consent

    monkeypatch.setattr(consent, "remote_source_allowed", lambda project_id, **kwargs: True)


def _archive(path="src/OrderService.php", contents="<?php\nclass OrderService {\n    public function run() { return 1; }\n}\n"):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(path, contents)
    return buffer.getvalue()


def _detail():
    return {
        "available": True,
        "request": {"method": "POST", "route": "/orders", "status_code": 500, "duration_ms": 40,
                    "php_cpu_time_us": 1200, "php_peak_memory_bytes": 800000},
        "database_calls": [{"duration_ms": 5, "db_system": "mysql", "db_namespace": "shop",
                            "operation": "SELECT", "table_ids": ["table:mysql.orders"],
                            "symbol_id": "OrderRepository::find", "caller_symbol_id": "OrderService::run",
                            "db.query.text": "SELECT private"}],
        "method_activity": {"edges": [{"caller_symbol_id": "OrderService::run", "symbol_id": "OrderRepository::find", "calls": 2, "source": "hook"}]},
        "ai_summary": {"php_app_time_ms": 30, "php_cpu_time_ms": 1.2, "php_peak_memory_bytes": 800000,
                       "excimer_hotspots": [{"symbol_id": "OrderService::run", "sample_percent": 60, "sample_count": 3}]},
    }


def _definitions(_project, _commit):
    return [{"symbol_id": "OrderService::run", "kind": "method", "fqn": "OrderService::run",
             "file": "src/OrderService.php", "line": 3, "end_line": 3},
            {"symbol_id": "OrderRepository::find", "kind": "method", "fqn": "OrderRepository::find",
             "file": "src/OrderService.php", "line": 3, "end_line": 3}]


def test_analysis_sends_only_allowlisted_evidence_and_reports_actual_provider():
    archive = _archive(contents="<?php\n// api_key = 'credential-value-123'\nclass OrderService {\n    public function run() { return 1; }\n}\n")
    prompts = []

    def model(prompt, **kwargs):
        prompts.append((prompt, kwargs))
        result = {"findings": [{"title": "Slow request", "summary": "Database activity is observed.",
                               "recommendations": ["Inspect the query path."], "severity": "medium",
                               "evidence_refs": ["OrderService::run"], "source_refs": ["OrderService::run"]}],
                  "coverage_gaps": []}
        return json.dumps(result), "local-chat", "local"

    result = analyze_request(
        "pizza", {"commit_sha": "a" * 40, "revision_id": "rev_" + "b" * 32, "archive_key": "archive"},
        _detail(), "remote", definitions_loader=_definitions, archive_loader=lambda _key: archive,
        model_call=model,
    )

    submitted = prompts[0][0]
    assert "db.query.text" not in submitted and "SELECT private" not in submitted
    assert "credential-value-123" not in submitted and "[REDACTED]" in submitted
    assert result["provider"] == "local" and result["requested_provider"] == "remote"
    assert result["model"] == "local-chat"
    assert result["findings"][0]["source_refs"] == ["OrderService::run"]
    assert "No database spans" not in " ".join(result["coverage_gaps"])


def test_analysis_allows_missing_source_and_marks_diff_unavailable():
    def model(_prompt, **_kwargs):
        return json.dumps({"findings": [], "coverage_gaps": []}), "local-chat", "local"

    result = analyze_request(
        "pizza", {"commit_sha": "a" * 40, "revision_id": "rev_" + "b" * 32}, _detail(), "local",
        definitions_loader=lambda *_args: [], archive_loader=lambda _key: (_ for _ in ()).throw(KeyError()),
        model_call=model,
    )
    assert result["source_available"] is False
    assert any("source mapping is unavailable" in gap for gap in result["coverage_gaps"])


def test_analysis_rejects_invalid_model_output():
    with pytest.raises(AnalysisError, match="invalid structured data"):
        analyze_request(
            "pizza", {"commit_sha": "a" * 40, "revision_id": "rev_" + "b" * 32}, _detail(), "local",
            definitions_loader=_definitions, archive_loader=lambda _key: _archive(),
            model_call=lambda *_args, **_kwargs: ("not json", "model", "local"),
        )


def test_diff_preview_is_revision_pinned_and_rejects_out_of_scope_paths(monkeypatch):
    symbol_id = "OrderService::run"
    finding = {"id": "finding-1", "title": "Improve", "summary": "Use a faster query.",
               "recommendations": [], "evidence_refs": [symbol_id], "source_refs": [symbol_id],
               "severity": "low"}
    analysis = {"findings": [finding], "requested_provider": "local"}
    monkeypatch.setattr("telemetry.runtime.snapshot_definitions", lambda *_args: _definitions("pizza", "a" * 40))
    revision = {"project_id": "pizza", "commit_sha": "a" * 40, "revision_id": "rev_" + "b" * 32, "archive_key": "archive"}

    with pytest.raises(AnalysisError, match="invalid or out-of-scope"):
        preview_diff(revision, finding, analysis, _detail(), archive_loader=lambda _key: _archive(),
                     model_call=lambda *_args, **_kwargs: (json.dumps({"file_path": "../secrets.php", "old_text": "return 1;", "new_text": "return 2;"}), "model", "local"))


def test_diff_preview_returns_read_only_unified_diff(monkeypatch):
    symbol_id = "OrderService::run"
    finding = {"id": "finding-1", "title": "Improve", "summary": "Use a faster query.",
               "recommendations": [], "evidence_refs": [symbol_id], "source_refs": [symbol_id],
               "severity": "low"}
    analysis = {"findings": [finding], "requested_provider": "remote"}
    monkeypatch.setattr("telemetry.runtime.snapshot_definitions", lambda *_args: _definitions("pizza", "a" * 40))
    result = preview_diff(
        {"project_id": "pizza", "commit_sha": "a" * 40, "revision_id": "rev_" + "b" * 32, "archive_key": "archive"},
        finding, analysis, _detail(), archive_loader=lambda _key: _archive(),
        model_call=lambda _prompt, **kwargs: (json.dumps({"file_path": "src/OrderService.php", "old_text": "return 1;", "new_text": "return 2;"}), "remote-chat", kwargs["provider"]),
    )
    assert "-    public function run() { return 1; }" in result["diff"]
    assert result["revision_id"] == "rev_" + "b" * 32
    assert result["provider"] == "remote"
    assert result["applied"] is False


def test_analysis_routes_require_internal_auth_and_an_indexed_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("SETTINGS_DB_PATH", str(tmp_path / "settings.db"))
    from telemetry import http
    from telemetry.registry import SnapshotRegistry

    monkeypatch.setattr(http, "get_registry", lambda: SnapshotRegistry(tmp_path))
    monkeypatch.setenv("RAG_CALLBACK_SECRET", "s" * 40)
    app = FastAPI()
    app.include_router(http.runtime_router)
    client = TestClient(app)
    base = "/telemetry/runtime/projects/pizza/revisions/rev_" + "a" * 32 + "/requests/" + "b" * 32

    assert client.post(base + "/analysis", json={"provider": "local"}).status_code == 401
    headers = {"X-Internal-Secret": "s" * 40}
    assert client.post(base.replace("rev_" + "a" * 32, "bad" ) + "/analysis", headers=headers,
                       json={"provider": "local"}).status_code == 422
    assert client.post(base + "/analysis", headers=headers, json={"provider": "local"}).status_code == 404
    assert client.post(base + "/analysis/diff", headers=headers, json={}).status_code == 404


def test_database_calls_are_grouped_by_query_shape_with_counts_and_durations():
    from telemetry.analysis import _group_database_calls

    calls = [{"duration_ms": ms, "db_system": "mysql", "db_namespace": "shop", "operation": "SELECT",
              "table_ids": ["table:mysql.cache"], "symbol_id": "Cache::get", "caller_symbol_id": "App::run",
              "span_status": "Unset", "db.query.text": "SELECT private"} for ms in (1.234, 0.5, 3.0)]
    calls.append({"duration_ms": 9, "db_system": "mysql", "db_namespace": "shop", "operation": "UPDATE",
                  "table_ids": ["table:mysql.sessions"], "span_status": "Error"})
    rows, common = _group_database_calls(calls)

    assert common == {"db_system": "mysql", "db_namespace": "shop"}
    assert len(rows) == 2
    select = rows[0]
    assert (select["n"], select["ms"], select["total_ms"]) == (3, [3.0, 1.23, 0.5], 4.73)
    assert "span_status" not in select and "db_system" not in select and "db.query.text" not in json.dumps(rows)
    assert rows[1]["span_status"] == "Error" and rows[1]["n"] == 1
    mixed, none_common = _group_database_calls([{"duration_ms": 1, "db_system": "mysql"}, {"duration_ms": 2, "db_system": "pgsql"}])
    assert none_common == {} and all("db_system" in row for row in mixed)


def test_source_windows_are_tight_skip_blank_lines_and_do_not_repeat_shared_lines():
    from telemetry.analysis import _source_context

    code = "\n".join(["<?php"] + [f"// filler {number}" for number in range(2, 41)]
                     + ["    public function one()", "    {", "", "        return 1;", "    }", "",
                        "    public function two()", "    {", "        return 2;", "    }"] + ["// tail"] * 10)
    lines = code.split("\n")
    first = next(i for i, text in enumerate(lines, 1) if "function one" in text)
    second = next(i for i, text in enumerate(lines, 1) if "function two" in text)
    definitions = {
        "A::one": {"symbol_id": "A::one", "file": "a.php", "line": first, "end_line": first + 4},
        "A::two": {"symbol_id": "A::two", "file": "a.php", "line": second, "end_line": second + 3},
    }
    out = _source_context(_archive("a.php", code), definitions, ["A::one", "A::two"])

    one_numbers = [int(row.split(":", 1)[0]) for row in out["A::one"]["source"].split("\n")]
    assert one_numbers[0] == first - 4, "four lines above"
    assert one_numbers[-1] == first + 4, "the line below the method is blank, so it is omitted"
    assert all(lines[n - 1].strip() for n in one_numbers), "no blank lines are sent"
    two_numbers = [int(row.split(":", 1)[0]) for row in out["A::two"]["source"].split("\n")]
    assert not set(one_numbers) & set(two_numbers), "lines already sent with the first symbol are not repeated"
    assert second in two_numbers and second + 3 in two_numbers


def test_analysis_prompt_is_compact_json_and_states_its_layout():
    prompts = []

    def model(prompt, **_kwargs):
        prompts.append(prompt)
        return json.dumps({"findings": [], "coverage_gaps": []}), "m", "local"

    analyze_request("pizza", {"commit_sha": "a" * 40, "revision_id": "rev_" + "b" * 32}, _detail(), "local",
                    definitions_loader=_definitions, archive_loader=lambda _key: _archive(), model_call=model)
    body = json.loads(prompts[0])
    assert '": ' not in prompts[0].replace("\\\": ", ""), "no padding after separators"
    assert "one row per distinct query shape" in body["task"]
    evidence = body["evidence"]
    assert evidence["database"] == {"db_system": "mysql", "db_namespace": "shop"}
    assert evidence["database_calls"][0]["n"] == 1 and evidence["method_activity_source"] == "hook"
    assert all("source" not in edge for edge in evidence["method_activity"])
