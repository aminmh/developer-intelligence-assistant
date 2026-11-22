"""Route authorization through the framework adapter layer (gap-closing plan, task 7).

The Symfony plugin reads routes, guards and access rules from PHP facts and security.yaml
(no PHP is parsed in Python); the framework-neutral check decides each route. Fixtures and
expectations are those of the first, regex-based analyzer (branch
feature/route-authorization-analyzer), so the move keeps its behavior.
"""

import io
import shutil
import zipfile
from pathlib import Path

import pytest

from agent import default_registry
from agent.core.capability import Capability
from agent.core.revision import RevisionContext
from code_quality import route_authorization
from code_quality.framework_evidence import run_on_archive


def _php_cli_available() -> bool:
    from infra.settings import settings

    return shutil.which("php") is not None and Path(settings.php_cli_root, "bin", "project-inspect").is_file()


pytestmark = pytest.mark.skipif(not _php_cli_available(), reason="PHP CLI tools are not installed here")

COMPOSER = '{"require": {"php": ">=8.1", "symfony/framework-bundle": "6.3.*"}}'

ROUTES_YAML = """
controllers:
    resource:
        path: ../src/Controller/Api/
        namespace: App\\Controller\\Api
    type: attribute
    prefix: /api
admin:
    resource: ../src/Controller/Admin/
    type: attribute
    prefix: /admin
health:
    path: /health
    controller: App\\Controller\\Api\\HealthController::check
    methods: [GET]
"""

SECURITY_YAML = """
security:
    firewalls:
        dev:
            pattern: ^/(_profiler|_wdt)
            security: false
        hooks:
            pattern: ^/api/hooks
            security: false
        main:
            lazy: true
    access_control:
        - { path: ^/api/files/\\d+$, methods: [GET], roles: PUBLIC_ACCESS }
        - { path: ^/admin, roles: ROLE_ADMIN }
        - { path: ^/api/open, roles: [] }
when@test:
    security:
        access_control:
            - { path: ^/api, roles: ROLE_USER }
"""

BASE_CONTROLLER = """<?php
namespace App\\Controller;

use Symfony\\Bundle\\FrameworkBundle\\Controller\\AbstractController;

abstract class BaseController extends AbstractController
{
    protected const ID_ROUTE = '/{id<\\d+>}';
}
"""

ORDER_CONTROLLER = """<?php
namespace App\\Controller\\Api;

use App\\Controller\\BaseController;
use Symfony\\Component\\Routing\\Annotation\\Route;
use Symfony\\Component\\Security\\Http\\Attribute\\{IsGranted, Security};

#[Route('/orders')]
class OrderController extends BaseController
{
    #[Route('', name: 'order_index', methods: ['GET'])]
    #[IsGranted('ROLE_USER')]
    public function index(): Response
    {
        $sort = fn ($a) => $a;   // a closure must not end the action
        return $this->json([]);
    }

    // #[IsGranted('ROLE_USER')] is commented out here, so it does not protect show()
    #[Route(self::ID_ROUTE, name: 'order_show', methods: ['GET'])]
    public function show(int $id): Response
    {
        return $this->json(['id' => $id, 'text' => "{not a brace}"]);
    }

    #[Route('/{id}/cancel', name: 'order_cancel', methods: ['POST'])]
    public function cancel(int $id): Response
    {
        $this->denyAccessUnlessGranted('ORDER_EDIT', $id);
        return $this->json([]);
    }

    /**
     * @IsGranted("ROLE_ADMIN")
     */
    #[Route('/{id}', name: 'order_delete', methods: ['DELETE'])]
    public function delete(int $id): Response
    {
        return $this->json([]);
    }
}
"""

REPORT_CONTROLLER = """<?php
namespace App\\Controller\\Api;

use Symfony\\Component\\Routing\\Annotation\\Route;
use Symfony\\Component\\Security\\Http\\Attribute\\IsGranted;

#[Route('/reports')]
#[IsGranted('ROLE_ANALYST')]
final class ReportController
{
    #[Route('/{id}', methods: ['GET'])]
    public function show(int $id): Response { return new Response(); }
}
"""

