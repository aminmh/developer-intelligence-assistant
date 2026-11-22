"""Contract change notifications: @.archcore/frontend-assist/contract-change-notifications.spec.md."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_contracts import events as change_events
from api_contracts.breaking import classify, max_severity
from api_contracts.code_drift import changed_handlers, drift_items, handler_file
from api_contracts.model import load_contract
from api_contracts.service import ContractService
from api_contracts.store import ContractStore

PET = {"type": "object", "required": ["id", "name"], "properties": {
    "id": {"type": "integer"}, "name": {"type": "string"},
    "status": {"type": "string", "enum": ["available", "sold"]}, "tag": {"type": "string"}}}
NEW_PET = {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"},
                                                                  "kind": {"type": "string", "enum": ["cat", "dog"]}}}


def contract() -> dict:
    return {
        "openapi": "3.0.3", "info": {"title": "Pets", "version": "1"}, "servers": [{"url": "/api"}],
        "paths": {
            "/pets": {
                "get": {"parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                        "responses": {"200": {"description": "ok", "content": {"application/json": {
                            "schema": {"type": "array", "items": {"$ref": "#/components/schemas/Pet"}}}}}}},
                "post": {"requestBody": {"required": True, "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/NewPet"}}}},
                    "responses": {"201": {"description": "made"}}},
            },
            "/pets/{id}": {
                "get": {"parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                        "responses": {"200": {"description": "ok", "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Pet"}}}}}},
                "delete": {"parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                           "responses": {"204": {"description": "gone"}}},
            },
        },
        "components": {"schemas": {"Pet": copy.deepcopy(PET), "NewPet": copy.deepcopy(NEW_PET)}},
    }


def model(document: dict) -> dict:
    return load_contract(json.dumps(document).encode())[0]


def codes(items: list[dict]) -> set[tuple[str, str, str | None]]:
    return {(item["severity"], item["code"], item["operation"]) for item in items}


# ── classification ──────────────────────────────────────────────────────────

def test_identical_versions_and_first_version_have_no_items():
    assert classify(model(contract()), model(contract())) == []
    assert classify(None, model(contract())) == []


def test_response_changes_are_judged_from_the_reader_side():
    new = contract()
    pet = new["components"]["schemas"]["Pet"]
    del pet["properties"]["tag"]                                   # removed → breaking
    pet["properties"]["id"] = {"type": "string"}                   # type change → breaking
    pet["required"] = ["id"]                                       # name now optional → risky
    pet["properties"]["status"]["enum"].append("pending")          # new enum value → risky
    pet["properties"]["born"] = {"type": "string"}                 # added → safe
    items = classify(model(contract()), model(new))
    found = codes(items)
    for operation in ("GET /pets", "GET /pets/{id}"):
        assert ("breaking", "response_property_removed", operation) in found
        assert ("breaking", "response_type_changed", operation) in found
        assert ("risky", "response_property_optional", operation) in found
        assert ("risky", "response_enum_value_added", operation) in found
        assert ("safe", "response_property_added", operation) in found
    removed = next(i for i in items if i["code"] == "response_property_removed" and i["operation"] == "GET /pets")
    assert removed["pointer"] == "responses.200[].tag"
    assert items[0]["severity"] == "breaking" and max_severity(items) == "breaking"


def test_request_changes_are_judged_from_the_writer_side():
    new = contract()
    body = new["components"]["schemas"]["NewPet"]
    body["required"] = ["name", "owner"]
    body["properties"]["owner"] = {"type": "string"}               # new required → breaking
    body["properties"]["kind"]["enum"] = ["cat"]                   # value removed → breaking
    get = new["paths"]["/pets"]["get"]
    get["parameters"].append({"name": "page", "in": "query", "required": True, "schema": {"type": "integer"}})
    found = codes(classify(model(contract()), model(new)))
    assert ("breaking", "request_property_required", "POST /pets") in found
    assert ("breaking", "request_enum_value_removed", "POST /pets") in found
    assert ("breaking", "required_parameter_added", "GET /pets") in found

    lenient = contract()
    del lenient["components"]["schemas"]["NewPet"]["properties"]["kind"]   # server ignores it → risky
    lenient["paths"]["/pets"]["get"]["parameters"].append({"name": "q", "in": "query", "schema": {"type": "string"}})
    found = codes(classify(model(contract()), model(lenient)))
    assert ("risky", "request_property_removed", "POST /pets") in found
    assert ("safe", "parameter_added", "GET /pets") in found
    assert not any(severity == "breaking" for severity, _code, _op in found)


def test_operation_level_changes():
    new = contract()
    del new["paths"]["/pets/{id}"]["delete"]
    new["paths"]["/pets"]["get"]["deprecated"] = True
    new["paths"]["/owners"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    new["paths"]["/pets/{id}"]["get"]["responses"] = {"404": {"description": "missing"}}
    found = codes(classify(model(contract()), model(new)))
    assert ("breaking", "operation_removed", "DELETE /pets/{id}") in found
    assert ("risky", "operation_deprecated", "GET /pets") in found
    assert ("safe", "operation_added", "GET /owners") in found
    assert ("breaking", "response_status_removed", "GET /pets/{id}") in found


def test_recursive_schema_terminates():
    document = contract()
    document["components"]["schemas"]["Pet"]["properties"]["parent"] = {"$ref": "#/components/schemas/Pet"}
    changed = copy.deepcopy(document)
    del changed["components"]["schemas"]["Pet"]["properties"]["tag"]
    assert ("breaking", "response_property_removed", "GET /pets") in codes(classify(model(document), model(changed)))


# ── code drift ──────────────────────────────────────────────────────────────

def routes(*pairs: tuple[str, str, str]) -> dict:
    return {"framework": "laravel", "routes": [
        {"method": method, "path": path, "action": action, "file": "routes/api.php", "line": None, "api": True}
        for method, path, action in pairs]}


OLD_ROUTES = routes(("GET", "/api/pets", "App\\Http\\Controllers\\PetController@index"),
                    ("POST", "/api/pets", "App\\Http\\Controllers\\PetController@store"),
                    ("GET", "/api/pets/{pet}", "App\\Http\\Controllers\\PetShowController@show"),
                    ("DELETE", "/api/pets/{pet}", "App\\Http\\Controllers\\PetController@destroy"))


def test_removed_route_and_new_undocumented_route():
    new = routes(("GET", "/api/pets", "App\\Http\\Controllers\\PetController@index"),
                 ("POST", "/api/pets", "App\\Http\\Controllers\\PetController@store"),
                 ("GET", "/api/pets/{pet}", "App\\Http\\Controllers\\PetShowController@show"),
                 ("GET", "/api/vets", "App\\Http\\Controllers\\VetController@index"))
    found = codes(drift_items(OLD_ROUTES, new, model(contract())))
    assert found == {("breaking", "route_removed_in_code", "DELETE /pets/{id}"),
                     ("safe", "undocumented_route_added", None)}


def test_changed_handler_is_risky_only_when_the_contract_did_not_change(tmp_path: Path):
    old_root, new_root = tmp_path / "old", tmp_path / "new"
    for root, body in ((old_root, "return $pet;"), (new_root, "return new PetResource($pet);")):
        (root / "app/Http/Controllers").mkdir(parents=True)
        (root / "routes").mkdir()
        (root / "routes/api.php").write_text("<?php // same")
        (root / "app/Http/Controllers/PetShowController.php").write_text(f"<?php {body}")
        (root / "app/Http/Controllers/PetController.php").write_text("<?php same")
    assert handler_file(OLD_ROUTES["routes"][2], "laravel") == "app/Http/Controllers/PetShowController.php"
    changed = changed_handlers(old_root, new_root, OLD_ROUTES["routes"], "laravel")
    assert changed == {"app/Http/Controllers/PetShowController.php"}
    items = drift_items(OLD_ROUTES, OLD_ROUTES, model(contract()), changed_files=changed)
    assert codes(items) == {("risky", "handler_changed_contract_unchanged", "GET /pets/{id}")}
    assert drift_items(OLD_ROUTES, OLD_ROUTES, model(contract()), changed_files=changed, contract_changed=True) == []


# ── events: storage, emission, delivery ─────────────────────────────────────

class FakeRegistry:
    def __init__(self, revisions: list[dict]):
        self.revisions = revisions  # newest first

    def project_revisions(self, project_id):
        return [row for row in self.revisions if row["project_id"] == project_id]


@pytest.fixture
def service(tmp_path) -> ContractService:
    return ContractService(ContractStore(tmp_path))


def upload(service: ContractService, source_id: str, document: dict) -> dict:
    return service.ingest_upload(source_id, json.dumps(document).encode())


def breaking_change() -> dict:
    document = contract()
    del document["components"]["schemas"]["Pet"]["properties"]["tag"]
    return document


def test_event_is_recorded_for_a_new_version_only(service):
    source = service.store.create_source("backend", "upload", "Pets", {})
    service.store.attach(source["source_id"], "web", "consumer")
    upload(service, source["source_id"], contract())
    assert service.events_for_project("web")["events"] == []          # first version: nothing changed
    upload(service, source["source_id"], contract())                  # unchanged document
    assert service.events_for_project("web")["events"] == []
    created = upload(service, source["source_id"], breaking_change())
    listed = service.events_for_project("web", "dev-1")["events"]
    assert len(listed) == 1
    event = listed[0]
    assert event["kind"] == "contract" and event["max_severity"] == "breaking"
    assert event["to_version_id"] == created["version"]["version_id"] and event["acknowledged"] is False
    assert event["headline"][0]["code"] == "response_property_removed"
    assert service.events_for_project("other")["events"] == []         # not a consumer
    service.store.acknowledge(event["event_id"], "dev-1")
    assert service.events_for_project("web", "dev-1")["events"][0]["acknowledged"] is True
    assert service.events_for_project("web", "dev-1", after_seq=event["seq"])["events"] == []


def test_push_reaches_following_consumers_only(service, monkeypatch):
    pushed = []
    monkeypatch.setattr(change_events, "publish_change", lambda event, projects: pushed.append((event, set(projects))))
    source = service.store.create_source("backend", "upload", "Pets", {})
    first = upload(service, source["source_id"], contract())
    service.store.attach(source["source_id"], "web", "consumer")
    service.store.attach(source["source_id"], "admin", "consumer", "pinned", first["version"]["version_id"])
    upload(service, source["source_id"], breaking_change())
    assert len(pushed) == 1 and pushed[0][1] == {"web"}

    safe = breaking_change()
    safe["paths"]["/owners"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    upload(service, source["source_id"], safe)                        # additive only: listed, not pushed
    assert len(pushed) == 1
    assert len(service.events_for_project("admin")["events"]) == 2    # a pinned consumer still sees the history


def test_event_detail_points_at_consumer_calls(service):
    source = service.store.create_source("backend", "upload", "Pets", {})
    attachment = service.store.attach(source["source_id"], "web", "consumer")
    upload(service, source["source_id"], contract())
    upload(service, source["source_id"], breaking_change())
    event = service.events_for_project("web")["events"][0]
    ui_map = {"revision_id": "rev_web", "notes": [], "components": [], "routes": [], "http_calls": [
        {"id": 0, "method": "GET", "url": "/api/pets/${id}", "file": "src/api/pets.ts", "line": 12, "symbol": "getPet"}]}
    detail = service.event_detail(event["event_id"], "web", ui_map_loader=lambda project, revision: ui_map)
    assert detail["attachment_id"] == attachment["attachment_id"]
    entry = next(e for e in detail["entries"] if e["operation"] == "GET /pets/{id}")
    assert entry["severity"] == "breaking"
    assert entry["calls"] == [{"file": "src/api/pets.ts", "line": 12, "symbol": "getPet", "request": "GET /api/pets/${id}"}]
    assert any(item["code"] == "response_property_removed" for item in entry["items"])


def test_code_drift_event_after_a_provider_revision(service, monkeypatch):
    pushed = []
    monkeypatch.setattr(change_events, "publish_change", lambda event, projects: pushed.append(set(projects)))
    source = service.store.create_source("backend", "upload", "Pets", {})
    upload(service, source["source_id"], contract())
    service.store.attach(source["source_id"], "backend", "provider")
    service.store.attach(source["source_id"], "web", "consumer")
    service._registry = FakeRegistry([
        {"project_id": "backend", "revision_id": "rev_2", "state": "ready", "source_kind": "git"},
        {"project_id": "backend", "revision_id": "rev_wt", "state": "ready", "source_kind": "working_tree"},
        {"project_id": "backend", "revision_id": "rev_1", "state": "ready", "source_kind": "git"},
    ])
    new = routes(*[(r["method"], r["path"], r["action"]) for r in OLD_ROUTES["routes"] if r["method"] != "DELETE"])
    loaded = {"rev_1": OLD_ROUTES, "rev_2": new}
    events = service.detect_code_drift("backend", {"revision_id": "rev_2"},
                                       routes_loader=lambda project, revision: loaded[revision])
    assert len(events) == 1 and events[0]["kind"] == "code"
    assert events[0]["from_revision_id"] == "rev_1" and events[0]["to_revision_id"] == "rev_2"
    assert pushed == [{"web"}]
    assert service.events_for_project("web")["events"][0]["headline"][0]["code"] == "route_removed_in_code"


def test_deleting_a_source_removes_its_events(service):
    source = service.store.create_source("backend", "upload", "Pets", {})
    upload(service, source["source_id"], contract())
    upload(service, source["source_id"], breaking_change())
    assert service.store.list_events([source["source_id"]])
    service.store.delete_source(source["source_id"])
    assert service.store.list_events([source["source_id"]]) == []


def test_stream_delivers_only_to_listeners_of_the_project():
    async def run() -> list[str]:
        web = change_events.change_events(["web"], keepalive=5)
        other = change_events.change_events(["other"], keepalive=0.05)
        assert (await web.__anext__()).startswith("event: connected")
        assert (await other.__anext__()).startswith("event: connected")
        event = {"seq": 7, "event_id": "ce_1", "kind": "contract", "max_severity": "breaking",
                 "items": [{"severity": "breaking", "code": "operation_removed", "operation": "GET /x",
                            "pointer": "", "message": "gone"}]}
        assert change_events.publish_change(event, ["web"]) == 1
        message = await asyncio.wait_for(web.__anext__(), 1)
        keepalive = await asyncio.wait_for(other.__anext__(), 1)
        await web.aclose()
        await other.aclose()
        return [message, keepalive]

    message, keepalive = asyncio.run(run())
    assert message.startswith("id: 7\nevent: contract.changed\ndata: ")
    payload = json.loads(message.split("data: ", 1)[1])
    assert payload["consumer_project_id"] == "web" and "items" not in payload
    assert payload["headline"][0]["code"] == "operation_removed"
    assert keepalive == ": keep-alive\n\n"
    assert change_events.subscriber_count() == 0


def test_http_routes_are_scoped_to_consumers(service, monkeypatch):
    from routers import api_contracts as routes_module

    secret = "s" * 32
    monkeypatch.setattr(routes_module.settings._env, "internal_secret", secret)
    monkeypatch.setattr(routes_module, "contract_service", service)
    app = FastAPI()
    app.include_router(routes_module.router)
    client = TestClient(app, headers={"X-Internal-Secret": secret})
    source = service.store.create_source("backend", "upload", "Pets", {})
    service.store.attach(source["source_id"], "web", "consumer")
    upload(service, source["source_id"], contract())
    upload(service, source["source_id"], breaking_change())

    listed = client.get("/contracts/projects/web/events", params={"user_id": "dev-1"}).json()
    event_id = listed["events"][0]["event_id"]
    assert listed["latest_seq"] == listed["events"][0]["seq"]
    assert client.get(f"/contracts/projects/other/events/{event_id}").status_code == 404
    assert client.post(f"/contracts/projects/other/events/{event_id}/ack", json={"user_id": "dev-1"}).status_code == 404
    assert client.post(f"/contracts/projects/web/events/{event_id}/ack", json={}).status_code == 400
    assert client.post(f"/contracts/projects/web/events/{event_id}/ack", json={"user_id": "dev-1"}).status_code == 200
    assert client.get("/contracts/projects/web/events", params={"user_id": "dev-1"}).json()["events"][0]["acknowledged"]
    assert client.get("/contracts/events/stream").status_code == 400
    assert TestClient(app).get("/contracts/projects/web/events").status_code == 401
