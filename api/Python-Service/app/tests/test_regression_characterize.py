"""Characterization tests: @.archcore/regression/regression-check.spec.md clauses 19-21; plan tasks T17-T21."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import zipfile

import pytest

from agent.plugins.frameworks.laravel.test_templates import PEST, PHPUNIT
from agent.plugins.frameworks.symfony.test_templates import WEB_TEST_CASE
from regression.characterize import build_bundle, bundle_zip, quote, symbol_tests
from regression.probes import import_requests

ORDER = {"data": {"id": 1, "total": 12, "note": "O'Brien \\ said \"hi\"", "items": [{"sku": "cola", "price": 3}],
                  "tags": [], "updated_at": "2026-11-23T07:29:28.421Z", "dotted.key": 1}, "request_id": "ab12", "_token": "x"}


def _json(body, status=200):
    return {"status": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body).encode(),
            "body_sha256": "h", "error": None}


RECORDED = {
    "p1": _json(ORDER),
    "p2": _json({"data": {"id": 3, "total": 3}}, status=201),
    "p3": {"status": 200, "headers": {"content-type": "text/html"}, "error": None,
           "body": b"<html><head><meta name='csrf-token' content='t'><script>x()</script></head>"
                   b"<body><h1>Pizza shop</h1><p>2 orders</p></body></html>"},
    "p4": _json({"ok": True}),
}
PROBES = [
    {"probe_id": "p1", "order": 0, "method": "GET", "path": "/api/orders/1", "template": "/api/orders/1", "headers": {}, "body": None},
    {"probe_id": "p2", "order": 1, "method": "POST", "path": "/api/orders", "template": "/api/orders", "headers": {},
     "body": "{\"items\": [{\"sku\": \"cola\", \"price\": 3}]}"},
    {"probe_id": "p3", "order": 2, "method": "GET", "path": "/page", "template": "/page", "headers": {}, "body": None},
    {"probe_id": "p4", "order": 3, "method": "GET", "path": "/tested", "template": "/tested", "headers": {}, "body": None},
    {"probe_id": "p5", "order": 4, "method": "GET", "path": "/never-recorded", "template": "/never-recorded", "headers": {}, "body": None},
]
BASELINE = {"run_id": "run-b", "summary": {"noise": {"p1": ["body:$.request_id"], "p3": []}}}


def _check():
    return {"check_id": "rc_0123456789abcdef", "base_revision_id": "rev_a", "candidate_revision_id": "rev_b", "state": "done",
            "report": {"routes": [{"method": "GET", "path": "/api/orders/{id}", "depends_on": ["Shop::total"], "coverage": ["replay"],
                                   "static_coverage": [], "replay": {"probes": 1}},
                                  {"method": "GET", "path": "/tested", "depends_on": [], "static_coverage": ["test"]}],
                       "symbols": [{"fqn": "Shop::total", "canonical_symbol_id": "s1", "change": "modified", "coverage": []},
                                   {"fqn": "Shop::fee", "canonical_symbol_id": "s2", "change": "modified", "coverage": [],
                                    "language": "php"},
                                   {"fqn": "Shop::added", "canonical_symbol_id": "s3", "change": "added", "coverage": []}]}}


def _load(run_id, probe_id):
    assert run_id == "run-b"
    return RECORDED.get(probe_id)


def _php_lint(content: str, tmp_path, name: str) -> None:
    if not shutil.which("php"):
        pytest.skip("php CLI not available")
    path = tmp_path / name
    path.write_text(content)
    result = subprocess.run(["php", "-l", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr + content


# --- T17: templates ------------------------------------------------------------------------------------

def test_templates_follow_the_project_test_runner(tmp_path):
    from agent import default_registry
    from agent.core.capability import Capability
    from agent.core.revision import RevisionContext
    from agent.plugins.frameworks.laravel.test_templates import provide_test_templates

    (tmp_path / "composer.json").write_text(json.dumps({"require": {"laravel/framework": "^11.0"},
                                                        "require-dev": {"phpunit/phpunit": "^11"}}))
    (tmp_path / "artisan").write_text("#!/usr/bin/env php")
    context = RevisionContext.from_directory(tmp_path)
    assert provide_test_templates(context) == [PHPUNIT]
    collection = default_registry().collect(Capability.CHARACTERIZATION_TESTS, context)
    assert "laravel" in collection.providers and collection.records[0].runner == "phpunit"
    (tmp_path / "composer.json").write_text(json.dumps({"require": {"laravel/framework": "^11.0"},
                                                        "require-dev": {"pestphp/pest": "^3"}}))
    assert provide_test_templates(RevisionContext.from_directory(tmp_path)) == [PEST]


# --- T18: route tests from recordings ------------------------------------------------------------------

@pytest.mark.parametrize("template", [PHPUNIT, PEST, WEB_TEST_CASE], ids=lambda t: f"{t.framework}-{t.runner}")
def test_route_tests_pin_recorded_responses_minus_noise(template, tmp_path):
    bundle = build_bundle(_check(), PROBES, BASELINE, _load, {"ignore": ["$..price"]}, [template],
                          model_call=lambda *a, **k: (json.dumps({"code": "<?php\n// written by a model\n"}), "m", "local"))
    tests = next(f for f in bundle["files"] if f["kind"] == "route_tests")
    content = tests["content"]
    _php_lint(content, tmp_path, "RouteTest.php")
    assert tests["path"].endswith("RegressionCheck89Abcdef" + "Test.php") or "RegressionCheck" in tests["path"]
    assert tests["tests"] == 3  # p4's route already has a test; p5 was never recorded
    assert "'/api/orders/1'" in content and "'POST'" in content and "201" in content
    if template.uses_dot_path:
        assert "assertJsonPath('data.total', 12)" in content
        assert "assertJsonPath('data.items.0.sku', 'cola')" in content
        assert "dotted" not in content  # a key with a dot cannot be addressed by a dot path
    else:
        assert "self::assertSame(12, $data['data']['total']);" in content
        assert "$data['data']['dotted.key']" in content
    # Noise, ignore rules, shape-compared values and CSRF-like keys are left out; quotes are escaped.
    asserted = "\n".join(line for line in content.splitlines() if "assertJsonPath" in line or "$data[" in line)
    for absent in ("request_id", "updated_at", "price", "_token"):
        assert absent not in asserted
    assert quote(ORDER["data"]["note"]) in content
    assert "json_decode('[]', true)" in content  # an empty list stays a value
    assert "json_decode('{\"items\": [{\"sku\": \"cola\", \"price\": 3}]}', true)" in content
    assert "'Pizza shop'" in content and "csrf" not in content.lower().replace("csrf-like", "")
    reasons = {s.get("probe"): s["reason"] for s in bundle["skipped"]}
    assert reasons["GET /tested"] == "an existing test covers this route"
    assert reasons["GET /never-recorded"] == "no baseline recording"


# --- T19: symbol tests ---------------------------------------------------------------------------------

def test_symbol_tests_are_model_written_review_only_and_budgeted():
    prompts = []

    def model(prompt, **kwargs):
        prompts.append(json.loads(prompt))
        return json.dumps({"code": "```php\n<?php\n// Written by a model: review before trusting it.\n```"}), "m", "local"

    rows = [{"fqn": "App\\Shop::fee", "canonical_symbol_id": "s2", "language": "php"},
            {"fqn": "App\\Shop::tax", "canonical_symbol_id": "s9"},
            {"fqn": "App\\Shop::round", "canonical_symbol_id": "s3"}]
    bodies = {"s2": {"path": "app/Shop.php", "line": 3, "body": "function fee() { return 2; }"},
              "s3": {"path": "app/Shop.php", "line": 9, "body": "function round() {}"}}
    files, skipped = symbol_tests(rows, bodies, PHPUNIT, model_call=model, limit=1)
    assert [f["path"] for f in files] == ["tests/Characterization/ShopFeeCharacterizationTest.php"]
    assert files[0]["review_only"] is True and files[0]["content"].startswith("<?php")
    assert prompts[0]["base_source"]["body"].startswith("function fee") and prompts[0]["test_runner"] == "phpunit"
    assert skipped == [{"symbol": "App\\Shop::tax", "reason": "base source unavailable"},
                       {"symbol": "App\\Shop::round", "reason": "budget"}]

    def down(*_a, **_k):
        raise RuntimeError("no model")

    files, skipped = symbol_tests(rows[:1], bodies, PHPUNIT, model_call=down)
    assert files == [] and skipped[0]["reason"] == "the model could not be reached"


def test_symbol_tests_only_for_changed_code_no_route_covers():
    asked = []
    model = lambda prompt, **k: (asked.append(json.loads(prompt)["symbol"]) or json.dumps({"code": "<?php\n"}), "m", "local")
    bodies = {"s1": {"body": "a"}, "s2": {"body": "b"}, "s3": {"body": "c"}}
    build_bundle(_check(), PROBES, BASELINE, _load, {}, [PHPUNIT], symbol_bodies=bodies, model_call=model)
    # Shop::total is covered by a replayed route; Shop::added is new; only Shop::fee needs a model-written test.
    assert asked == ["Shop::fee"]


# --- T20: request file fallback ------------------------------------------------------------------------

def test_tests_from_capability_or_http_file():
    bundle = build_bundle(_check(), PROBES, BASELINE, _load, {}, [])
    kinds = [f["kind"] for f in bundle["files"]]
    assert kinds == ["readme", "request_file"] and bundle["template"] is None
    readme, request_file = bundle["files"]
    assert "No test template is available" in readme["content"]
    rows = json.loads(request_file["content"])
    assert rows[0]["expect"] == {"status": 200, "body_sha256": "h"} and rows[4]["expect"]["status"] is None
    # The request file is exactly what replay's importer accepts.
    imported = import_requests(request_file["content"], "http://host.docker.internal:8000")
    assert [(p["method"], p["path"]) for p in imported["probes"]] == [(p["method"], p["path"]) for p in PROBES]
    assert imported["probes"][1]["body"] == PROBES[1]["body"]


def test_tests_from_capability_or_http_file_without_a_baseline():
    bundle = build_bundle(_check(), PROBES, None, None, {}, [PHPUNIT], model_call=lambda *a, **k: ("{}", "m", "local"))
    assert "route_tests" not in [f["kind"] for f in bundle["files"]]
    assert bundle["skipped"][0]["reason"] == "no baseline recording: record a baseline first"


# --- T21: bundle and route -----------------------------------------------------------------------------

def test_bundle_zip_and_route(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from regression import http, replay_service
    from regression.recordings import Recordings
    from regression.store import RegressionStore

    secret = "s" * 40
    monkeypatch.setenv("RAG_CALLBACK_SECRET", secret)
    store = RegressionStore(tmp_path / "r.sqlite")
    recordings = Recordings(tmp_path / "rec")
    check = store.create("pizza", "rev_a", "rev_b")
    store.save("pizza", check["check_id"], state="done", report=_check()["report"])
    store.set_probes("pizza", check["check_id"], PROBES[:1])
    run = store.claim_run("pizza", check["check_id"], "baseline", "t")
    recordings.save("pizza", check["check_id"], run["run_id"], "p1", 1, RECORDED["p1"])
    store.finish_run("pizza", run["run_id"], "completed", {"noise": {"p1": []}})
    monkeypatch.setattr(http, "_store", lambda: store)
    real = replay_service.test_bundle
    monkeypatch.setattr(replay_service, "test_bundle", lambda *a, **k: real(
        *a, **{**k, "recordings": recordings, "material": lambda *_: ([PHPUNIT], {}),
               "model_call": lambda *_a, **_k: ("{}", "m", "local")}))
    app = FastAPI()
    app.include_router(http.router)
    client = TestClient(app)
    headers = {"x-internal-secret": secret, "x-user-id": "u1"}
    url = f"/projects/pizza/regression-checks/{check['check_id']}/tests"
    listed = client.post(url, headers=headers)
    assert listed.status_code == 200, listed.text
    assert [f["kind"] for f in listed.json()["files"]] == ["readme", "route_tests", "request_file"]
    zipped = client.post(url + "?format=zip", headers=headers)
    assert zipped.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
        names = archive.namelist()
    assert names[0] == "README-regression-tests.md" and any(n.startswith("tests/Characterization/") for n in names)
    pending = store.create("pizza", "rev_a", "rev_c")
    assert client.post(f"/projects/pizza/regression-checks/{pending['check_id']}/tests", headers=headers).status_code == 422
    assert bundle_zip({"files": []})  # an empty bundle is still a valid archive


def test_route_tests_run_on_the_recorded_database_without_reset_and_skip_own_urls():
    """Found live on pizza-builder: Pest.php applies RefreshDatabase to tests/Feature, seeders mint new ULIDs,
    and pagination links carry the host the request used."""
    paged = _json({"data": [{"id": "01M83PAZN1MT9DZ8CKBFQ1DAB1"}],
                   "links": {"first": "http://host.docker.internal:8201/api/v1/sizes?page=1", "next": None,
                             "docs": "https://example.com/docs"},
                   "meta": {"path": "http://localhost:8201/api/v1/sizes"}})
    baseline = {"run_id": "run-b", "summary": {"noise": {}, "target_origin": "http://host.docker.internal:8201"}}
    probes = [{"probe_id": "p1", "order": 0, "method": "GET", "path": "/api/v1/sizes", "template": "/api/v1/sizes",
               "headers": {}, "body": None}]
    bundle = build_bundle(_check(), probes, baseline, lambda _r, _p: paged, {}, [PEST], symbol_bodies={},
                          model_call=lambda *a, **k: ("{}", "m", "local"), notes=["Source was partial."])
    readme, tests = bundle["files"][0], bundle["files"][1]
    assert tests["path"].startswith("tests/Characterization/")  # outside ->in('Feature') and its RefreshDatabase
    assert "uses(Tests\\TestCase::class);" in tests["content"] and "RefreshDatabase" not in tests["content"]
    assert "assertJsonPath('data.0.id', '01M83PAZN1MT9DZ8CKBFQ1DAB1')" in tests["content"]  # same database: ids hold
    assert "8201" not in tests["content"] and "2 own-origin URL" in tests["content"]
    assert "assertJsonPath('links.docs', 'https://example.com/docs')" in tests["content"]  # other hosts still pinned
    assert "> Source was partial." in readme["content"] and "DB_DATABASE=" in readme["content"]
    assert "No test template is available" not in readme["content"]


# --- knowledge layer Phase 4, T16: version-exact guides as untrusted context ------------------------------

def test_framework_context_in_symbol_test_prompts_fetched_once_and_only_when_needed():
    prompts, calls = [], []
    block = {"note": "untrusted", "untrusted_documentation": [{"cite": "https://github.com/laravel/docs/blob/12.x/http-tests.md:9",
                                                               "text": "assertJsonPath"}]}

    def model(prompt, **kwargs):
        prompts.append((json.loads(prompt), kwargs["system"]))
        return json.dumps({"code": "<?php\n"}), "m", "local"

    def docs():
        calls.append(1)
        return block

    rows = [{"fqn": "App\\Shop::fee", "canonical_symbol_id": "s2"}, {"fqn": "App\\Shop::tax", "canonical_symbol_id": "s3"}]
    bodies = {"s2": {"body": "a"}, "s3": {"body": "b"}}
    symbol_tests(rows, bodies, PHPUNIT, model_call=model, documentation=docs)
    assert calls == [1] and all(p["framework_documentation"] == block for p, _ in prompts)
    assert "untrusted" in prompts[0][1]
    symbol_tests([], bodies, PHPUNIT, model_call=model, documentation=docs)
    assert calls == [1]  # no prompt, no lookup

    def broken():
        raise OSError("offline")

    files, _ = symbol_tests(rows[:1], bodies, PHPUNIT, model_call=model, documentation=broken)
    assert files and "framework_documentation" not in prompts[-1][0]
    # The bundle asks for the guides of the base version, which the tests describe.
    asked = []
    build_bundle(_check(), PROBES, BASELINE, _load, {}, [PHPUNIT], symbol_bodies={"s2": {"body": "a"}}, model_call=model,
                 documentation=lambda: asked.append(1) or block)
    assert asked == [1] and prompts[-1][0]["framework_documentation"] == block


# --- knowledge layer Phase 4, T17: verified ranges ----------------------------------------------------

def test_verified_range_of_each_template():
    from regression.characterize import template_range_note

    assert PEST.package == "laravel/framework" and PEST.verified_range == ("12.0", "13.0")  # pizza-builder, 12.69.1
    assert PHPUNIT.verified_range == () and WEB_TEST_CASE.verified_range == ()  # never run against a real project
    assert WEB_TEST_CASE.package == "symfony/framework-bundle"
    assert template_range_note(PEST, {"laravel/framework": "12.69.1"}) is None
    newer = template_range_note(PEST, {"laravel/framework": "13.0.2"})
    assert "verified on laravel/framework 12.0 up to (not including) 13.0" in newer and "installs 13.0.2" in newer
    assert "version is unknown" in template_range_note(PEST, {})
    assert "has not been run against a real project" in template_range_note(WEB_TEST_CASE, {"symfony/framework-bundle": "7.4.0"})


def test_verified_range_outside_marks_the_bundle_partial():
    def model(*_a, **_k):
        return json.dumps({"code": "<?php\n"}), "m", "local"

    inside = build_bundle(_check(), PROBES, BASELINE, _load, {}, [PEST], model_call=model,
                          installed={"laravel/framework": "12.69.1"})
    assert inside["template_status"] == "verified" and inside["partial"] == []
    outside = build_bundle(_check(), PROBES, BASELINE, _load, {}, [PEST], model_call=model,
                           installed={"laravel/framework": "13.1.0"})
    assert outside["template_status"] == "partial" and outside["partial"][0]["capability"] == "characterization_tests"
    readme = outside["files"][0]["content"]
    assert "installs 13.1.0" in readme and [f["kind"] for f in outside["files"]][1] == "route_tests"  # still generated
    assert build_bundle(_check(), PROBES, BASELINE, _load, {}, [])["template_status"] is None


def test_verified_range_from_the_base_revision_lockfile(tmp_path):
    from regression import replay_service
    from regression.recordings import Recordings
    from regression.store import RegressionStore

    store = RegressionStore(tmp_path / "r.sqlite")
    check = store.create("pizza", "rev_a", "rev_b")
    store.save("pizza", check["check_id"], state="done", report=_check()["report"])
    model = lambda *_a, **_k: (json.dumps({"code": "<?php\n"}), "m", "local")  # noqa: E731
    bundle = replay_service.test_bundle(store, "pizza", check["check_id"], recordings=Recordings(tmp_path / "rec"),
                                        material=lambda *_: ([PEST], {}, {"laravel/framework": "11.9.0"}),
                                        model_call=model, docs=lambda *_: None)
    assert bundle["template_status"] == "partial" and any("installs 11.9.0" in note for note in bundle["notes"])
    legacy = replay_service.test_bundle(store, "pizza", check["check_id"], recordings=Recordings(tmp_path / "rec"),
                                        material=lambda *_: ([PEST], {}), model_call=model, docs=lambda *_: None)
    assert any("version is unknown" in note for note in legacy["notes"])