FILE_CONTROLLER = """<?php
namespace App\\Controller\\Api;

use Symfony\\Component\\Routing\\Annotation\\Route;

class FileController
{
    #[Route('/files/{id}', methods: ['GET'])]
    public function download(int $id): Response { return new Response(); }

    #[Route('/files/{id}', methods: ['DELETE'])]
    public function remove(int $id): Response { return new Response(); }

    #[Route('/hooks/stripe', methods: ['POST'])]
    public function stripe(): Response { return new Response(); }

    #[Route('/open/ping', methods: ['GET'])]
    public function ping(): Response { return new Response(); }
}
"""

HEALTH_CONTROLLER = """<?php
namespace App\\Controller\\Api;

class HealthController
{
    public function check(): Response { return new Response(); }
}
"""

# Same short name as the API controller, different namespace and rules.
ADMIN_ORDER_CONTROLLER = """<?php
namespace App\\Controller\\Admin;

use Symfony\\Component\\Routing\\Annotation\\Route;

#[Route('/orders')]
class OrderController
{
    #[Route('/{id}', methods: ['GET'])]
    public function show(int $id): Response { return new Response(); }
}
"""

UNLOADED_CONTROLLER = """<?php
namespace App\\Legacy;

use Symfony\\Component\\Routing\\Annotation\\Route;

class OldController
{
    #[Route('/old', methods: ['GET'])]
    public function old(): Response { return new Response(); }
}
"""


def _archive(files: dict[str, str]) -> tuple[bytes, list[str]]:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue(), list(files)


def _symfony_app(**extra: str) -> dict[str, str]:
    return {
        "composer.json": COMPOSER,
        "config/routes.yaml": ROUTES_YAML,
        "config/packages/security.yaml": SECURITY_YAML,
        "src/Controller/BaseController.php": BASE_CONTROLLER,
        "src/Controller/Api/OrderController.php": ORDER_CONTROLLER,
        "src/Controller/Api/ReportController.php": REPORT_CONTROLLER,
        "src/Controller/Api/FileController.php": FILE_CONTROLLER,
        "src/Controller/Api/HealthController.php": HEALTH_CONTROLLER,
        "src/Controller/Admin/OrderController.php": ADMIN_ORDER_CONTROLLER,
        "src/Legacy/OldController.php": UNLOADED_CONTROLLER,
        **extra,
    }


def _rows(tmp_path, files: dict[str, str] | None = None) -> list[dict]:
    root = tmp_path / "rev"
    for path, content in (files or _symfony_app()).items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)
    context = RevisionContext.from_directory(root)
    registry = default_registry()
    collections = {c: registry.collect(c, context) for c in (Capability.ROUTE_ANALYSIS, Capability.ROUTE_AUTHORIZATION)}
    return route_authorization.evaluate(collections)


def _route(rows: list[dict], method: str, path: str) -> dict:
    matches = [row for row in rows if row["method"] == method and row["path"] == path]
    assert len(matches) == 1, (method, path, [(r["method"], r["path"]) for r in rows])
    return matches[0]


def test_method_attribute_protects_the_route(tmp_path):
    row = _route(_rows(tmp_path), "GET", "/api/orders")
    assert row["status"] == "protected" and row["mechanisms"] == ["attribute"]


def test_class_attribute_protects_every_action_of_the_class(tmp_path):
    row = _route(_rows(tmp_path), "GET", "/api/reports/{id}")
    assert row["status"] == "protected" and row["mechanisms"] == ["attribute"]


def test_deny_call_and_docblock_annotation_protect_the_route(tmp_path):
    rows = _rows(tmp_path)
    assert _route(rows, "POST", "/api/orders/{id}/cancel")["mechanisms"] == ["call"]
    assert _route(rows, "DELETE", "/api/orders/{id}")["mechanisms"] == ["annotation"]


