"""Regression check: store, revision pair, static evidence.

@.archcore/regression/regression-check.spec.md clauses 1-3, 6-9 and failure clauses 1-2;
Phase 1 of @.archcore/regression/implement-regression-check.plan.md.
"""

from __future__ import annotations

import pytest

from regression import service
from regression.model import NOT_COVERED, VERDICTS, verdict_counts
from regression.service import (RegressionRequestError, RegressionUnavailable, contract_changes, create_check,
                                resolve_pair, run_static)
from regression.store import CheckNotFound, RegressionStore

READY = {"state": "ready", "commit_sha": "c" * 40}


class Registry:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else {
            ("pizza", "rev_a"): {**READY, "revision_id": "rev_a"},
            ("pizza", "rev_b"): {**READY, "revision_id": "rev_b"},
            ("pizza", "rev_busy"): {**READY, "revision_id": "rev_busy", "state": "indexing"},
            ("pizza", "rev_zip"): {"state": "ready", "revision_id": "rev_zip", "commit_sha": None},
            ("other", "rev_x"): {**READY, "revision_id": "rev_x"},
        }

    def get_by_revision_id(self, project_id, revision_id):
        row = self.rows.get((project_id, revision_id))
        return dict(row) if row else None


def _impact_report(coverage="complete"):
    return {
        "revision_id": "rev_b", "base_revision_id": "rev_a", "coverage": coverage, "coverage_reasons": [],
        "truncated": False, "limit_reached": None, "unresolved": [], "runtime": {"observed": False},
        "changed": [{"symbol_id": "g1", "canonical_symbol_id": "s-find", "fqn": "App\\Repo::find", "kind": "method",
                     "language": "php", "path": "app/Repo.php", "line": 10, "change": "modified",
                     "revision_id": "rev_b"}],
        "impacted": [{"symbol_id": "g2", "canonical_symbol_id": "s-show", "fqn": "App\\OrderController::show",
                      "path": "app/OrderController.php", "line": 20, "depth": 1, "via": ["App\\Repo::find"],
                      "confirmed": None, "test": False}],
        "routes": [{"source": "static", "method": "GET", "path": "/orders/{id}", "fqn": "App\\OrderController::show",
                    "depth": 1, "handler_path": "app/OrderController.php", "handler_line": 20}],
        "tests": [],
    }


class _Revision:
    definitions: dict = {}

    @staticmethod
    def archive_loader():
        return b""


def _run_static(project_id, check_id, **kwargs):
    """run_static with no model, archive or plugin access unless a test passes its own."""
    kwargs.setdefault("revisions", lambda *_: _Revision())
    kwargs.setdefault("reviewer", lambda *_: {"reviewed": 0})
    kwargs.setdefault("evidence", lambda *_: ({}, {}))
    return run_static(project_id, check_id, **kwargs)


# --- T1: store ----------------------------------------------------------------------------------------

def test_store_creates_lists_saves_and_scopes_checks(tmp_path):
    store = RegressionStore(tmp_path / "regression.sqlite")
    check = store.create("pizza", "rev_a", "rev_b", created_by="u1")
    assert check["state"] == "pending" and check["check_id"].startswith("rc_")
    assert check["report"]["routes"] == [] and check["report"]["tier"] == "static"
    saved = store.save("pizza", check["check_id"], state="done", report={**check["report"], "notes": ["x"]})
    assert saved["state"] == "done" and saved["report"]["notes"] == ["x"]
    assert [c["check_id"] for c in store.list("pizza")] == [check["check_id"]]
    assert store.list("other") == []
    with pytest.raises(CheckNotFound):
        store.get("other", check["check_id"])
    with pytest.raises(ValueError):
        store.save("pizza", check["check_id"], state="bogus")
    store.delete("pizza", check["check_id"])
    with pytest.raises(CheckNotFound):
        store.get("pizza", check["check_id"])


def test_store_verdict_counts_cover_every_verdict():
    counts = verdict_counts({"routes": [{"verdict": NOT_COVERED}], "symbols": [{"verdict": None}]})
    assert set(counts) == set(VERDICTS) and counts[NOT_COVERED] == 1


# --- T2: clause 1, failure clause 1 -------------------------------------------------------------------

