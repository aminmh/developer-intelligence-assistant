"""Contract store: @.archcore/frontend-assist/api-contract-sources-and-versions.spec.md."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_contracts import fetch
from api_contracts.fetch import FetchFailed, FetchRefused, fetch_contract, public_addresses, validate_url
from api_contracts.model import ContractInvalid, diff_models, load_contract
from api_contracts.service import ContractInputError, ContractService, clean_source_input
from api_contracts.store import ContractConflict, ContractStore
from telemetry.registry import SnapshotRegistry
from telemetry.snapshot import Snapshot

OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "Pets", "version": "1.0"},
    "servers": [{"url": "/api"}],
    "paths": {
        "/pets": {"get": {"operationId": "listPets", "summary": "List pets", "responses": {"200": {
            "description": "ok", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": "#/components/schemas/Pet"}}}}}}}},
        "/pets/{id}": {
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "get": {"responses": {"200": {"$ref": "#/components/responses/OnePet"}}},
        },
    },
    "components": {
        "schemas": {"Pet": {"type": "object", "properties": {
            "name": {"type": "string"}, "parent": {"$ref": "#/components/schemas/Pet"}}}},
        "responses": {"OnePet": {"description": "ok", "content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Pet"}}}}},
    },
}

SWAGGER = {
    "swagger": "2.0",
    "info": {"title": "Pets", "version": "1.0"},
    "basePath": "/api",
    "paths": {"/pets": {"post": {
        "parameters": [{"name": "body", "in": "body", "required": True, "schema": {"$ref": "#/definitions/Pet"}},
                       {"name": "dry", "in": "query", "type": "boolean"}],
        "responses": {"201": {"description": "made", "schema": {"$ref": "#/definitions/Pet"}}}}}},
    "definitions": {"Pet": {"type": "object", "properties": {"name": {"type": "string"}}}},
}


def dump(document: dict) -> bytes:
    return json.dumps(document).encode()


def with_extra_operation() -> dict:
    document = json.loads(json.dumps(OPENAPI))
    document["paths"]["/owners"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    return document


# ── model ────────────────────────────────────────────────────────────────────

def test_openapi_and_yaml_give_the_same_model_and_hash():
    model, digest = load_contract(dump(OPENAPI))
    yaml_model, yaml_digest = load_contract(yaml.safe_dump(OPENAPI).encode())
    assert digest == yaml_digest and model == yaml_model
    assert model["dialect"] == "openapi-3.0" and model["base_paths"] == ["/api"]
    assert sorted(model["operations"]) == ["GET /pets", "GET /pets/{id}"]
    one = model["operations"]["GET /pets/{id}"]
    assert one["parameters"] == [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}]
    # A shared response is inlined; schema references use one form; a recursive schema stays a reference.
    assert one["responses"]["200"]["content"]["application/json"] == {"$ref": "#/schemas/Pet"}
    assert model["schemas"]["Pet"]["properties"]["parent"] == {"$ref": "#/schemas/Pet"}


def test_swagger_2_becomes_the_same_shape():
    model, _ = load_contract(dump(SWAGGER))
    operation = model["operations"]["POST /pets"]
    assert model["dialect"] == "swagger-2.0" and model["base_paths"] == ["/api"]
    assert operation["request_body"] == {"required": True, "content": {"application/json": {"$ref": "#/schemas/Pet"}}}
    assert operation["parameters"] == [{"name": "dry", "in": "query", "required": False, "schema": {"type": "boolean"}}]
    assert operation["responses"]["201"]["content"]["application/json"] == {"$ref": "#/schemas/Pet"}


def test_openapi_31_is_accepted():
    model, _ = load_contract(dump({"openapi": "3.1.0", "info": {"title": "T", "version": "1"}}))
    assert model["dialect"] == "openapi-3.1" and model["operations"] == {}


@pytest.mark.parametrize("document, expected", [
    (b"", "empty"),
    (b"[1, 2]", "object"),
    (b"{not json", "not valid JSON or YAML"),
    (dump({"openapi": "4.0.0", "info": {"title": "T", "version": "1"}, "paths": {}}), "must declare"),
    (dump({"openapi": "3.0.0", "paths": {}}), "info.title"),
    (dump({"openapi": "3.0.0", "info": {"title": "T", "version": "1"},
           "paths": {"/a": {"get": {"responses": {"200": {"$ref": "#/components/responses/Nope"}}}}}}), "does not resolve"),
    (dump({"openapi": "3.0.0", "info": {"title": "T", "version": "1"}, "paths": {"/a": {"get": {}}}}), "responses are required"),
    (b"a: &x [1]\nb: *x\n", "aliases"),
])
def test_invalid_documents_are_rejected_with_a_reason(document, expected):
    with pytest.raises(ContractInvalid) as error:
        load_contract(document)
    assert any(expected in message for message in error.value.errors)


def test_a_reference_to_another_file_needs_a_resolver():
    document = json.loads(json.dumps(OPENAPI))
    document["paths"]["/pets"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
        "$ref": "schemas/owner.yaml#/components/schemas/Owner"}
    with pytest.raises(ContractInvalid) as error:
        load_contract(dump(document))
    assert "self-contained" in error.value.errors[0]

    files = {"docs/schemas/owner.yaml": yaml.safe_dump({"components": {"schemas": {
        "Owner": {"type": "object", "properties": {"pet": {"$ref": "#/components/schemas/Pet"}}},
        "Pet": {"type": "string"}}}}).encode()}
    model, _ = load_contract(dump(document), resolver=files.get, base_path="docs/openapi.json")
    assert model["operations"]["GET /pets"]["responses"]["200"]["content"]["application/json"] == {"$ref": "#/schemas/Owner"}
    # The other file's own "Pet" does not overwrite the root document's "Pet".
    assert model["schemas"]["Owner"]["properties"]["pet"] == {"$ref": "#/schemas/Pet_2"}
    assert model["schemas"]["Pet_2"] == {"type": "string"} and model["schemas"]["Pet"]["type"] == "object"


@pytest.mark.parametrize("ref", ["https://example.com/a.yaml#/x", "../../etc/passwd", "/etc/passwd"])
def test_references_cannot_leave_the_project(ref):
    document = json.loads(json.dumps(OPENAPI))
    document["components"]["schemas"]["Pet"] = {"$ref": ref}
    with pytest.raises(ContractInvalid):
        load_contract(dump(document), resolver=lambda _path: b"{}", base_path="openapi.json")


def test_diff_lists_added_removed_and_changed():
    old, _ = load_contract(dump(OPENAPI))
    changed = with_extra_operation()
    del changed["paths"]["/pets/{id}"]
    changed["paths"]["/pets"]["get"]["parameters"] = [{"name": "limit", "in": "query", "schema": {"type": "integer"}}]
    changed["components"]["schemas"]["Pet"]["properties"]["age"] = {"type": "integer"}
    new, _ = load_contract(dump(changed))
    result = diff_models(old, new)
    assert result["operations"] == {"added": ["GET /owners"], "removed": ["GET /pets/{id}"],
                                    "changed": [{"operation": "GET /pets", "parts": ["parameters"]}]}
    assert result["schemas"] == {"added": [], "removed": [], "changed": ["Pet"]}


# ── store ────────────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    return ContractStore(tmp_path / "contracts")


def test_unchanged_document_reuses_the_version_and_failure_keeps_it_current(store):
    source = store.create_source("backend", "upload", "Pets", {})
    model, digest = load_contract(dump(OPENAPI))
    first, created = store.record_valid(source["source_id"], model, digest)
    again, created_again = store.record_valid(source["source_id"], model, digest)
    assert created and not created_again and again["version_id"] == first["version_id"]
    assert first["changes"]["first_version"] is True

    store.record_failure(source["source_id"], "failed", ["broken"])
    store.record_failure(source["source_id"], "failed", ["broken"])  # the same failure is one row
    assert store.newest_valid_version(source["source_id"])["version_id"] == first["version_id"]
    assert store.get_source(source["source_id"])["status"] == "failed"
    assert [row["state"] for row in store.list_versions(source["source_id"])] == ["failed", "valid"]

    new_model, new_digest = load_contract(dump(with_extra_operation()))
    second, created = store.record_valid(source["source_id"], new_model, new_digest)
    assert created and second["changes"]["operations"]["added"] == ["GET /owners"]
    assert store.get_source(source["source_id"])["status"] == "ok"


def test_attachments_follow_latest_or_pin_and_protect_pinned_versions(store):
    source = store.create_source("backend", "upload", "Pets", {})
    follow = store.attach(source["source_id"], "web", "consumer")
    assert store.resolve(follow) is None  # explicit unavailable state: nothing valid yet
    with pytest.raises(ValueError):
        store.attach(source["source_id"], "admin", "consumer", "pinned", "cv_missing")

    model, digest = load_contract(dump(OPENAPI))
    first, _ = store.record_valid(source["source_id"], model, digest)
    pinned = store.attach(source["source_id"], "admin", "consumer", "pinned", first["version_id"])
    store.attach(source["source_id"], "backend", "provider")
    new_model, new_digest = load_contract(dump(with_extra_operation()))
    second, _ = store.record_valid(source["source_id"], new_model, new_digest)

    assert store.resolve(follow)["version_id"] == second["version_id"]
    assert store.resolve(pinned)["version_id"] == first["version_id"]
    assert len(store.list_attachments(source_id=source["source_id"])) == 3  # several per source
    with pytest.raises(ContractConflict):
        store.attach(source["source_id"], "web", "consumer")
    with pytest.raises(ContractConflict) as error:
        store.delete_version(source["source_id"], first["version_id"])
    assert "admin (consumer)" in str(error.value)
    with pytest.raises(ContractConflict):
        store.delete_source(source["source_id"])  # other projects are attached

    store.update_attachment(pinned["attachment_id"], "follow_latest")
    store.delete_version(source["source_id"], first["version_id"])
    assert store.resolve(store.get_attachment(pinned["attachment_id"]))["version_id"] == second["version_id"]


# ── URL fetching ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", ["http://example.com/a.json", "https://user:pw@example.com/a.json", "ftp://example.com/a",
                                 "https:///a.json", "", None])
def test_urls_must_be_https_without_credentials(url):
    with pytest.raises(FetchRefused):
        validate_url(url)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254", "::1",
                                     "fe80::1", "fd00::1", "::ffff:10.0.0.1", "100.64.0.1", "0.0.0.0"])
def test_non_public_addresses_are_refused(address):
    with pytest.raises(FetchRefused):
        public_addresses("internal.example", 443, lambda _host, _port: [address])


def test_a_host_with_any_private_address_is_refused():
    with pytest.raises(FetchRefused):
        public_addresses("mixed.example", 443, lambda _host, _port: ["93.184.216.34", "10.0.0.1"])
    assert public_addresses("ok.example", 443, lambda _host, _port: ["93.184.216.34"]) == ["93.184.216.34"]


def test_redirects_are_rechecked_and_limited():
    public = lambda host, _port: ["10.0.0.1"] if host == "internal.example" else ["93.184.216.34"]  # noqa: E731
    calls = []

    def to_internal(url, host, port, address, deadline):
        calls.append((host, address))
        return 302, "https://internal.example/secret", b""

    with pytest.raises(FetchRefused):
        fetch_contract("https://docs.example/openapi.json", resolver=public, request=to_internal)
    assert calls == [("docs.example", "93.184.216.34")]  # the internal hop was never contacted

    with pytest.raises(FetchRefused):  # a redirect may not downgrade to HTTP
        fetch_contract("https://docs.example/a", resolver=public,
                       request=lambda *_args: (301, "http://docs.example/a", b""))
    with pytest.raises(FetchFailed) as error:
        fetch_contract("https://docs.example/a", resolver=public, request=lambda *_args: (302, "/again", b""))
    assert "more than 3" in str(error.value)

    hops = iter([(302, "/v2/openapi.json", b""), (200, None, b"{}")])
    assert fetch_contract("https://docs.example/a", resolver=public, request=lambda *_args: next(hops)) == b"{}"


def test_fetch_limits_match_the_spec():
    assert fetch.TIMEOUT_SECONDS == 15 and fetch.MAX_REDIRECTS == 3


# ── service ──────────────────────────────────────────────────────────────────

class MemoryStorage:
    def __init__(self):
        self.objects = {}

    def get_snapshot_archive(self, key):
        return self.objects[key]


def revision_zip(files: dict[str, bytes], wrapper: str = "") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name, data in files.items():
            bundle.writestr(wrapper + name, data)
    return output.getvalue()


@pytest.fixture
def backend(tmp_path):
    registry = SnapshotRegistry(tmp_path / "registry")
    storage = MemoryStorage()
    service = ContractService(ContractStore(tmp_path / "contracts"), registry=registry, storage=storage,
                              fetcher=lambda _url: dump(OPENAPI))

    def add_revision(commit: str, files: dict[str, bytes], *, dirty: bool = False, wrapper: str = "") -> dict:
        import hashlib
        data = revision_zip(files, wrapper)
        snapshot = Snapshot("backend", None if dirty else commit, hashlib.sha256(data).hexdigest(), dirty=dirty)
        registry.register(snapshot)
        key = f"{snapshot.key}.zip"
        storage.objects[key] = data
        registry.transition(snapshot, "ready", archive_key=key)
        service.on_revision_ready("backend", snapshot.reference)
        return registry.get("backend", snapshot.reference)

    service.add_revision = add_revision
    return service


def test_repo_path_follows_backend_revisions(backend):
    source = backend.store.create_source("backend", "repo_path", "Pets", {"path": "docs/openapi.json"})
    consumer = backend.store.attach(source["source_id"], "web", "consumer")

    first = backend.add_revision("a" * 40, {"docs/openapi.json": dump(OPENAPI), "index.php": b"<?php"}, wrapper="repo-main/")
    version = backend.store.resolve(consumer)
    assert version["revision_id"] == first["revision_id"] and version["operation_count"] == 2

    # Same contract in the next revision: no new version, but the revision still maps to it.
    second = backend.add_revision("b" * 40, {"docs/openapi.json": dump(OPENAPI), "index.php": b"<?php 2"})
    assert backend.store.resolve(consumer)["version_id"] == version["version_id"]
    assert backend.version_for_revision(source["source_id"], second["revision_id"])["version"]["version_id"] == version["version_id"]

    third = backend.add_revision("c" * 40, {"docs/openapi.json": dump(with_extra_operation())})
    newest = backend.store.resolve(consumer)
    assert newest["revision_id"] == third["revision_id"]
    assert newest["changes"]["operations"]["added"] == ["GET /owners"]

    # The file disappears: the source is marked missing and the last valid version stays current.
    fourth = backend.add_revision("d" * 40, {"index.php": b"<?php 4"})
    assert backend.store.get_source(source["source_id"])["status"] == "missing"
    assert backend.store.resolve(consumer)["version_id"] == newest["version_id"]
    assert backend.version_for_revision(source["source_id"], fourth["revision_id"]) == {
        "available": False, "state": "missing", "version": None}

    # An uncommitted working tree never becomes the contract other projects follow.
    backend.add_revision("", {"docs/openapi.json": dump(SWAGGER)}, dirty=True)
    assert backend.store.resolve(consumer)["version_id"] == newest["version_id"]


def test_repo_path_resolves_files_inside_the_revision_only(backend):
    source = backend.store.create_source("backend", "repo_path", "Pets", {"path": "docs/openapi.json"})
    document = json.loads(json.dumps(OPENAPI))
    document["components"]["schemas"]["Owner"] = {"$ref": "./parts/owner.json#/components/schemas/Owner"}
    owner = {"components": {"schemas": {"Owner": {"type": "object"}}}}
    backend.add_revision("a" * 40, {"docs/openapi.json": dump(document), "docs/parts/owner.json": dump(owner)})
    schemas = backend.store.newest_valid_version(source["source_id"], with_model=True)["model"]["schemas"]
    # The root "Owner" points at the other file's "Owner", which is brought in under its own name.
    assert schemas["Owner"] == {"$ref": "#/schemas/Owner_2"} and schemas["Owner_2"] == {"type": "object"}

    document["components"]["schemas"]["Owner"] = {"$ref": "../../outside.json#/x"}
    backend.add_revision("b" * 40, {"docs/openapi.json": dump(document)})
    assert backend.store.get_source(source["source_id"])["status"] == "failed"
    assert "leaves the project" in backend.store.get_source(source["source_id"])["last_errors"][0]


def test_upload_and_url_sources(backend):
    upload = backend.store.create_source("backend", "upload", "Uploaded", {})
    assert backend.ingest_upload(upload["source_id"], dump(OPENAPI))["outcome"] == "created"
    assert backend.ingest_upload(upload["source_id"], yaml.safe_dump(OPENAPI).encode())["outcome"] == "unchanged"
    assert backend.ingest_upload(upload["source_id"], b"{")["outcome"] == "failed"
    assert backend.store.newest_valid_version(upload["source_id"]) is not None
    with pytest.raises(ContractInputError):
        backend.refresh(upload["source_id"])

    url = backend.store.create_source("backend", "url", "Remote", {"url": "https://docs.example/openapi.json"})
    assert backend.refresh(url["source_id"])["outcome"] == "created"

    def refuse(_url):
        raise FetchRefused("not allowed")

    backend._fetcher = refuse
    result = backend.refresh(url["source_id"])
    assert result["outcome"] == "failed" and result["retriable"] is False
    assert backend.store.newest_valid_version(url["source_id"]) is not None


def test_scheduled_refresh_only_checks_due_url_sources(backend):
    fetched = []
    backend._fetcher = lambda url: fetched.append(url) or dump(OPENAPI)
    backend.store.create_source("backend", "url", "Manual", {"url": "https://a.example/x.json"})
    backend.store.create_source("backend", "url", "Hourly", {"url": "https://b.example/x.json", "refresh_minutes": 60})
    now = datetime.now(timezone.utc)
    assert backend.refresh_due_urls(now) == 1 and fetched == ["https://b.example/x.json"]
    assert backend.refresh_due_urls(now + timedelta(minutes=30)) == 0
    assert backend.refresh_due_urls(now + timedelta(minutes=61)) == 1


def test_source_input_is_validated():
    assert clean_source_input("repo_path", " Pets ", {"path": "./docs\\openapi.yaml"}) == (
        "repo_path", "Pets", {"path": "docs/openapi.yaml"})
    assert clean_source_input("url", "R", {"url": "https://x.example/a", "refresh_minutes": 60})[2]["refresh_minutes"] == 60
    for kind, body in [("repo_path", {"path": "../x.json"}), ("repo_path", {"path": "/abs/x.json"}),
                       ("repo_path", {"path": "docs/readme.md"}), ("url", {"url": "http://x.example/a"}),
                       ("url", {"url": "https://x.example/a", "refresh_minutes": 5}), ("ftp", {})]:
        with pytest.raises(ContractInputError):
            clean_source_input(kind, "Name", body)
    with pytest.raises(ContractInputError):
        clean_source_input("upload", "", {})


# ── HTTP ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(backend, monkeypatch):
    from routers import api_contracts
    monkeypatch.setattr(api_contracts, "contract_service", backend)
    monkeypatch.setattr(api_contracts.settings._env, "internal_secret", "s" * 32)
    app = FastAPI()
    app.include_router(api_contracts.router)
    return TestClient(app, headers={"X-Internal-Secret": "s" * 32})


def test_http_flow_and_project_scoping(client):
    assert TestClient(client.app).get("/contracts/sources").status_code == 401
    created = client.post("/contracts/projects/backend/sources", json={"kind": "upload", "name": "Pets"})
    assert created.status_code == 200
    source_id = created.json()["source"]["source_id"]
    assert client.post("/contracts/projects/backend/sources", json={"kind": "url", "name": "X", "url": "http://x"}).status_code == 400

    # Another project cannot reach the source by ID.
    assert client.post(f"/contracts/projects/web/sources/{source_id}/document", content=dump(OPENAPI)).status_code == 404
    uploaded = client.post(f"/contracts/projects/backend/sources/{source_id}/document", content=dump(OPENAPI))
    assert uploaded.status_code == 200 and uploaded.json()["outcome"] == "created"
    version_id = uploaded.json()["version"]["version_id"]

    detail = client.get(f"/contracts/sources/{source_id}/versions/{version_id}").json()["version"]
    assert [row["operation"] for row in detail["operations"]] == ["GET /pets", "GET /pets/{id}"]
    assert detail["schemas"] == ["Pet"] and "model" not in detail

    attached = client.post("/contracts/projects/web/attachments",
                           json={"source_id": source_id, "role": "consumer", "mode": "pinned", "pinned_version_id": version_id})
    assert attached.status_code == 200 and attached.json()["attachment"]["available"] is True
    attachment_id = attached.json()["attachment"]["attachment_id"]
    assert client.post("/contracts/projects/web/attachments", json={"source_id": source_id, "role": "owner"}).status_code == 400

    overview = client.get("/contracts/projects/web").json()
    assert overview["sources"] == [] and overview["attachments"][0]["source"]["project_id"] == "backend"
    assert client.get("/contracts/projects/backend").json()["sources"][0]["attachments"][0]["project_id"] == "web"

    assert client.delete(f"/contracts/projects/backend/sources/{source_id}/versions/{version_id}").status_code == 409
    assert client.delete(f"/contracts/projects/backend/sources/{source_id}").status_code == 409
    assert client.delete(f"/contracts/projects/backend/attachments/{attachment_id}").status_code == 404
    assert client.put(f"/contracts/projects/web/attachments/{attachment_id}", json={"mode": "follow_latest"}).status_code == 200
    assert client.delete(f"/contracts/projects/web/attachments/{attachment_id}").status_code == 200
    assert client.delete(f"/contracts/projects/backend/sources/{source_id}").status_code == 200
    assert client.get(f"/contracts/sources/{source_id}").status_code == 404


# ── agent tools ──────────────────────────────────────────────────────────────

def test_agent_tools_read_only_attached_contracts(backend, monkeypatch):
    from agent_tools import contracts
    from agent_tools.types import ToolCallRejected, ToolContext
    import api_contracts.service as service_module
    import infra.remote_consent as consent

    monkeypatch.setattr(service_module, "contract_service", backend)
    allowed = {"web"}
    monkeypatch.setattr(consent, "remote_source_allowed", lambda project_id, **_kw: project_id in allowed)

    source = backend.store.create_source("backend", "upload", "Pets", {})
    backend.ingest_upload(source["source_id"], dump(OPENAPI))
    backend.store.attach(source["source_id"], "web", "consumer")
    web = ToolContext(workflow="chat", project_id="web")

    listing = contracts._list_operations({}, web)
    assert "GET /pets — List pets" in listing and "consumer" in listing
    assert "GET /pets/{id}" not in contracts._list_operations({"search": "listPets"}, web)
    detail = json.loads(contracts._read_operation({"method": "get", "path": "/pets"}, web))
    assert detail["schemas"]["Pet"]["type"] == "object"

    # A project without an attachment sees nothing, and consent is checked first.
    assert contracts._list_operations({}, ToolContext(workflow="chat", project_id="web2")).startswith("No API contract") \
        if allowed.add("web2") is None else False
    with pytest.raises(ToolCallRejected) as error:
        contracts._list_operations({}, ToolContext(workflow="chat", project_id="backend"))
    assert error.value.code == "remote_source_not_allowed"
    with pytest.raises(ToolCallRejected):
        contracts._read_operation({"method": "DELETE", "path": "/pets"}, web)
