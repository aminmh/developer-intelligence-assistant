"""Consumer check and change impact: @.archcore/frontend-assist/contract-conformance-findings.spec.md."""

from __future__ import annotations

import json

import pytest

from api_contracts.conformance import ContractIndex, consumer_report, impact_report, infer_prefix
from api_contracts.model import load_contract
from api_contracts.service import ContractInputError, ContractService
from api_contracts.store import ContractStore

ORDER = {"type": "object", "required": ["id", "total"], "properties": {
    "id": {"type": "integer"}, "total": {"type": "number"}, "note": {"type": "string"}}}
NEW_ORDER = {"type": "object", "required": ["total"], "properties": {"total": {"type": "number"}, "note": {"type": "string"}}}


def contract(**changes) -> dict:
    document = {
        "openapi": "3.0.3", "info": {"title": "Shop", "version": "1"}, "servers": [{"url": "https://shop.example/api"}],
        "paths": {
            "/orders": {
                "get": {"parameters": [{"name": "status", "in": "query", "schema": {"type": "string"}},
                                       {"name": "page", "in": "query", "required": True, "schema": {"type": "integer"}}],
                        "responses": {"200": {"description": "ok", "content": {"application/json": {
                            "schema": {"type": "array", "items": {"$ref": "#/components/schemas/Order"}}}}}}},
                "post": {"requestBody": {"required": True, "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/NewOrder"}}}}, "responses": {"201": {"description": "made"}}},
            },
            "/orders/{orderId}": {"get": {"responses": {"200": {"description": "ok", "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/Order"}}}}}},
                "delete": {"deprecated": True, "responses": {"204": {"description": "gone"}}}},
            "/orders/export": {"get": {"responses": {"200": {"description": "ok"}}}},
            "/health": {"get": {"responses": {"200": {"description": "ok"}}}},
        },
        "components": {"schemas": {"Order": ORDER, "NewOrder": NEW_ORDER}},
    }
    for path, value in changes.items():
        if value is None:
            document["paths"].pop(path, None)
        else:
            document["paths"][path] = value
    return document


def model_of(document: dict) -> dict:
    return load_contract(json.dumps(document).encode())[0]


def call(index: int, method: str, url: str | None, **extra) -> dict:
    return {"id": index, "method": method, "url": url, "raw": url or "buildUrl()", "file": "src/api.ts", "line": 10 + index,
            "symbol": f"fn{index}", "query": [], "query_complete": True, "has_body": False, "body_keys": None,
            "body_complete": False, **extra}


def codes(report: dict) -> list[tuple[str, int]]:
    return sorted((finding["code"], finding["location"].get("line") or 0) for finding in report["findings"])


def test_paths_match_through_base_path_variables_and_host():
    index = ContractIndex(model_of(contract()))
    assert index.match("/api/orders/{id}") == "/orders/{orderId}"
    assert index.match("{baseUrl}/orders/{id}") == "/orders/{orderId}"
    assert index.match("https://shop.example/api/orders") == "/orders"
    assert index.match("/api/orders/export") == "/orders/export"      # fixed text wins over a parameter
    assert index.match("/api/orders/42") == "/orders/{orderId}"       # a literal value for a parameter
    assert index.match("/api/customers") is None
    assert index.match("/v2/orders") is None                          # an undeclared prefix is not accepted on its own
    assert index.match("/v2/orders", accepted_prefix="/v2") == "/orders"


def test_an_undeclared_prefix_is_inferred_only_when_requests_agree():
    document = contract()
    del document["servers"]
    index = ContractIndex(model_of(document))
    agreeing = [call(0, "GET", "/api/v1/orders"), call(1, "GET", "/api/v1/orders/{id}"), call(2, "GET", "/other/health")]
    assert infer_prefix(index, agreeing) == "/api/v1"
    assert infer_prefix(index, [call(0, "GET", "/a/orders"), call(1, "GET", "/b/orders")]) is None
    report = consumer_report({"http_calls": agreeing}, model_of(document))
    assert report["coverage"]["inferred_prefix"] == "/api/v1" and any("/api/v1" in note for note in report["notes"])
    assert codes(report) == [("missing_required_parameter", 10), ("unknown_operation", 12)]