def test_rejects_unready_or_foreign_revisions(tmp_path):
    registry = Registry()
    base, candidate = resolve_pair("pizza", "rev_a", "rev_b", registry)
    assert (base["revision_id"], candidate["revision_id"]) == ("rev_a", "rev_b")
    with pytest.raises(RegressionUnavailable, match="not ready"):
        resolve_pair("pizza", "rev_a", "rev_busy", registry)
    with pytest.raises(RegressionUnavailable, match="does not exist"):
        resolve_pair("pizza", "rev_x", "rev_b", registry)  # a revision of another project
    with pytest.raises(RegressionUnavailable, match="no commit"):
        resolve_pair("pizza", "rev_zip", "rev_b", registry)
    with pytest.raises(RegressionRequestError):
        resolve_pair("pizza", "rev_a", "rev_a", registry)
    with pytest.raises(RegressionRequestError):
        resolve_pair("pizza", None, "rev_b", registry)
    store = RegressionStore(tmp_path / "r.sqlite")
    with pytest.raises(RegressionUnavailable):
        create_check("pizza", "rev_a", "rev_busy", store=store, registry=registry)
    assert store.list("pizza") == []


def test_rejects_unready_or_foreign_revisions_before_reading_the_graph(tmp_path):
    store = RegressionStore(tmp_path / "r.sqlite")
    registry = Registry()
    check = create_check("pizza", "rev_a", "rev_b", store=store, registry=registry)
    registry.rows[("pizza", "rev_b")] = {**READY, "revision_id": "rev_b", "state": "deleting"}
    calls = []
    result = _run_static("pizza", check["check_id"], store=store, registry=registry,
                        impact=lambda *a: calls.append(a) or _impact_report(), contracts=Contracts())
    assert result["state"] == "failed" and "not ready" in result["error"]
    assert calls == []


# --- T3: clauses 2-3, failure clause 2 ----------------------------------------------------------------

class Contracts:
    def __init__(self, versions=None):
        self.versions = versions or {}

    def list_attachments(self, project_id=None):
        return [{"source_id": "src1", "role": "provider"}, {"source_id": "src2", "role": "consumer"}] \
            if self.versions else []

    def version_for_revision(self, source_id, revision_id):
        version = self.versions.get((source_id, revision_id))
        return {"state": "valid", "version_id": version} if version else None

    def get_version(self, version_id, with_model=True):
        ops = {"GET /orders/{id}": {"responses": {"200": {}}}}
        if version_id == "v2":
            ops = {}
        return {"version_id": version_id, "model": {"operations": ops, "schemas": {}}}


def test_static_evidence_from_impact_and_contracts(tmp_path):
    store = RegressionStore(tmp_path / "r.sqlite")
    registry = Registry()
    check = create_check("pizza", "rev_a", "rev_b", store=store, registry=registry)
    seen = []

    def impact(project_id, revision_id, args):
        seen.append((project_id, revision_id, args))
        return _impact_report()

    contracts = Contracts({("src1", "rev_a"): "v1", ("src1", "rev_b"): "v2", ("src2", "rev_a"): "c1",
                           ("src2", "rev_b"): "c2"})
    result = _run_static("pizza", check["check_id"], store=store, registry=registry, impact=impact,
                        contracts=contracts)
    assert result["state"] == "done", result["error"]
    assert seen == [("pizza", "rev_b", {"base_revision_id": "rev_a"})]
    report = result["report"]
    assert [s["fqn"] for s in report["symbols"]] == ["App\\Repo::find"]
    assert report["symbols"][0]["change"] == "modified" and report["symbols"][0]["path"] == "app/Repo.php"
    assert [(r["method"], r["path"]) for r in report["routes"]] == [("GET", "/orders/{id}")]
    assert report["impacted"][0]["via"] == ["App\\Repo::find"]
    assert [(c["code"], c["source_id"]) for c in report["contract_changes"]] == [("operation_removed", "src1")]
    assert report["coverage"] == "full"


def test_static_evidence_unavailable_impact_gives_no_unchanged_verdict(tmp_path):
    store = RegressionStore(tmp_path / "r.sqlite")
    registry = Registry()
    check = create_check("pizza", "rev_a", "rev_b", store=store, registry=registry)
    result = _run_static("pizza", check["check_id"], store=store, registry=registry,
                        impact=lambda *a: _impact_report("unavailable"), contracts=Contracts())
    report = result["report"]
    assert report["coverage"] == "unavailable"
    assert {s["verdict"] for s in report["symbols"]} == {NOT_COVERED}
    assert report["notes"]


def test_static_evidence_impact_error_fails_the_check(tmp_path):
    store = RegressionStore(tmp_path / "r.sqlite")
    registry = Registry()
    check = create_check("pizza", "rev_a", "rev_b", store=store, registry=registry)

    def boom(*_):
        raise LookupError("graph store down")

    result = _run_static("pizza", check["check_id"], store=store, registry=registry, impact=boom, contracts=Contracts())
    assert result["state"] == "failed" and "graph store down" in result["error"]


