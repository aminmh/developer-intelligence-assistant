"""Replay: @.archcore/regression/regression-check.spec.md clauses 13-14, 17-18, failure clauses 3-6;
plan tasks T12-T15."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from regression import replay
from regression.recordings import MAX_BODY_BYTES, Recordings
from regression.replay import ReplayError, execute, merge, plan_probes, send, span_deltas, summarize_spans
from regression.store import RegressionStore

PUBLIC = {"target_id": "t-base", "origin": "http://host.docker.internal:8000", "auth_mode": "public"}


def _probe(probe_id, method, path, order, template=None, body=None):
    return {"probe_id": probe_id, "order": order, "method": method, "path": path, "template": template or path,
            "headers": {}, "body": body, "source": "manual", "note": ""}


def _response(body, status=200):
    raw = json.dumps(body).encode()
    return {"status": status, "headers": {"content-type": "application/json"}, "body": raw, "truncated": False,
            "size": len(raw), "error": None}


@pytest.fixture(autouse=True)
def _local_targets(monkeypatch):
    # Replay targets on host.docker.internal are a Developer Mode feature; tests run in that mode.
    monkeypatch.setenv("APP_DEPLOYMENT_MODE", "developer")


# --- T12: recordings, failure clause 6 -----------------------------------------------------------------

def test_recordings_keep_bodies_hash_large_ones_and_purge(tmp_path, monkeypatch):
    recordings = Recordings(tmp_path)
    meta = recordings.save("pizza", "rc_1", "run-1", "p1", 1, _response({"a": 1}))
    assert meta["truncated"] is False and meta["body_sha256"]
    assert json.loads(recordings.load("pizza", "rc_1", "run-1", "p1", 1)["body"]) == {"a": 1}
    big = {"status": 200, "headers": {}, "body": None, "body_sha256": "f" * 64, "truncated": True, "size": MAX_BODY_BYTES + 1}
    assert recordings.save("pizza", "rc_1", "run-1", "p2", 1, big)["truncated"] is True
    assert recordings.load("pizza", "rc_1", "run-1", "p2", 1)["body"] is None
    monkeypatch.setattr("regression.recordings.MAX_CHECK_BYTES", 10)  # over the check budget: hash only
    assert recordings.save("pizza", "rc_1", "run-1", "p3", 1, _response({"b": 2}))["truncated"] is True
    with pytest.raises(ValueError):
        recordings.save("pizza", "../x", "run-1", "p1", 1, _response({}))
    assert recordings.purge(now=time.time()) == 0
    assert recordings.purge(now=time.time() + 31 * 86400) == 1
    assert recordings.load("pizza", "rc_1", "run-1", "p1", 1) is None


# --- T13: order, egress, write gating, failure clauses 3-5 ---------------------------------------------

class Sender:
    def __init__(self, answers=None, unreachable=False):
        self.calls = []
        self.answers = answers or {}
        self.unreachable = unreachable

    def __call__(self, origin, pinned, method, path, headers, body):
        self.calls.append((method, path, headers.get("X-DevAssistant-Benchmark"), headers.get("Authorization")))
        if self.unreachable:
            return {"status": None, "headers": {}, "body": None, "error": "ConnectionRefusedError: refused"}
        answer = self.answers.get((method, path), {"ok": True})
        return _response(answer() if callable(answer) else answer)


def test_order_egress_and_write_gating(tmp_path):
    probes = [_probe("p2", "POST", "/orders", 1, body='{"total": 1}'), _probe("p1", "GET", "/orders", 0),
              _probe("p3", "DELETE", "/orders/1", 2)]
    sent, skipped = plan_probes(probes, disposable=False)
    assert [p["probe_id"] for p in sent] == ["p1"]
    assert [s["probe_id"] for s in skipped] == ["p2", "p3"]
    sent, skipped = plan_probes(probes, disposable=True)
    assert [p["probe_id"] for p in sent] == ["p1", "p2", "p3"] and skipped == []

    counter = iter(range(100))
    sender = Sender({("GET", "/orders"): lambda: {"items": [1], "request": next(counter)}})
    recordings = Recordings(tmp_path)
    run = {"run_id": "11111111-1111-1111-1111-111111111111", "check_id": "rc_1", "kind": "baseline"}
    summary = execute(run, PUBLIC, probes, settings={"disposable": True, "reset_path": "/__reset"}, token="tok",
                      recordings=recordings, project_id="pizza", sender=sender)
    # Two passes, each after the reset, probes in order, tagged with the run id and the token.
    assert [c[:2] for c in sender.calls] == [("POST", "/__reset"), ("GET", "/orders"), ("POST", "/orders"),
                                             ("DELETE", "/orders/1")] * 2
    assert {c[2] for c in sender.calls} == {run["run_id"]} and {c[3] for c in sender.calls} == {"Bearer tok"}
    assert summary["noise"]["p1"] == ["body:$.request"] and summary["noise"]["p2"] == []


def test_order_egress_and_write_gating_refusals(tmp_path):
    recordings = Recordings(tmp_path)
    run = {"run_id": "r", "check_id": "rc_1", "kind": "baseline"}
    probes = [_probe("p1", "GET", "/", 0)]
    with pytest.raises(ReplayError, match="unreachable"):
        execute(run, PUBLIC, probes, settings={}, token=None, recordings=recordings, project_id="pizza",
                sender=Sender(unreachable=True))
    with pytest.raises(ReplayError, match="HTTP-login"):
        execute(run, {**PUBLIC, "auth_mode": "http_login"}, probes, settings={}, token=None, recordings=recordings,
                project_id="pizza", sender=Sender())
    for origin in ("http://10.0.0.5", "ftp://example.com", "http://app.internal"):
        with pytest.raises(ReplayError):
            execute(run, {**PUBLIC, "origin": origin}, probes, settings={}, token=None, recordings=recordings,
                    project_id="pizza", sender=Sender())
    with pytest.raises(ReplayError, match="No probe can be sent"):
        execute(run, PUBLIC, [_probe("p2", "POST", "/x", 0)], settings={}, token=None, recordings=recordings,
                project_id="pizza", sender=Sender())


def test_order_egress_and_write_gating_real_http_with_body_cap(tmp_path, monkeypatch):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append((self.path, self.headers.get("Host"), self.headers.get("X-DevAssistant-Benchmark")))
            body = b"x" * (MAX_BODY_BYTES + 10) if self.path == "/big" else b'{"ok": true}'
            self.send_response(302 if self.path == "/moved" else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Location", "/elsewhere")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        origin = f"http://example.test:{server.server_port}"
        ok = send(origin, "127.0.0.1", "GET", "/ok", {"X-DevAssistant-Benchmark": "run"}, None)
        assert ok["status"] == 200 and json.loads(ok["body"]) == {"ok": True}
        # Pinned: the socket went to 127.0.0.1 while Host kept the target's name.
        assert seen[0] == ("/ok", f"example.test:{server.server_port}", "run")
        big = send(origin, "127.0.0.1", "GET", "/big", {}, None)
        assert big["truncated"] is True and big["body"] is None and big["size"] == MAX_BODY_BYTES + 10
        moved = send(origin, "127.0.0.1", "GET", "/moved", {}, None)
        assert moved["status"] == 302 and moved["headers"]["location"] == "/elsewhere"  # redirects are not followed
    finally:
        server.shutdown()
    assert send("http://example.test:1", "127.0.0.1", "GET", "/", {}, None)["error"]


# --- T14: verdicts and span deltas, clauses 17-18 -------------------------------------------------------

def _store_with_check(tmp_path):
    store = RegressionStore(tmp_path / "r.sqlite")
    check = store.create("pizza", "rev_a", "rev_b")
    store.set_probes("pizza", check["check_id"], [
        _probe("p1", "GET", "/orders/7", 0, template="/orders/{order}"),
        _probe("p2", "GET", "/health", 1)])
    return store, check


def test_verdicts_from_baseline_and_candidate_runs(tmp_path):
    store, check = _store_with_check(tmp_path)
    check_id = check["check_id"]
    recordings = Recordings(tmp_path / "rec")
    version = {"total": 12}
    sender = Sender({("GET", "/orders/7"): lambda: {"id": 7, **version}, ("GET", "/health"): {"ok": True}})
    with pytest.raises(ReplayError, match="baseline"):
        replay.start(store, "pizza", check_id, "candidate", PUBLIC, recordings=recordings, sender=sender, background=False)
    base = replay.start(store, "pizza", check_id, "baseline", PUBLIC, recordings=recordings, sender=sender, background=False)
    assert base["state"] == "completed", base["error"]
    version["total"] = 13  # the candidate changed behavior
    cand = replay.start(store, "pizza", check_id, "candidate", {**PUBLIC, "target_id": "t-cand"},
                        recordings=recordings, sender=sender, background=False)
    assert cand["state"] == "completed", cand["error"]
    results = {r["probe_id"]: r for r in cand["summary"]["results"]}
    assert results["p2"]["equal"] is True
    assert results["p1"]["differences"] == [{"where": "body", "path": "$.total", "base": 12, "candidate": 13}]

    report = {"routes": [{"method": "GET", "path": "/orders/{order}", "fqn": "App\\Order::show", "verdict": "unchanged",
                          "handler_path": "app/Order.php", "handler_line": 3},
                         {"method": "GET", "path": "/users", "fqn": "App\\User::list", "verdict": None}],
              "symbols": [{"fqn": "App\\Order::show", "change": "modified", "verdict": "unchanged"}],
              "impacted": [], "uncovered": [], "notes": []}
    merged = merge(report, cand)
    orders, users = merged["routes"]
    # Clause 17: a differing response beats the model's "unchanged".
    assert orders["verdict"] == "suspected_regression" and orders["coverage"] == ["replay"]
    assert orders["replay"]["differing"] == 1 and orders["static_verdict"] == "unchanged"
    assert users["verdict"] == "not_covered" and "replay" not in users
    assert [u["name"] for u in merged["uncovered"]] == ["GET /users"]
    assert [(o["path"], o["verdict"]) for o in merged["replay_other"]] == [("/health", "unchanged")]
    assert merged["tier"] == "replay" and merged["replay"]["differing"] == 1
    # A later replay that matches starts again from the static verdict.
    for row in cand["summary"]["results"]:
        row.update(equal=True, differences=[])
    assert merge(merged, cand)["routes"][0]["verdict"] == "unchanged"


def test_verdicts_skip_writes_on_non_disposable_targets(tmp_path):
    store, check = _store_with_check(tmp_path)
    probes = store.get_probes("pizza", check["check_id"]) + [_probe("p3", "POST", "/orders", 2, body="{}")]
    store.set_probes("pizza", check["check_id"], probes)
    run = replay.start(store, "pizza", check["check_id"], "baseline", PUBLIC, recordings=Recordings(tmp_path / "rec"),
                       sender=Sender(), background=False)
    assert [s["probe_id"] for s in run["summary"]["skipped"]] == ["p3"]


def test_span_deltas_per_route():
    before = summarize_spans([
        {"route": "api/orders/{order}", "duration_ms": 10, "db_spans": 2, "errors": 0},
        {"route": "api/orders/{order}", "duration_ms": 20, "db_spans": 2, "errors": 0},
        {"route": "", "duration_ms": 1, "db_spans": 0, "errors": 0}])
    after = summarize_spans([
        {"route": "api/orders/{order}", "duration_ms": 30, "db_spans": 5, "errors": 1},
        {"route": "api/orders/{order}", "duration_ms": 30, "db_spans": 5, "errors": 0}])
    assert before == {"api/orders/{order}": {"requests": 2, "p95_ms": 20.0, "db_spans_per_request": 2.0, "errors": 0}}
    deltas = span_deltas(before, after)
    assert deltas["api/orders/{order}"]["flags"] == ["query_count", "new_errors", "slower"]
    assert deltas["api/orders/{order}"]["p95_change_pct"] == 50.0
    assert span_deltas(None, after) == {}
    report = {"routes": [{"method": "GET", "path": "/api/orders/{order}", "verdict": "unchanged"}], "symbols": [],
              "impacted": [], "uncovered": []}
    candidate = {"run_id": "c", "summary": {"baseline_run_id": "b", "results": [
        {"probe_id": "p", "method": "GET", "path": "/api/orders/7", "template": "/api/orders/{order}", "equal": True,
         "differences": []}]}}
    merged = merge(report, candidate, deltas)
    assert merged["routes"][0]["spans"]["flags"] == ["query_count", "new_errors", "slower"]
    assert merged["tier"] == "telemetry"


# --- T15: routes ----------------------------------------------------------------------------------------

SECRET = "s" * 40
HEADERS = {"x-internal-secret": SECRET, "x-user-id": "u1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from regression import http, replay_service

    monkeypatch.setenv("RAG_CALLBACK_SECRET", SECRET)
    store = RegressionStore(tmp_path / "r.sqlite")
    recordings = Recordings(tmp_path / "rec")
    monkeypatch.setattr(http, "_store", lambda: store)
    monkeypatch.setattr(http, "_recordings", lambda: recordings)

    class Targets:
        def get_target(self, user_id, project_id, target_id):
            targets = {"t-base": PUBLIC, "t-cand": {**PUBLIC, "target_id": "t-cand"},
                       "t-tok": {**PUBLIC, "target_id": "t-tok", "auth_mode": "bearer"}}
            return dict(targets[target_id]) if user_id == "u1" and project_id == "pizza" and target_id in targets else None

    monkeypatch.setattr(replay_service, "_targets", lambda: Targets())
    sender = Sender({("GET", "/orders/7"): {"total": 12}})
    real_run = replay_service.run
    monkeypatch.setattr(replay_service, "run", lambda *a, **k: real_run(*a, **{**k, "recordings": recordings,
                                                                          "background": False, "sender": sender}))
    monkeypatch.setattr(replay_service, "span_summary", lambda *a: None)
    check = store.create("pizza", "rev_a", "rev_b")
    report = check["report"] | {"routes": [{"method": "GET", "path": "/orders/{order}", "fqn": "X::show",
                                            "verdict": "unchanged", "handler_path": "x.php", "handler_line": 1}]}
    store.save("pizza", check["check_id"], state="done", report=report)
    app = FastAPI()
    app.include_router(http.router)
    return TestClient(app), store, check["check_id"], sender, recordings


def test_routes_probes_settings_runs_and_report(client):
    http_client, store, check_id, sender, recordings = client
    base = f"/projects/pizza/regression-checks/{check_id}"
    saved = http_client.put(f"{base}/probes", headers=HEADERS, json={"probes": [
        {"method": "GET", "path": "/orders/7", "template": "/orders/{order}"}]})
    assert saved.status_code == 200, saved.text
    assert http_client.put(f"{base}/probes", headers=HEADERS, json={"probes": [{"path": "nope"}]}).status_code == 422
    settings = http_client.put(f"{base}/replay-settings", headers=HEADERS, json={
        "baseline_target_id": "t-base", "candidate_target_id": "t-cand", "ignore": ["$..updated_at"]})
    assert settings.status_code == 200 and settings.json()["settings"]["disposable"] is False
    assert http_client.put(f"{base}/replay-settings", headers=HEADERS, json={"ignore": ["bad"]}).status_code == 422
    assert http_client.post(f"{base}/replay-runs", headers=HEADERS, json={"kind": "candidate"}).status_code == 409
    baseline = http_client.post(f"{base}/replay-runs", headers=HEADERS, json={"kind": "baseline"})
    assert baseline.status_code == 202 and baseline.json()["state"] == "completed"
    sender.answers[("GET", "/orders/7")] = {"total": 99}
    candidate = http_client.post(f"{base}/replay-runs", headers=HEADERS, json={"kind": "candidate"})
    assert candidate.status_code == 202 and candidate.json()["summary"]["results"][0]["equal"] is False
    run = http_client.get(f"{base}/replay-runs/{candidate.json()['run_id']}", headers=HEADERS)
    assert run.status_code == 200
    # The candidate run refreshed the saved report.
    report = http_client.get(f"/projects/pizza/regression-checks/{check_id}", headers=HEADERS).json()["report"]
    assert report["routes"][0]["verdict"] == "suspected_regression" and report["tier"] == "replay"
    state = http_client.get(f"{base}/replay", headers=HEADERS).json()
    assert [r["kind"] for r in state["runs"]] == ["candidate", "baseline"]
    assert state["settings"]["ignore"] == ["$..updated_at"]
    # Bearer targets need the token in the request; unknown targets are 404.
    assert http_client.post(f"{base}/replay-runs", headers=HEADERS,
                            json={"kind": "baseline", "target_id": "t-tok"}).status_code == 422
    assert http_client.post(f"{base}/replay-runs", headers=HEADERS,
                            json={"kind": "baseline", "target_id": "t-zzz"}).status_code == 404
    # Deleting the check removes its recordings.
    assert recordings.load("pizza", check_id, candidate.json()["run_id"], saved.json()["probes"][0]["probe_id"], 1)
    assert http_client.delete(f"/projects/pizza/regression-checks/{check_id}", headers=HEADERS).status_code == 204
    assert recordings.load("pizza", check_id, candidate.json()["run_id"], saved.json()["probes"][0]["probe_id"], 1) is None


def test_routes_import_and_generate(client, monkeypatch):
    from regression import replay_service

    http_client, store, check_id, _sender, _rec = client
    base = f"/projects/pizza/regression-checks/{check_id}"
    har = json.dumps({"log": {"entries": [{"request": {"method": "GET", "url": "http://localhost:8000/health",
                                                        "headers": []}}]}})
    imported = http_client.post(f"{base}/probes/import", headers=HEADERS, json={"text": har, "target_id": "t-base"})
    assert imported.status_code == 200, imported.text
    assert imported.json()["imported"] == 1 and imported.json()["probes"][0]["path"] == "/health"
    monkeypatch.setattr(replay_service, "_candidate_hints", lambda *a: [])
    real_generate = replay_service.generate
    model = lambda *a, **k: (json.dumps({"probes": [{"route": "GET /orders/{order}", "params": {"order": 3}}]}), "m", "local")
    monkeypatch.setattr(replay_service, "generate", lambda *a, **k: real_generate(*a, **{**k, "model_call": model}))
    generated = http_client.post(f"{base}/probes/generate", headers=HEADERS)
    assert generated.status_code == 200, generated.text
    assert [p["path"] for p in generated.json()["probes"]] == ["/orders/3"]


def test_routes_agent_tool_lists_replay_differences(tmp_path):
    from agent_tools.regression import _items

    report = {"symbols": [], "routes": [{"method": "GET", "path": "/o/{id}", "handler_path": "o.php", "handler_line": 2,
                                         "verdict": "suspected_regression",
                                         "replay": {"differences": [{"path": "/o/7", "differences": []}]},
                                         "spans": {"flags": ["query_count"]}}],
              "replay_other": [{"method": "GET", "path": "/h", "equal": False, "verdict": "suspected_regression",
                                "differences": [{"where": "status"}]}, {"method": "GET", "path": "/ok", "equal": True}]}
    route, other = _items(report)
    assert route["replay_differences"][0]["path"] == "/o/7" and route["span_flags"] == ["query_count"]
    assert other["kind"] == "request" and other["route"] == "GET /h"


def test_verdicts_match_hand_typed_paths_to_route_templates():
    """Found live: a probe typed as /api/orders/1 belongs to GET /api/orders/{id}, not to "other requests"."""
    report = {"routes": [{"method": "GET", "path": "/api/orders/{id}", "verdict": "unchanged"},
                         {"method": "GET", "path": "/api/orders", "verdict": "unchanged"},
                         {"method": "GET", "path": "/api/items/{item?}", "verdict": "unchanged"}],
              "symbols": [], "impacted": [], "uncovered": []}
    row = lambda path, equal, method="GET": {"probe_id": path, "method": method, "path": path, "template": path,
                                             "equal": equal, "differences": [] if equal else [{"where": "status"}]}
    candidate = {"run_id": "c", "summary": {"baseline_run_id": "b", "results": [
        row("/api/orders/1", False), row("/api/orders?page=2", True), row("/api/orders/1/items", False),
        row("/api/orders/1", False, method="DELETE"), row("/api/items/9", True)]}}
    merged = merge(report, candidate)
    by_path = {r["path"]: r for r in merged["routes"]}
    assert by_path["/api/orders/{id}"]["verdict"] == "suspected_regression"
    assert by_path["/api/orders/{id}"]["replay"]["probes"] == 1
    assert by_path["/api/orders"]["replay"] == {"probes": 1, "differing": 0, "differences": []}
    assert by_path["/api/items/{item?}"]["coverage"] == ["replay"]
    assert sorted((o["method"], o["path"]) for o in merged["replay_other"]) == [("DELETE", "/api/orders/1"),
                                                                              ("GET", "/api/orders/1/items")]