def test_consumer_report_findings_and_coverage():
    ui_map = {"notes": [], "http_calls": [
        call(0, "GET", "/api/orders", query=["page", "sort"]),                        # unknown query parameter
        call(1, "GET", "/api/orders", query=["status"]),                              # required 'page' missing
        call(2, "GET", "/api/orders", query=[], query_complete=False),                # partly visible: no "missing" claim
        call(3, "POST", "/api/orders", has_body=True, body_keys=["total", "coupon"], body_complete=True),
        call(4, "POST", "/api/orders", has_body=True, body_keys=["note"], body_complete=True),
        call(5, "POST", "/api/orders", has_body=True, body_keys=["note"], body_complete=False),
        call(6, "POST", "/api/orders"),                                               # required body missing
        call(7, "PUT", "/api/orders/{id}", has_body=True, body_keys=["total"]),       # method not defined
        call(8, "DELETE", "/api/orders/{id}"),                                        # deprecated
        call(9, "GET", "/api/customers/{id}"),                                        # unknown
        call(10, "GET", None),                                                        # built at run time
    ], "models": [
        {"name": "IOrder", "file": "src/types.ts", "line": 3, "properties": [
            {"name": "id", "optional": False}, {"name": "customerName", "optional": False}]},
        {"name": "Unrelated", "file": "src/types.ts", "line": 9, "properties": [{"name": "x", "optional": False}]},
    ]}
    report = consumer_report(ui_map, model_of(contract()))
    assert codes(report) == sorted([
        ("unknown_query_parameter", 10), ("missing_required_parameter", 11),
        ("unknown_body_property", 13), ("missing_required_property", 14), ("missing_request_body", 16),
        ("method_not_in_contract", 17), ("deprecated_operation", 18), ("unknown_operation", 19), ("unresolved_address", 20),
        ("model_property_not_in_contract", 3), ("model_missing_property", 3),
    ])
    severities = {finding["code"]: finding["severity"] for finding in report["findings"]}
    assert severities["unknown_operation"] == severities["method_not_in_contract"] == "error"
    assert severities["unresolved_address"] == severities["model_missing_property"] == "info"
    assert report["findings"][0]["severity"] == "error"                               # most severe first
    assert report["summary"] == {"errors": 2, "warnings": 7, "info": 2}
    coverage = report["coverage"]
    assert (coverage["requests_total"], coverage["requests_checked"], coverage["requests_unresolved"]) == (11, 10, 1)
    assert coverage["requests_matched"] == 8 and coverage["operations_used"] == 3
    assert coverage["unused_operations"] == ["GET /health", "GET /orders/export", "GET /orders/{orderId}"]
    # Same inputs, same findings and IDs.
    again = consumer_report(ui_map, model_of(contract()))
    assert [f["finding_id"] for f in again["findings"]] == [f["finding_id"] for f in report["findings"]]
    assert len({f["finding_id"] for f in report["findings"]}) == len(report["findings"])


UI_MAP = {
    "notes": [], "models": [],
    "http_calls": [call(0, "GET", "/api/orders"), call(1, "GET", "/api/orders/{id}"), call(2, "GET", "/api/health")],
    "components": [
        {"id": "a#OrdersPage", "name": "OrdersPage", "file": "a", "uses": ["b#OrderRow"], "http_calls": [], "via_imports": [0]},
        {"id": "b#OrderRow", "name": "OrderRow", "file": "b", "uses": [], "http_calls": [1], "via_imports": []},
        {"id": "c#Shell", "name": "Shell", "file": "c", "uses": ["a#OrdersPage"], "http_calls": [], "via_imports": []},
        {"id": "d#Status", "name": "Status", "file": "d", "uses": [], "http_calls": [2], "via_imports": []},
    ],
    "routes": [{"path": "/orders", "component_id": "a#OrdersPage"}, {"path": "/", "component_id": "c#Shell"},
               {"path": "/status", "component_id": "d#Status"}],
}