def test_contract_changes_tolerate_a_broken_store():
    class Broken:
        def list_attachments(self, project_id=None):
            raise OSError("db locked")

    assert contract_changes("pizza", "rev_a", "rev_b", Broken()) == []


def test_service_dependencies_are_swappable():
    assert callable(service._run_impact) and callable(service._registry)


# --- T5: clauses 6-9 ----------------------------------------------------------------------------------

class _Collection:
    def __init__(self, name, records=(), status="complete", gaps=(), detected=("laravel",)):
        self.name, self.records, self._status = name, list(records), status
        self.gaps, self.detected = list(gaps), list(detected)

    def coverage(self):
        return {"capability": self.name, "status": self._status, "providers": [], "gaps": self.gaps,
                "detected_frameworks": self.detected, "records": len(self.records)}


def _reviewed(project_id, report, *_):
    for row in report["symbols"]:
        row["verdict"] = "suspected_regression" if row["fqn"] == "App\\Repo::find" else "unchanged"
    return {"reviewed": len(report["symbols"])}


def test_tiers_uncovered_and_partial(tmp_path):
    from agent.core.capability import Capability
    from agent.core.records import RunHintRecord

    store = RegressionStore(tmp_path / "r.sqlite")
    registry = Registry()
    check = create_check("pizza", "rev_a", "rev_b", store=store, registry=registry)
    hint = RunHintRecord(framework="laravel", kind="command", name="php artisan migrate --seed", detail="Seed.",
                         file="database/migrations")
    collections = {
        Capability.TEST_CONVENTIONS: _Collection("test_conventions", status="unavailable", detected=()),
        Capability.RUN_HINTS: _Collection("run_hints", [hint]),
    }
    files = {"docker-compose.yml": "services:\n  app:\n    build: .\n  db:\n    image: mysql\nvolumes:\n  x: {}\n",
             ".env.example": "APP_KEY=\nDB_HOST=127.0.0.1\n", "Dockerfile": "FROM php\nEXPOSE 8080\n"}
    result = _run_static("pizza", check["check_id"], store=store, registry=registry,
                         impact=lambda *a: _impact_report(), contracts=Contracts(), reviewer=_reviewed,
                         evidence=lambda *a: (collections, files))
    report = result["report"]
    # Clause 8: no replay target yet, so the tier is static and static evidence is still there.
    assert result["state"] == "done" and report["tier"] == "static"
    # Clause 6: the route takes its changed symbol's verdict and, with no test or span, is uncovered.
    route = report["routes"][0]
    assert route["verdict"] == "suspected_regression" and route["depends_on"] == ["App\\Repo::find"]
    assert {(u["kind"], u["verdict"]) for u in report["uncovered"]} == {("route", "not_covered"),
                                                                         ("symbol", "not_covered")}
    # Clause 7: a capability no plugin provided is listed with what it takes away.
    assert [p["capability"] for p in report["partial"]] == ["test_conventions"]
    assert "No framework plugin was detected" in report["partial"][0]["reasons"][0]
    # Clause 9: checklist from the plugin's hints plus plain project files.
    checklist = {(item["kind"], item["name"]) for item in report["baseline_checklist"]}
    assert {("command", "php artisan migrate --seed"), ("command", "docker compose up"), ("service", "app"),
            ("service", "db"), ("port", "8080"), ("env", "APP_KEY")} <= checklist
    assert ("service", "x") not in checklist and ("env", "DB_HOST") not in checklist


def test_tiers_uncovered_and_partial_tests_and_runtime_count_as_coverage(tmp_path):
    from agent.core.capability import Capability
    from agent.core.records import TestRecord

    store = RegressionStore(tmp_path / "r.sqlite")
    registry = Registry()
    check = create_check("pizza", "rev_a", "rev_b", store=store, registry=registry)
    test = TestRecord(framework="phpunit", file="tests/OrderTest.php", test_class="Tests\\OrderTest",
                      methods=("test_show",), references=("/orders/1",))
    collections = {Capability.TEST_CONVENTIONS: _Collection("test_conventions", [test])}
    result = _run_static("pizza", check["check_id"], store=store, registry=registry,
                         impact=lambda *a: _impact_report(), contracts=Contracts(), reviewer=_reviewed,
                         evidence=lambda *a: (collections, {}))
    report = result["report"]
    assert report["routes"][0]["coverage"] == ["test"]
    assert report["symbols"][0]["coverage"] == ["route"]
    assert report["uncovered"] == [] and report["partial"] == []


