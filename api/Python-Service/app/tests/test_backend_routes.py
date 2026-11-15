"""Back-end routes and the provider check (front-end assistance plan, Phase 4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api_contracts.conformance import provider_report
from api_contracts.model import load_contract
from api_contracts.service import ContractInputError, ContractService
from api_contracts.store import ContractStore
from projects.backend_routes import backend_routes


def write(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


LARAVEL = {
    "composer.json": json.dumps({"require": {"laravel/framework": "^11.0"}}),
    "routes/api.php": """<?php
use App\\Http\\Controllers\\OrderController;
Route::prefix('v1')->group(function () {
    Route::get('/orders', [OrderController::class, 'index']);
    Route::post('/orders', [OrderController::class, 'store']);
    Route::get('/orders/{order}', [OrderController::class, 'show']);
    Route::delete('/orders/{order}', [OrderController::class, 'destroy']);
    Route::post('/orders/{order}/refund', [OrderController::class, 'refund']);
});
""",
    "routes/web.php": "<?php\nRoute::get('/', fn () => view('home'));\nRoute::view('/about', 'about');\n"
                      "Route::get('/dashboard', [DashboardController::class, 'show']);\n",
}

SYMFONY = {
    "composer.json": json.dumps({"require": {"symfony/framework-bundle": "^7.0"}}),
    "src/Controller/InvoiceController.php": """<?php
namespace App\\Controller;
use Symfony\\Component\\Routing\\Attribute\\Route;

#[Route('/api/invoices', name: 'invoice_')]
final class InvoiceController
{
    #[Route('', name: 'list', methods: ['GET'])]
    public function list() {}

    #[Route('/{id}', name: 'show', methods: ['GET', 'HEAD'])]
    public function show(int $id) {}

    #[Route(path: '/{id}/pay', methods: 'POST')]
    public function pay(int $id) {}
}
""",
    "src/Controller/LegacyController.php": """<?php
namespace App\\Controller;
class LegacyController
{
    /**
     * @Route("/api/legacy/ping", methods={"GET"})
     */
    public function ping() {}

