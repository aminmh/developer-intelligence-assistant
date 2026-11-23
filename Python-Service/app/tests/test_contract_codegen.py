"""Code and mock generation from a contract version (front-end assistance plan, Phase 5)."""

from __future__ import annotations

import json
import re

import pytest

from api_contracts.codegen import GenerationError, generate
from api_contracts.codegen.generators import default_base_url, example, syntax_errors
from api_contracts.codegen.typescript import TypeNames, camel, operations_of, pascal, ts_type
from api_contracts.model import load_contract
from api_contracts.service import ContractInputError, ContractService
from api_contracts.store import ContractStore

DOCUMENT = {
    "openapi": "3.0.3", "info": {"title": "Shop", "version": "1.2"}, "servers": [{"url": "https://shop.example/api/v1"}],
    "paths": {
        "/orders": {
            "get": {"operationId": "listOrders", "summary": "List orders", "tags": ["orders"], "parameters": [
                {"name": "status", "in": "query", "schema": {"type": "string", "enum": ["open", "paid"]}},
                {"name": "page", "in": "query", "required": True, "schema": {"type": "integer"}},
                {"name": "X-Trace", "in": "header", "schema": {"type": "string"}}],
                "responses": {"200": {"description": "ok", "content": {"application/json": {
                    "schema": {"type": "array", "items": {"$ref": "#/components/schemas/Order"}}}}}}},
            "post": {"operationId": "create-order", "tags": ["orders"], "requestBody": {"required": True, "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/NewOrder"}}}},
                "responses": {"201": {"description": "made", "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Order"}}}}}},
        },
        "/orders/{order-id}": {
            "get": {"tags": ["orders"], "parameters": [
                {"name": "order-id", "in": "path", "required": True, "schema": {"type": "integer"}},
                {"name": "expand", "in": "query", "schema": {"type": "array", "items": {"type": "string"}}}],
                "responses": {"200": {"description": "ok", "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Order"}}}}}},
            "delete": {"deprecated": True, "tags": ["orders"], "parameters": [
                {"name": "order-id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"204": {"description": "gone"}}},
        },
        "/uploads": {"post": {"tags": ["files"], "requestBody": {"required": True, "content": {"multipart/form-data": {
            "schema": {"type": "object", "properties": {"file": {"type": "string", "format": "binary"}}}}}},
            "responses": {"200": {"description": "ok", "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}}}}},
    },
    "components": {"schemas": {
        "Order": {"type": "object", "description": "An order */ here", "required": ["id", "status", "lines"], "properties": {
            "id": {"type": "integer"}, "status": {"$ref": "#/components/schemas/order-status"},
            "lines": {"type": "array", "items": {"$ref": "#/components/schemas/Line"}},
            "note": {"type": "string", "nullable": True}, "created-at": {"type": "string", "format": "date-time"},
            "parent": {"$ref": "#/components/schemas/Order"}}},
        "order-status": {"type": "string", "enum": ["open", "paid"]},
        "Line": {"type": "object", "required": ["sku"], "properties": {"sku": {"type": "string"},
                                                                    "quantity": {"type": "integer", "minimum": 2}}},
        "NewOrder": {"allOf": [{"type": "object", "required": ["lines"], "properties": {
            "lines": {"type": "array", "items": {"$ref": "#/components/schemas/Line"}}}},
            {"type": "object", "properties": {"coupon": {"type": "string"}}}]},
        "Record": {"type": "object", "properties": {"class": {"type": "string"}}},
        "Unused": {"type": "object", "properties": {"x": {"type": "string"}}},
    }},
}


@pytest.fixture(scope="module")
def model():
    return load_contract(json.dumps(DOCUMENT).encode())[0]


def files(result: dict) -> dict[str, str]:
    return {file["path"]: file["content"] for file in result["files"]}


def test_names_are_valid_identifiers():
    assert pascal("order-status") == "OrderStatus" and pascal("2fa") == "_2fa"
    assert camel("create-order") == "createOrder" and camel("Delete") == "delete_" and camel("X-Trace_id") == "xTraceId"
    names = TypeNames({"Record": {}, "order-status": {}, "OrderStatus": {}})
    assert names.names == {"OrderStatus": "OrderStatus", "Record": "Record2", "order-status": "OrderStatus2"}


@pytest.mark.parametrize("schema, expected", [
    ({"type": "string"}, "string"),
    ({"type": "string", "format": "binary"}, "Blob"),
    ({"type": "integer", "nullable": True}, "number | null"),
    ({"type": ["string", "null"]}, "string | null"),
    ({"type": ["string", "integer"]}, "string | number"),
    ({"enum": ["a", 1, None]}, '"a" | 1 | null'),
    ({"type": "array", "items": {"$ref": "#/schemas/Line"}}, "Array<Line>"),
    ({"type": "object", "additionalProperties": {"type": "number"}}, "Record<string, number>"),
    ({"type": "object"}, "Record<string, unknown>"),
    ({"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}, "b-c": {"type": "boolean"}}},
     '{ a: string; "b-c"?: boolean }'),
    ({"oneOf": [{"type": "string"}, {"$ref": "#/schemas/Line"}]}, "string | Line"),
    ({"allOf": [{"$ref": "#/schemas/Line"}, {"oneOf": [{"type": "string"}, {"type": "number"}]}]}, "Line & (string | number)"),
    ({"$ref": "#/schemas/Missing"}, "unknown"),
    ({}, "unknown"),
])
def test_schema_types(schema, expected):
    assert ts_type(schema, TypeNames({"Line": {}})) == expected


def test_models_file_declares_every_schema(model):
    source = files(generate(model, "models"))["models.ts"]
    assert "export interface Order {" in source and "  status: OrderStatus;" in source
    assert '  "created-at"?: string;' in source and "  note?: string | null;" in source and "  parent?: Order;" in source
    assert "/** An order * / here */" in source                       # a comment cannot be closed by contract text
    assert 'export type OrderStatus = "open" | "paid";' in source
    assert "export type NewOrder = { lines: Array<Line> } & { coupon?: string };" in source
    assert "export interface Record2 {" in source                     # does not shadow the built-in Record
    assert source.startswith('// Generated from the API contract "Shop" 1.2.')


def test_operations_get_stable_names_and_ordered_arguments(model):
    operations = {operation.key: operation for operation in operations_of(model, TypeNames(model["schemas"]))}
    listing = operations["GET /orders"]
    assert listing.name == "listOrders" and listing.query_required and listing.skipped_parameters == 1
    assert listing.query_type == '{ page: number; status?: "open" | "paid" }'
    assert operations["POST /orders"].name == "createOrder" and operations["POST /orders"].success_status == 201
    detail = operations["GET /orders/{order-id}"]
    assert detail.name == "getOrdersByOrderId" and detail.path_parameters == [("order-id", "orderId", "number")]
    assert operations["DELETE /orders/{order-id}"].response_type == "void"
    upload = operations["POST /uploads"]
    assert upload.body_type == "FormData" and upload.response_type == "Blob"


@pytest.mark.parametrize("target, expected", [
    ("angular", ["models.ts", "api.service.ts"]), ("react", ["models.ts", "client.ts", "hooks.ts"]),
    ("vue", ["models.ts", "client.ts", "composables.ts"]), ("mocks", ["models.ts", "mock-data.ts", "handlers.ts"]),
    ("models", ["models.ts"]),
])
def test_every_target_parses_as_typescript_and_is_deterministic(model, target, expected):
    result = generate(model, target)
    assert [file["path"] for file in result["files"]] == expected
    assert syntax_errors(result["files"]) == [] and result["operations"] == 5
    assert result["base_url"] == "/api/v1"
    assert generate(model, target) == result
    assert all("\t" not in file["content"] and file["content"].endswith("\n") for file in result["files"])


def test_fetch_client_and_wrappers(model):
    react, vue = files(generate(model, "react")), files(generate(model, "vue"))
    client = react["client.ts"]
    assert "let apiConfig: ApiConfig = { baseUrl: \"/api/v1\" };" in client
    assert "import type { NewOrder, Order } from './models';" in client
    assert ('export function listOrders(query: { page: number; status?: "open" | "paid" }, signal?: AbortSignal): '
            "Promise<Array<Order>> {") in client
    assert "return request<Order>(\"GET\", `/orders/${encodeURIComponent(String(orderId))}`, { query, signal });" in client
    assert "@deprecated" in client and react["client.ts"] == vue["client.ts"]
    hooks = react["hooks.ts"]
    assert "export function useListOrders(query: { page: number; status?: \"open\" | \"paid\" }): QueryState<Array<Order>> {" in hooks
    assert "return useQuery((signal) => api.listOrders(query, signal), JSON.stringify([query]));" in hooks
    assert "export function useCreateOrder() {\n  return useMutation(api.createOrder);" in hooks
    composables = vue["composables.ts"]
    assert "export function useGetOrdersByOrderId(orderId: MaybeRefOrGetter<number>, query?: MaybeRefOrGetter<{ expand?: Array<string> }>)" in composables
    assert "api.getOrdersByOrderId(toValue(orderId), toValue(query), signal)" in composables


def test_angular_service(model):
    service = files(generate(model, "angular", base_url="https://api.test/"))["api.service.ts"]
    assert "factory: () => \"https://api.test\"" in service and "@Injectable({ providedIn: 'root' })" in service
    assert "listOrders(query: { page: number; status?: \"open\" | \"paid\" }): Observable<Array<Order>> {" in service
    assert "return this.http.request<Array<Order>>(\"GET\", url, { params: toParams(query) });" in service
    assert "return this.http.request<Order>(\"POST\", url, { body });" in service
    assert "return this.http.request(\"POST\", url, { body, responseType: \"blob\" });" in service
    assert "return this.http.request<void>(\"DELETE\", url);" in service


def test_mocks_fit_the_schemas(model):
    result = files(generate(model, "mocks"))
    data = result["mock-data.ts"]
    order = json.loads(re.search(r"export const createOrderResponse: Order = (.*);", data).group(1))
    assert order == {"id": 1, "status": "open", "lines": [{"sku": "sku", "quantity": 2}], "note": "note",
                     "created-at": "2026-01-15T09:30:00Z"}          # the recursive "parent" is left out
    handlers = result["handlers.ts"]
    assert 'http.get("*/api/v1/orders/:orderId", () => HttpResponse.json(data.getOrdersByOrderIdResponse, { status: 200 })),' in handlers
    assert 'http.delete("*/api/v1/orders/:orderId", () => new HttpResponse(null, { status: 204 })),' in handlers
    assert 'http.post("*/api/v1/orders", () => HttpResponse.json(data.createOrderResponse, { status: 201 })),' in handlers
    assert example({"type": "object", "properties": {"a": {"example": 7}, "b": {"default": "x"}, "c": {"enum": ["q"]}}}, {}) == {
        "a": 7, "b": "x", "c": "q"}
    assert example({"allOf": [{"$ref": "#/schemas/A"}, {"properties": {"b": {"type": "boolean"}}}]},
                   {"A": {"type": "object", "properties": {"a": {"type": "string", "format": "email"}}}}) == {
        "a": "user@example.com", "b": True}


def test_search_narrows_operations_and_schemas(model):
    result = generate(model, "react", search="files")
    assert result["operations"] == 1 and result["schemas"] == 0
    assert "postUploads" in files(result)["client.ts"] and "listOrders" not in files(result)["client.ts"]
    narrowed = files(generate(model, "models", search="/orders/{order-id}"))["models.ts"]
    assert "interface Order" in narrowed and "interface Line" in narrowed and "Unused" not in narrowed
    with pytest.raises(GenerationError):
        generate(model, "react", search="nothing-like-this")
    with pytest.raises(GenerationError):
        generate(model, "svelte")
    with pytest.raises(GenerationError):
        generate(model, "react", base_url="https://x`${evil}")


def test_notes_and_base_url(model):
    assert any("header or cookie" in note for note in generate(model, "vue")["notes"])
    assert generate(model, "models")["notes"] == []
    assert default_base_url({"base_paths": ["https://{tenant}.example/api"]}) == "/api"     # the host is dropped
    assert default_base_url({"base_paths": ["https://x.example/{version}/api", "/v2/"]}) == "/v2"  # a variable path is skipped
    assert default_base_url({"base_paths": []}) == ""


def test_service_generates_only_from_valid_versions_of_the_source(tmp_path):
    service = ContractService(ContractStore(tmp_path / "contracts"))
    source = service.store.create_source("backend", "upload", "Shop", {})
    other = service.store.create_source("backend", "upload", "Other", {})
    version = service.ingest_upload(source["source_id"], json.dumps(DOCUMENT).encode())["version"]
    failed = service.ingest_upload(source["source_id"], b"{")["version"]
    result = service.generate_code(source["source_id"], version["version_id"], "vue", search="orders")
    assert result["title"] == "Shop" and result["operations"] == 4 and "model" not in result
    for arguments in ((other["source_id"], version["version_id"], "vue"), (source["source_id"], failed["version_id"], "vue"),
                      (source["source_id"], version["version_id"], "cobol"), (source["source_id"], version["version_id"], 7)):
        with pytest.raises(ContractInputError):
            service.generate_code(*arguments)


def test_chat_tool_returns_code_and_caps_long_output(tmp_path, monkeypatch):
    from agent_tools import contracts
    from agent_tools.types import ToolContext
    import api_contracts.service as service_module
    import infra.remote_consent as consent

    service = ContractService(ContractStore(tmp_path / "contracts"))
    monkeypatch.setattr(service_module, "contract_service", service)
    monkeypatch.setattr(consent, "remote_source_allowed", lambda *_args, **_kw: True)
    source = service.store.create_source("backend", "upload", "Shop", {})
    service.ingest_upload(source["source_id"], json.dumps(DOCUMENT).encode())
    service.store.attach(source["source_id"], "web", "consumer")
    context = ToolContext(workflow="chat", project_id="web")

    output = contracts._generate({"target": "models"}, context)
    assert "### models.ts\n```ts" in output and "export interface Order {" in output and "target models" in output
    monkeypatch.setattr(contracts, "_MAX_GENERATED_CHARS", 1500)
    capped = contracts._generate({"target": "react"}, context)
    assert "were left out because the output is too long" in capped and "### hooks.ts" not in capped