def test_tiers_uncovered_and_partial_evidence_failure_is_stated(tmp_path):
    store = RegressionStore(tmp_path / "r.sqlite")
    registry = Registry()
    check = create_check("pizza", "rev_a", "rev_b", store=store, registry=registry)

    def broken(*_):
        raise OSError("archive gone")

    result = _run_static("pizza", check["check_id"], store=store, registry=registry,
                         impact=lambda *a: _impact_report(), contracts=Contracts(), evidence=broken)
    report = result["report"]
    assert result["state"] == "done"
    assert any("could not be read" in note for note in report["notes"])
    assert {s["verdict"] for s in report["symbols"]} == {"not_covered"}  # unreviewed is never unchanged


def test_laravel_provides_run_hints(tmp_path):
    from agent.core.revision import RevisionContext
    from agent.plugins.frameworks.laravel.run_hints import provide_run_hints

    (tmp_path / "composer.json").write_text("{}")
    (tmp_path / ".env.example").write_text("APP_NAME=x\nAPP_KEY=\nDB_HOST=127.0.0.1\n")
    (tmp_path / "database" / "migrations").mkdir(parents=True)
    (tmp_path / "database" / "migrations" / "0001_create.php").write_text("<?php")
    (tmp_path / "database" / "seeders").mkdir()
    (tmp_path / "database" / "seeders" / "DatabaseSeeder.php").write_text("<?php")
    hints = provide_run_hints(RevisionContext.from_directory(tmp_path))
    names = [(h.kind, h.name) for h in hints]
    assert ("command", "composer install") in names
    assert ("env", "APP_KEY") in names and ("env", "DB_HOST") in names and ("env", "APP_NAME") not in names
    assert ("command", "php artisan migrate --seed") in names
    assert next(h for h in hints if h.name == "APP_KEY").line == 2


# --- T6: HTTP routes ----------------------------------------------------------------------------------

SECRET = "s" * 40


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from regression import http

    monkeypatch.setenv("RAG_CALLBACK_SECRET", SECRET)
    store = RegressionStore(tmp_path / "r.sqlite")
    monkeypatch.setattr(http, "_store", lambda: store)
    calls = []

    def start(project_id, base, candidate, **kwargs):
        from regression.service import start_static as real

        calls.append((project_id, base, candidate))
        return real(project_id, base, candidate, created_by=kwargs.get("created_by"), store=kwargs["store"],
                    runner=lambda *a, **k: None, background=False)

    monkeypatch.setattr(http, "start_static", start)
    monkeypatch.setattr(service, "_registry", lambda: Registry())
    app = FastAPI()
    app.include_router(http.router)
    return TestClient(app), store, calls


HEADERS = {"x-internal-secret": SECRET, "x-user-id": "u1"}


def test_routes_create_list_read_and_delete(client):
    http_client, store, calls = client
    created = http_client.post("/projects/pizza/regression-checks", headers=HEADERS,
                               json={"base_revision_id": "rev_a", "candidate_revision_id": "rev_b"})
    assert created.status_code == 202, created.text
    check_id = created.json()["check_id"]
    busy = http_client.post("/projects/pizza/regression-checks", headers=HEADERS,
                            json={"base_revision_id": "rev_a", "candidate_revision_id": "rev_b"})
    assert busy.status_code == 409  # one active check per project
    store.save("pizza", check_id, state="done")
    listed = http_client.get("/projects/pizza/regression-checks", headers=HEADERS).json()["checks"]
    assert [c["check_id"] for c in listed] == [check_id] and "report" not in listed[0]
    read = http_client.get(f"/projects/pizza/regression-checks/{check_id}", headers=HEADERS).json()
    assert read["report"]["check_id"] == check_id and read["counts"]["unchanged"] == 0
    assert http_client.get(f"/projects/other/regression-checks/{check_id}", headers=HEADERS).status_code == 404
    assert http_client.delete(f"/projects/pizza/regression-checks/{check_id}", headers=HEADERS).status_code == 204


def test_routes_map_errors_and_require_credentials(client):
    http_client, _store, _calls = client
    assert http_client.get("/projects/pizza/regression-checks").status_code == 401
    bad = http_client.post("/projects/pizza/regression-checks", headers=HEADERS,
                           json={"base_revision_id": "rev_a", "candidate_revision_id": "rev_a"})
    assert bad.status_code == 422
    unready = http_client.post("/projects/pizza/regression-checks", headers=HEADERS,
                               json={"base_revision_id": "rev_a", "candidate_revision_id": "rev_busy"})
    assert unready.status_code == 409


def test_interrupted_checks_are_failed_on_first_use(tmp_path):
    store = RegressionStore(tmp_path / "r.sqlite")
    check = store.create("pizza", "rev_a", "rev_b")
    assert store.fail_interrupted() == 1
    assert store.get("pizza", check["check_id"])["state"] == "failed"