def test_access_rule_protects_the_route_and_is_matched_by_prefix(tmp_path):
    row = _route(_rows(tmp_path), "GET", "/admin/orders/{id}")
    assert row["status"] == "protected" and row["mechanisms"] == ["access-rule"]
    assert row["requirements"] == ["ROLE_ADMIN"]
    # Same short class name as the API OrderController: the admin class is read, not the API one.
    assert row["file"] == "src/Controller/Admin/OrderController.php"
    assert row["handler"] == "App\\Controller\\Admin\\OrderController::show"


def test_public_access_rule_is_public_and_only_for_its_methods(tmp_path):
    rows = _rows(tmp_path)
    download = _route(rows, "GET", "/api/files/{id}")
    assert download["status"] == "public" and "access_control rule 1" in download["public_reason"]
    assert _route(rows, "DELETE", "/api/files/{id}")["status"] == "unprotected"


def test_firewall_without_security_is_public_and_rule_without_roles_does_not_protect(tmp_path):
    rows = _rows(tmp_path)
    hooks = _route(rows, "POST", "/api/hooks/stripe")
    assert hooks["status"] == "public" and "firewall 'hooks'" in hooks["public_reason"]
    assert _route(rows, "GET", "/api/open/ping")["status"] == "unprotected"


def test_unprotected_route_becomes_a_security_finding_at_the_action(tmp_path):
    rows = _rows(tmp_path)
    show = _route(rows, "GET", "/api/orders/{id<\\d+>}")    # path read through the parent's constant
    assert show["status"] == "unprotected"
    root = tmp_path / "rev"
    data, names = _archive(_symfony_app())
    findings, coverage = run_on_archive(data, names, checks=[route_authorization.check()])
    finding = next(row for row in findings if "/api/orders/{id<\\d+>}" in row["message"])
    assert finding["rule"] == route_authorization.RULE_ID and finding["category"] == "security"
    assert finding["file"] == "src/Controller/Api/OrderController.php"
    assert finding["line"] == ORDER_CONTROLLER.splitlines().index("    public function show(int $id): Response") + 1
    assert finding["severity"] == "warning"
    assert finding["tool"] == "route-authorization"
    delete = next(row for row in findings if row["message"].startswith("DELETE /api/files/{id}"))
    assert delete["severity"] == "error"
    summary = coverage["checks"][0]["summary"]
    assert (summary["total"], summary["protected"], summary["public"], summary["unprotected"]) == (11, 5, 2, 4)
    assert summary["groups"]["/admin"] == {"total": 1, "protected": 1, "public": 0, "unprotected": 0,
                                           "access_rule_only": 1}
    assert summary["frameworks"] == ["symfony"]
    assert len(findings) == summary["unprotected"]


def test_yaml_route_uses_its_controller_and_unloaded_files_are_skipped(tmp_path):
    rows = _rows(tmp_path)
    health = _route(rows, "GET", "/health")
    assert health["status"] == "unprotected"
    assert health["file"] == "src/Controller/Api/HealthController.php"
    assert not any(row["path"] == "/old" for row in rows)      # no resource loads src/Legacy


def test_files_outside_the_assessed_list_are_not_read():
    data, names = _archive(_symfony_app())
    names.remove("src/Controller/Api/OrderController.php")
    findings, coverage = run_on_archive(data, names, checks=[route_authorization.check()])
    assert coverage["checks"][0]["summary"]["total"] == 7
    assert not any("/api/orders" in row["message"] for row in findings)


def test_projects_without_a_route_framework_produce_no_findings():
    data, names = _archive({"main.py": "print(1)"})
    findings, coverage = run_on_archive(data, names, checks=[route_authorization.check()])
    assert findings == [] and coverage["capabilities"][0]["detected_frameworks"] == []


def test_missing_security_config_leaves_routes_unprotected(tmp_path):
    files = _symfony_app()
    del files["config/packages/security.yaml"]
    assert _route(_rows(tmp_path, files), "GET", "/admin/orders/{id}")["status"] == "unprotected"


def test_the_check_never_names_a_framework():
    import inspect

    source = inspect.getsource(route_authorization)
    assert "symfony" not in source.lower() and "laravel" not in source.lower()