def test_impact_follows_requests_to_components_and_routes():
    old = model_of(contract())
    changed = contract(**{"/orders/export": None, "/reports": {"get": {"responses": {"200": {"description": "ok"}}}}})
    changed["paths"]["/orders/{orderId}"]["get"]["parameters"] = [{"name": "expand", "in": "query", "schema": {"type": "string"}}]
    changed["components"]["schemas"]["Order"]["properties"]["currency"] = {"type": "string"}
    report = impact_report(UI_MAP, old, model_of(changed))

    entries = {entry["operation"]: entry for entry in report["entries"]}
    assert set(entries) == {"GET /orders/export", "GET /orders/{orderId}", "GET /orders"}
    detail = entries["GET /orders/{orderId}"]
    assert detail["change"] == "changed" and detail["parts"] == ["parameters"] and detail["schemas"] == ["Order"]
    assert [c["name"] for c in detail["components"]] == ["OrderRow"]
    assert detail["routes"] == ["/", "/orders"]                       # through OrdersPage and Shell, which render it
    listing = entries["GET /orders"]
    assert listing["change"] == "schema" and listing["schemas"] == ["Order"]   # unchanged itself, but returns Order
    assert [c["name"] for c in listing["components"]] == ["OrdersPage"] and listing["calls"][0]["line"] == 10
    removed = entries["GET /orders/export"]
    assert removed["change"] == "removed" and removed["calls"] == [] and removed["routes"] == []
    assert [entry["operation"] for entry in report["entries"]][-1] == "GET /orders/export"  # unused changes last
    assert report["added_operations"] == ["GET /reports"] and report["changed_schemas"] == ["Order"]
    assert report["summary"] == {"operations_changed": 3, "operations_in_use": 2, "components": 2, "routes": 2,
                                 "operations_added": 1}
    assert "GET /health" not in entries                                # untouched operations are not listed


@pytest.fixture
def service(tmp_path):
    return ContractService(ContractStore(tmp_path / "contracts"))


def test_service_resolves_versions_and_refuses_what_it_cannot_check(service):
    loader = lambda project_id, revision_id=None: {**UI_MAP, "revision_id": revision_id or "rev_latest", "framework": "vue"}  # noqa: E731
    source = service.store.create_source("backend", "upload", "Shop", {})
    consumer = service.store.attach(source["source_id"], "web", "consumer")
    provider = service.store.attach(source["source_id"], "backend", "provider")

    empty = service.conformance(consumer["attachment_id"], ui_map_loader=loader)
    assert empty["available"] is False and empty["findings"] == []
    with pytest.raises(ContractInputError):
        service.impact(provider["attachment_id"], ui_map_loader=loader)   # impact is for consumers

    first = service.ingest_upload(source["source_id"], json.dumps(contract()).encode())["version"]
    report = service.conformance(consumer["attachment_id"], ui_map_loader=loader)
    assert report["available"] and report["version"]["version_id"] == first["version_id"] and "model" not in report["version"]
    assert report["revision_id"] == "rev_latest" and report["contract"]["name"] == "Shop" and report["checker_version"] == 1
    assert service.impact(consumer["attachment_id"], ui_map_loader=loader)["available"] is False  # one version only

    second = service.ingest_upload(source["source_id"], json.dumps(contract(**{"/health": None})).encode())["version"]
    latest = service.impact(consumer["attachment_id"], ui_map_loader=loader)           # newest against the one before it
    assert latest["from_version"]["version_id"] == first["version_id"] and latest["to_version"]["version_id"] == second["version_id"]
    assert [entry["operation"] for entry in latest["entries"]] == ["GET /health"]
    assert latest["entries"][0]["components"][0]["name"] == "Status" and latest["entries"][0]["routes"] == ["/status"]

    service.store.update_attachment(consumer["attachment_id"], "pinned", first["version_id"])
    pinned = service.impact(consumer["attachment_id"], ui_map_loader=loader)           # pinned version against the newest
    assert pinned["from_version"]["version_id"] == first["version_id"]
    assert service.conformance(consumer["attachment_id"], ui_map_loader=loader)["version"]["version_id"] == first["version_id"]
    with pytest.raises(ContractInputError):
        service.impact(consumer["attachment_id"], "cv_missing", second["version_id"], ui_map_loader=loader)
