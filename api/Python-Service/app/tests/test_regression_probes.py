"""Probe sets: @.archcore/regression/regression-check.spec.md clauses 10-12; plan tasks T8-T10."""

from __future__ import annotations

import json

import pytest

from regression.probes import ProbeError, build_from_routes, hint_excerpts, import_requests, validate_probes
from regression.store import RegressionStore


# --- T8: PROBE_HINTS ------------------------------------------------------------------------------------

def test_probe_hints_list_seeders_factories_and_form_requests(tmp_path):
    from agent.core.revision import RevisionContext
    from agent.plugins.frameworks.laravel.probe_hints import provide_probe_hints

    for path in ("database/seeders/OrderSeeder.php", "database/factories/OrderFactory.php",
                 "app/Http/Requests/StoreOrderRequest.php", "app/Models/Order.php", "database/seeders/readme.md"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("<?php")
    hints = provide_probe_hints(RevisionContext.from_directory(tmp_path))
    assert [(h.kind, h.name) for h in hints] == [("seeder", "OrderSeeder"), ("factory", "OrderFactory"),
                                                ("validation", "StoreOrderRequest")]


def test_probe_hints_reach_the_registry_for_a_laravel_project(tmp_path):
    from agent import default_registry
    from agent.core.capability import Capability
    from agent.core.revision import RevisionContext

    (tmp_path / "composer.json").write_text(json.dumps({"require": {"laravel/framework": "^11.0"}}))
    (tmp_path / "artisan").write_text("#!/usr/bin/env php")
    (tmp_path / "database" / "seeders").mkdir(parents=True)
    (tmp_path / "database" / "seeders" / "DatabaseSeeder.php").write_text("<?php")
    collection = default_registry().collect(Capability.PROBE_HINTS, RevisionContext.from_directory(tmp_path))
    assert "laravel" in collection.providers
    assert [r.name for r in collection.records] == ["DatabaseSeeder"]


# --- T9: from routes, clause 12 ------------------------------------------------------------------------

class Hint:
    def __init__(self, kind, name, file):
        self.kind, self.name, self.file = kind, name, file


def test_probes_from_capabilities_and_routes():
    routes = [{"method": "GET", "path": "/api/orders"},
              {"method": "GET|HEAD", "path": "/api/orders/{order}"},
              {"method": "POST", "path": "/api/orders"},
              {"method": "DELETE", "path": "/api/orders/{order}"},
              {"method": "GET", "path": "/api/orders"}]  # duplicate
    files = {"database/seeders/OrderSeeder.php": "Order::create(['id' => 7, 'total' => 12]);"}
    excerpts = hint_excerpts([Hint("seeder", "OrderSeeder", "database/seeders/OrderSeeder.php"),
                              Hint("factory", "Missing", "database/factories/Missing.php")], files.get)
    assert [e["name"] for e in excerpts] == ["OrderSeeder"]
    prompts = []

    def model(prompt, **kwargs):
        prompts.append(json.loads(prompt))
        return json.dumps({"probes": [
            {"route": "GET /api/orders/{order}", "params": {"order": 7}, "query": "with=items", "note": "seeded id"},
            {"route": "POST /api/orders", "params": {}, "body": {"total": 12}},
            {"route": "DELETE /api/orders/{order}", "params": {"order": "../x"}},
            {"route": "GET /not/asked", "params": {}},
        ]}), "m", "local"

    built = build_from_routes(routes, excerpts, model_call=model)
    probes = {(p["method"], p["path"]): p for p in built["probes"]}
    assert set(probes) == {("GET", "/api/orders"), ("GET", "/api/orders/7?with=items"), ("POST", "/api/orders")}
    assert probes[("POST", "/api/orders")]["body"] == '{"total": 12}'
    assert probes[("POST", "/api/orders")]["headers"]["content-type"] == "application/json"
    assert probes[("GET", "/api/orders/7?with=items")]["template"] == "/api/orders/{order}"
    # The parameterless GET needs no model; the model saw only the routes it had to fill and the hint files.
    assert prompts[0]["routes"] == ["GET /api/orders/{order}", "POST /api/orders", "DELETE /api/orders/{order}"]
    assert prompts[0]["files"][0]["file"] == "database/seeders/OrderSeeder.php"
    assert [u["route"] for u in built["unfilled"]] == ["DELETE /api/orders/{order}"]


def test_probes_from_capabilities_model_failure_leaves_routes_unfilled():
    def broken(*_a, **_k):
        raise RuntimeError("down")

    built = build_from_routes([{"method": "GET", "path": "/a/{id}"}], [], model_call=broken)
    assert built["probes"] == [] and built["unfilled"][0]["params"] == ["id"]
    assert built["unfilled"][0]["reason"] == "the model could not be reached"  # found live: say why


def test_probes_from_capabilities_edited_set_is_stored_in_order(tmp_path):
    store = RegressionStore(tmp_path / "r.sqlite")
    check = store.create("pizza", "rev_a", "rev_b")
    probes = validate_probes([{"method": "get", "path": "/b"}, {"method": "POST", "path": "/a", "body": {"x": 1},
                                                                "headers": {"Authorization": "Bearer s", "Accept": "x"}}])
    assert [p["order"] for p in probes] == [0, 1] and probes[1]["headers"] == {"accept": "x", "content-type": "application/json"}
    store.set_probes("pizza", check["check_id"], probes)
    assert [p["path"] for p in store.get_probes("pizza", check["check_id"])] == ["/b", "/a"]
    for bad in ([{"method": "GET", "path": "/a", "body": "x"}], [{"method": "TRACE", "path": "/a"}],
                [{"method": "GET", "path": "https://evil.example/"}], [{"method": "GET", "path": "/{id}"}],
                [{"probe_id": "p", "path": "/a"}, {"probe_id": "p", "path": "/b"}], "nope"):
        with pytest.raises(ProbeError):
            validate_probes(bad)


# --- T10: HAR and request lists, clause 11 -------------------------------------------------------------

def _har(*entries):
    return json.dumps({"log": {"entries": [{"request": request} for request in entries]}})


def test_har_import_keeps_the_target_origin_and_drops_credentials():
    text = _har(
        {"method": "GET", "url": "http://localhost:8000/api/orders?page=2",
         "headers": [{"name": "Cookie", "value": "session=s"}, {"name": "Authorization", "value": "Bearer t"},
                     {"name": "Accept", "value": "application/json"}]},
        {"method": "POST", "url": "http://localhost:8000/api/orders", "headers": [],
         "postData": {"mimeType": "application/json", "text": "{\"total\": 3}"}},
        {"method": "GET", "url": "https://cdn.example.com/app.js", "headers": []},
        {"method": "GET", "url": "http://localhost:9000/other", "headers": []},
        {"method": "GET", "url": "http://localhost:8000/../etc", "headers": []},
    )
    imported = import_requests(text, "http://host.docker.internal:8000")
    assert [(p["method"], p["path"]) for p in imported["probes"]] == [("GET", "/api/orders?page=2"), ("POST", "/api/orders")]
    assert imported["probes"][0]["headers"] == {"accept": "application/json"}
    assert imported["probes"][1]["body"] == '{"total": 3}' and imported["probes"][1]["source"] == "har"
    assert imported["skipped"] == {"other_origin": 2, "invalid": 1, "over_limit": 0}


def test_har_import_accepts_a_plain_request_list_and_rejects_other_json():
    listed = import_requests(json.dumps([{"method": "GET", "path": "/health"},
                                         {"method": "PUT", "url": "https://shop.example.com/api/x", "body": "{}"}]),
                             "https://shop.example.com")
    assert [(p["method"], p["path"], p["source"]) for p in listed["probes"]] == [("GET", "/health", "list"),
                                                                                 ("PUT", "/api/x", "list")]
    with pytest.raises(ProbeError):
        import_requests("{\"not\": \"har\"}", "https://shop.example.com")
    with pytest.raises(ProbeError):
        import_requests("not json", "https://shop.example.com")
