"""Request analysis gets the grouped database picture, never SQL, and tags database findings."""
import json
import zipfile
from io import BytesIO

from telemetry.analysis import analyze_request
from telemetry.runtime import _database_summary

SETTING = "App\\Models\\Setting::allCached"
GET_BY_KEY = "App\\Models\\Setting::getByKey"
REVISION = {"commit_sha": "a" * 40, "revision_id": "rev_" + "b" * 32, "archive_key": "archive"}


def _archive():
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("app/Models/Setting.php", "<?php\nclass Setting {\n    public static function allCached() { return 1; }\n"
                         "    public static function getByKey($key) { return self::allCached(); }\n}\n")
    return buffer.getvalue()


def _definitions(_project, _commit):
    return [{"symbol_id": SETTING, "fqn": SETTING, "file": "app/Models/Setting.php", "line": 3, "end_line": 3},
            {"symbol_id": GET_BY_KEY, "fqn": GET_BY_KEY, "file": "app/Models/Setting.php", "line": 4, "end_line": 4}]


def _detail():
    calls = [{"span_id": f"{index:016x}", "operation": "SELECT", "table_ids": ["table:mysql.cache"], "duration_ms": 0.3,
              "symbol_id": SETTING, "caller_symbol_id": GET_BY_KEY, "db.query.text": "select * from cache where key = ?"}
             for index in range(11)]
    calls.append({"span_id": "f" * 16, "operation": "UPDATE", "table_ids": ["table:mysql.sessions"], "duration_ms": 2.0,
                  "symbol_id": None, "caller_symbol_id": None})
    return {"available": True, "request": {"method": "GET", "route": "/pizzas/{pizza}", "status_code": 200, "duration_ms": 40},
            "database_calls": calls, "database_summary": _database_summary(calls, 40.0), "method_activity": {"edges": []}}


def _run(response: dict):
    prompts = []

    def model(prompt, **_kwargs):
        prompts.append(prompt)
        return json.dumps(response), "local-chat", "local"

    result = analyze_request("pizza", REVISION, _detail(), "local", definitions_loader=_definitions,
                             archive_loader=lambda _key: _archive(), model_call=model)
    return result, json.loads(prompts[0])


def test_database_summary_reaches_the_model_without_sql():
    _result, prompt = _run({"findings": [], "coverage_gaps": []})

    database = prompt["evidence"]["database_summary"]
    assert database["query_count"] == 12
    assert database["repeated_groups"] == 1
    first = database["groups"][0]
    assert (first["symbol_id"], first["count"], first["repeated"], first["caller_symbol_ids"]) == (SETTING, 11, True, [GET_BY_KEY])
    assert "span_ids" not in first
    assert first["framework"] is False
    sessions = next(group for group in database["groups"] if group["table_ids"] == ["table:mysql.sessions"])
    assert sessions["framework"] is True
    assert (database["framework_query_count"], database["app_query_count"]) == (1, 11)
    assert "select * from cache" not in json.dumps(prompt)
    assert "repeated_min" in prompt["task"]
    # The method behind the repeated query has its source supplied.
    assert {item["symbol_id"] for item in prompt["evidence"]["source"]} >= {SETTING, GET_BY_KEY}


def test_findings_keep_a_known_category_and_database_refs():
    result, _prompt = _run({"findings": [
        {"title": "Settings read 11 times", "summary": "Likely read again per key.", "recommendations": ["Keep the settings for the request."],
         "severity": "medium", "category": "database", "evidence_refs": [GET_BY_KEY, "Unknown::method"], "source_refs": [SETTING]},
        {"title": "Other", "summary": "x", "recommendations": [], "severity": "low", "category": "made-up",
         "evidence_refs": [], "source_refs": []},
    ], "coverage_gaps": []})

    database, other = result["findings"]
    assert database["category"] == "database"
    assert database["evidence_refs"] == [GET_BY_KEY]
    assert database["source_refs"] == [SETTING]
    assert other["category"] == "general"


def test_no_database_summary_sends_null():
    detail = _detail()
    detail["database_summary"] = {"query_count": 0, "groups": []}
    detail["database_calls"] = []
    prompts = []
    analyze_request("pizza", REVISION, detail, "local", definitions_loader=_definitions, archive_loader=lambda _key: _archive(),
                    model_call=lambda prompt, **_k: (prompts.append(prompt), (json.dumps({"findings": []}), "m", "local"))[1])
    assert json.loads(prompts[0])["evidence"]["database_summary"] is None


def test_model_failure_is_logged_without_the_prompt(caplog):
    import pytest as _pytest
    from telemetry.analysis import AnalysisError

    def failing(_prompt, **_kwargs):
        try:
            raise ConnectionRefusedError("connection refused on host.docker.internal:1234")
        except ConnectionRefusedError as cause:
            raise RuntimeError("Connection error.") from cause

    with caplog.at_level("WARNING"), _pytest.raises(AnalysisError):
        analyze_request("pizza", REVISION, _detail(), "local", definitions_loader=_definitions,
                        archive_loader=lambda _key: _archive(), model_call=failing)

    logged = caplog.text
    assert "connection refused on host.docker.internal:1234" in logged
    assert "provider=local" in logged
    assert "Setting" not in logged and "evidence" not in logged