    /** @Route("/status") */
    public function status() {}
}
""",
    "config/routes.yaml": "health:\n  path: /api/health\n  controller: App\\Controller\\HealthController\n  methods: [GET]\n"
                          "controllers:\n  resource: ../src/Controller/\n  type: attribute\n",
    "vendor/x/Controller.php": "<?php\n#[Route('/vendor/route')]\nclass X { public function a() {} }\n",
}


def test_laravel_routes_get_the_api_prefix_and_skip_views(tmp_path):
    result = backend_routes(write(tmp_path, LARAVEL))
    assert result["framework"] == "laravel"
    assert [(r["method"], r["path"]) for r in result["routes"]] == [
        ("GET", "/"), ("DELETE", "/api/v1/orders/{order}"), ("GET", "/api/v1/orders/{order}"),
        ("POST", "/api/v1/orders/{order}/refund"), ("GET", "/api/v1/orders"), ("POST", "/api/v1/orders"),
        ("GET", "/dashboard")] or sorted((r["method"], r["path"]) for r in result["routes"]) == sorted([
        ("GET", "/"), ("DELETE", "/api/v1/orders/{order}"), ("GET", "/api/v1/orders/{order}"),
        ("POST", "/api/v1/orders/{order}/refund"), ("GET", "/api/v1/orders"), ("POST", "/api/v1/orders"),
        ("GET", "/dashboard")])
    show = next(r for r in result["routes"] if r["path"] == "/api/v1/orders/{order}" and r["method"] == "GET")
    assert show["file"] == "routes/api.php" and show["api"] is True and show["action"].endswith("OrderController@show")

    (tmp_path / "bootstrap").mkdir()
    (tmp_path / "bootstrap/app.php").write_text("<?php ->withRouting(api: __DIR__.'/../routes/api.php', apiPrefix: 'rest')")
    assert any(r["path"] == "/rest/v1/orders" for r in backend_routes(tmp_path)["routes"])


def test_symfony_routes_from_attributes_annotations_and_yaml(tmp_path):
    result = backend_routes(write(tmp_path, SYMFONY))
    assert result["framework"] == "symfony"
    routes = {(r["method"], r["path"]): r for r in result["routes"]}
    assert set(routes) == {("GET", "/api/invoices"), ("GET", "/api/invoices/{id}"), ("HEAD", "/api/invoices/{id}"),
                           ("POST", "/api/invoices/{id}/pay"), ("GET", "/api/legacy/ping"), ("ANY", "/status"),
                           ("GET", "/api/health")}                     # vendor code is not read
    pay = routes[("POST", "/api/invoices/{id}/pay")]
    assert pay["action"] == "InvoiceController::pay" and pay["file"] == "src/Controller/InvoiceController.php" and pay["line"] == 14
    assert routes[("GET", "/api/health")]["file"] == "config/routes.yaml"


def test_projects_without_a_supported_back_end_say_so(tmp_path):
    result = backend_routes(write(tmp_path, {"package.json": "{}"}))
    assert result["framework"] is None and result["routes"] == [] and result["notes"]


def contract(base: str | None = "/api/v1") -> dict:
    ok = {"200": {"description": "ok"}}
    document = {"openapi": "3.0.3", "info": {"title": "Shop", "version": "1"}, "paths": {
        "/orders": {"get": {"responses": ok}, "post": {"responses": ok}},
        "/orders/{id}": {"get": {"responses": ok}, "put": {"responses": ok}},
        "/customers": {"get": {"responses": ok}},
    }}
    if base:
        document["servers"] = [{"url": f"https://shop.example{base}"}]
    return load_contract(json.dumps(document).encode())[0]


def findings(report: dict) -> list[tuple[str, str]]:
    return sorted((f["code"], f["evidence"].get("route") or f["evidence"].get("operation")) for f in report["findings"])


@pytest.mark.parametrize("base", ["/api/v1", None])
def test_provider_report_judges_only_api_routes(tmp_path, base):
    report = provider_report(backend_routes(write(tmp_path, LARAVEL)), contract(base))
    assert findings(report) == [
        ("undocumented_route", "DELETE /api/v1/orders/{order}"),           # the path exists, the method does not
        ("undocumented_route", "POST /api/v1/orders/{order}/refund"),
        ("unimplemented_operation", "GET /customers"),
        ("unimplemented_operation", "PUT /orders/{id}"),
    ]
    severities = {f["code"]: f["severity"] for f in report["findings"]}
    assert severities == {"undocumented_route": "warning", "unimplemented_operation": "error"}
    coverage = report["coverage"]
    assert (coverage["routes_total"], coverage["routes_in_scope"], coverage["routes_documented"]) == (7, 5, 3)
    assert (coverage["operations_total"], coverage["operations_implemented"]) == (5, 3)
    assert report["check"] == "provider" and report["summary"] == {"errors": 2, "warnings": 2, "info": 0}
    if base is None:   # no declared base path: the shared prefix is worked out and reported
        assert coverage["inferred_prefix"] == "/api/v1" and any("/api/v1" in note for note in report["notes"])
    refund = next(f for f in report["findings"] if f["evidence"].get("route", "").endswith("/refund"))
    assert refund["location"]["file"] == "routes/api.php" and refund["location"]["symbol"].endswith("@refund")


def test_a_route_for_any_method_implements_every_operation_of_its_path():
    routes = {"routes": [{"method": "ANY", "path": "/api/v1/orders/{id}", "action": "x", "file": "a.php", "line": 1, "api": False},
                         {"method": "GET", "path": "/api/v1/orders", "action": "x", "file": "a.php", "line": 2, "api": False},
                         {"method": "POST", "path": "/api/v1/orders", "action": "x", "file": "a.php", "line": 3, "api": False}]}
    assert findings(provider_report(routes, contract())) == [("unimplemented_operation", "GET /customers")]


def test_service_checks_a_provider_against_the_contract_of_the_same_revision(tmp_path):
    service = ContractService(ContractStore(tmp_path / "contracts"))
    routes = backend_routes(write(tmp_path / "src", LARAVEL))
    loader = lambda project_id, revision_id=None: {"project_id": project_id, "revision_id": revision_id or "rev_new", **routes}  # noqa: E731
    source = service.store.create_source("backend", "repo_path", "Shop", {"path": "openapi.json"})
    provider = service.store.attach(source["source_id"], "backend", "provider")
    assert service.conformance(provider["attachment_id"], routes_loader=loader)["available"] is False

    old_model = contract()
    new_model = {**old_model, "operations": {k: v for k, v in old_model["operations"].items() if k != "GET /customers"}}
    from api_contracts.model import content_hash
    old, _ = service.store.record_valid(source["source_id"], old_model, content_hash(old_model), "rev_old")
    new, _ = service.store.record_valid(source["source_id"], new_model, content_hash(new_model), "rev_new")

    latest = service.conformance(provider["attachment_id"], routes_loader=loader)
    assert latest["role"] == "provider" and latest["framework"] == "laravel" and latest["version"]["version_id"] == new["version_id"]
    assert ("unimplemented_operation", "GET /customers") not in findings(latest)
    earlier = service.conformance(provider["attachment_id"], "rev_old", routes_loader=loader)   # that revision's own contract
    assert earlier["version"]["version_id"] == old["version_id"]
    assert ("unimplemented_operation", "GET /customers") in findings(earlier)
    with pytest.raises(ContractInputError):
        service.impact(provider["attachment_id"])


# ── live traffic ─────────────────────────────────────────────────────────────

def test_runtime_report_compares_routes_and_status_codes():
    from api_contracts.conformance import runtime_report

    ok = {"200": {"description": "ok"}}
    model = load_contract(json.dumps({"openapi": "3.0.3", "info": {"title": "Shop", "version": "1"}, "paths": {
        "/orders": {"get": {"responses": ok}, "post": {"responses": {"201": {"description": "made"}, "4XX": {"description": "bad"}}}},
        "/orders/{id}": {"get": {"responses": {**ok, "default": {"description": "error"}}}},
        "/customers": {"get": {"responses": ok}},
    }}).encode())[0]
    observations = [
        {"method": "GET", "route": "api/v1/orders", "status_code": 200, "requests": 40},
        {"method": "GET", "route": "api/v1/orders", "status_code": 500, "requests": 2},        # not listed
        {"method": "POST", "route": "api/v1/orders", "status_code": 422, "requests": 5},       # covered by 4XX
        {"method": "GET", "route": "api/v1/orders/{order}", "status_code": 404, "requests": 3},  # covered by default
        {"method": "DELETE", "route": "api/v1/orders/{order}", "status_code": 204, "requests": 1},  # method not in contract
        {"method": "GET", "route": "api/v1/reports", "status_code": 200, "requests": 7},       # under the API prefix, unknown
        {"method": "GET", "route": "dashboard", "status_code": 200, "requests": 90},           # a page, not the API
        {"method": "GET", "route": "(unmatched)", "status_code": 404, "requests": 9},
    ]
    report = runtime_report(observations, model)
    assert sorted((f["code"], f["operation"] or f["evidence"]["route"]) for f in report["findings"]) == [
        ("undocumented_route_observed", "DELETE /api/v1/orders/{order}"),
        ("undocumented_route_observed", "GET /api/v1/reports"),
        ("undocumented_status", "GET /orders"),
    ]
    status = next(f for f in report["findings"] if f["code"] == "undocumented_status")
    assert status["evidence"] == {"status": 500, "requests": 2} and "HTTP 500 (2 requests)" in status["message"]
    coverage = report["coverage"]
    assert coverage["operations_observed"] == 3 and coverage["unobserved_operations"] == ["GET /customers"]
    assert coverage["requests_outside_api"] == 90 and coverage["inferred_prefix"] == "/api/v1"
    assert runtime_report([], model)["notes"]


def test_service_reports_runtime_unavailable_instead_of_failing(tmp_path):
    from types import SimpleNamespace

    from api_contracts.model import content_hash
    from api_contracts.runtime_observations import RuntimeUnavailable

    revision = {"revision_id": "rev_" + "a" * 32, "state": "ready"}
    registry = SimpleNamespace(latest_ready=lambda _project: revision, get_by_revision_id=lambda _project, _id: revision)
    service = ContractService(ContractStore(tmp_path / "contracts"), registry=registry)
    source = service.store.create_source("backend", "upload", "Shop", {})
    provider = service.store.attach(source["source_id"], "backend", "provider")
    consumer = service.store.attach(source["source_id"], "web", "consumer")
    model = contract()
    service.store.record_valid(source["source_id"], model, content_hash(model))

    seen = [{"method": "GET", "route": "api/v1/orders", "status_code": 500, "requests": 1},
            {"method": "GET", "route": "api/v1/orders/{o}", "status_code": 200, "requests": 1}]
    report = service.runtime_conformance(provider["attachment_id"], observations_loader=lambda *_args: seen)
    assert report["available"] and report["check"] == "runtime" and report["revision_id"] == revision["revision_id"]
    assert [f["code"] for f in report["findings"]] == ["undocumented_status"]

    def down(*_args):
        raise RuntimeUnavailable("Runtime telemetry is not configured.")

    unavailable = service.runtime_conformance(provider["attachment_id"], observations_loader=down)
    assert unavailable["available"] is False and unavailable["notes"] == ["Runtime telemetry is not configured."]
    with pytest.raises(ContractInputError):
        service.runtime_conformance(consumer["attachment_id"], observations_loader=lambda *_args: seen)


def test_runtime_query_uses_the_revision_scope_and_reports_failures():
    from api_contracts.runtime_observations import RuntimeUnavailable, observed_responses

    class Runtime:
        configured, database, table, window_hours = True, "otel", "otel_spans_full", 24

        def rows(self, sql, project_id, revision_id):
            self.sql, self.scope = sql, (project_id, revision_id)
            return [{"method": "GET", "route": "api/orders", "status_code": 200, "requests": "12"}]

    runtime = Runtime()
    assert observed_responses("backend", "rev_1", runtime=runtime) == [
        {"method": "GET", "route": "api/orders", "status_code": 200, "requests": 12}]
    assert runtime.scope == ("backend", "rev_1")
    assert "ProjectId = {project_id:String}" in runtime.sql and "SpanKind = 'Server'" in runtime.sql
    assert "GROUP BY method, route, status_code" in runtime.sql and "INTERVAL 24 HOUR" in runtime.sql

    class Broken(Runtime):
        def rows(self, *_args):
            raise OSError("connection refused")

    with pytest.raises(RuntimeUnavailable):
        observed_responses("backend", "rev_1", runtime=Broken())
    with pytest.raises(RuntimeUnavailable):
        observed_responses("backend", "rev_1", runtime=type("Off", (), {"configured": False})())


def test_a_declared_base_path_is_never_widened_by_a_guessed_prefix():
    # Seen on a real project: admin routes that end like public ones must not count as documented.
    routes = {"routes": [
        {"method": "GET", "path": "/api/v1/orders", "action": "a", "file": "routes/api.php", "line": None, "api": True},
        {"method": "GET", "path": "/api/v1/admin/orders", "action": "b", "file": "routes/api.php", "line": None, "api": True},
        {"method": "GET", "path": "/api/v1/admin/orders/{id}", "action": "c", "file": "routes/api.php", "line": None, "api": True},
        {"method": "GET", "path": "/api/v1/admin/customers", "action": "d", "file": "routes/api.php", "line": None, "api": True},
    ]}
    report = provider_report(routes, contract("/api/v1"))
    assert report["coverage"]["inferred_prefix"] is None and report["coverage"]["routes_documented"] == 1
    assert sum(1 for f in report["findings"] if f["code"] == "undocumented_route") == 3
