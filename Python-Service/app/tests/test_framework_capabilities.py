"""Framework adapter layer (gap-closing plan, Phase 0b).

Decision: framework-knowledge-lives-in-plugins-over-language-facts. Language facts come from
the language's own parser (PHP CLI ``facts``), framework plugins turn them into normalized
records through capability providers, and Code Quality checks read only those records.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest

from agent.core.capability import Capability
from agent.core.facts import (
    PhpFacts,
    argument,
    facts_for_workspace,
    literal_items,
    literal_str,
    literal_strings,
    load_facts,
    store_facts,
)
from agent.core.records import ConfigRiskRecord, GuardRecord, RouteRecord
from agent.core.registry import PluginRegistry
from agent.core.revision import RevisionContext, application_root, is_secret_path
from agent.core.types import DetectionResult, RuntimeInspection
from agent.interfaces.capabilities import CapabilityUnavailable
from code_quality.framework_evidence import FrameworkCheck, run_framework_checks, run_on_archive

CONTROLLER = """<?php

declare(strict_types=1);

namespace App\\Controller;

use Symfony\\Component\\Routing\\Attribute\\Route;
use Symfony\\Component\\Security\\Http\\Attribute\\IsGranted;

#[Route('/api/users')]
final class UserController extends BaseController
{
    public function __construct(private readonly Mailer $mailer)
    {
    }

    #[Route(self::SHOW, methods: ['GET'])]
    #[IsGranted('ROLE_ADMIN')]
    public function show(int $id): array
    {
        $this->denyAccessUnlessGranted('ROLE_USER');
        return Route::get('/x', [self::class, 'show'])->name('x');
    }
}
"""

BASE = """<?php

namespace App\\Controller;

abstract class BaseController
{
    public const SHOW = '/{id}';
}
"""

ROUTES = """<?php

use Illuminate\\Support\\Facades\\Route;

Route::middleware(['auth'])->group(function () {
    Route::get('/pizzas', [PizzaController::class, 'index']);
});
return ['debug' => env('APP_DEBUG', false)];
"""


def _php_cli_available() -> bool:
    from infra.settings import settings

    return shutil.which("php") is not None and Path(settings.php_cli_root, "bin", "project-inspect").is_file()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "wrapper" / "app"
    (root / "src/Controller").mkdir(parents=True)
    (root / "routes").mkdir()
    (root / "composer.json").write_text(json.dumps({"require": {"symfony/framework-bundle": "6.3.*"}}))
    (root / "src/Controller/UserController.php").write_text(CONTROLLER)
    (root / "src/Controller/BaseController.php").write_text(BASE)
    (root / "routes/api.php").write_text(ROUTES)
    (root / ".env").write_text("APP_SECRET=do-not-read")
    return tmp_path / "wrapper"


# ─── 6a: the PHP facts export ────────────────────────────────────────────────

@pytest.mark.skipif(not _php_cli_available(), reason="PHP CLI tools are not installed here")
def test_php_facts_export_resolves_attributes_constants_promoted_properties_and_chains(project):
    from infra.php_cli import PhpCli

    payload = PhpCli().inspect_facts(str(project / "app"))
    facts = PhpFacts(payload)
    controller = facts.class_("App\\Controller\\UserController")
    assert controller["extends"] == ["App\\Controller\\BaseController"]
    show = next(m for m in controller["methods"] if m["name"] == "show")
    route, granted = show["attributes"]
    assert route["name"] == "Symfony\\Component\\Routing\\Attribute\\Route"
    # self::SHOW resolves through the parent class's constant.
    assert literal_str(argument(route["args"], 0), facts) == "/{id}"
    assert literal_strings(argument(route["args"], 1, "methods")) == ["GET"]
    assert literal_str(argument(granted["args"], 0)) == "ROLE_ADMIN"
    assert [(p["name"], p.get("promoted")) for p in controller["properties"]] == [("$mailer", True)]
    calls = {call["name"]: call for call, _f in facts.calls("src/Controller/UserController.php")}
    assert calls["denyAccessUnlessGranted"]["receiver"] == "this"
    assert calls["denyAccessUnlessGranted"]["scope"] == "App\\Controller\\UserController::show"
    assert calls["get"]["chain"][0]["name"] == "name"
    routes = list(facts.calls("routes/api.php"))
    group, inner = routes[0][0], routes[1][0]
    assert group["name"] == "middleware" and group["chain"][0]["name"] == "group"
    assert inner["name"] == "get" and inner["parent"] == group["id"]
    config = facts.file("routes/api.php")["returns"]
    assert literal_items(config)[0][1] == {"call": {"name": "env", "args": [
        {"name": None, "value": "APP_DEBUG"}, {"name": None, "value": False}]}}
    assert facts.strict_types_ratio("src/") == (1, 2)


# ─── facts helpers and store ────────────────────────────────────────────────

def _facts_payload() -> dict:
    return {"schema": "php-facts-3", "files": [
        {"path": "a.php", "strict_types": True, "classes": [
            {"fqn": "A\\Child", "extends": ["A\\Base"], "implements": [], "constants": [], "methods": []}]},
        {"path": "b.php", "strict_types": False, "classes": [
            {"fqn": "A\\Base", "extends": [], "implements": ["A\\Iface"], "constants": [], "methods": []}]},
        {"path": "c.php", "classes": [
            {"fqn": "A\\Iface", "extends": [], "implements": [], "constants": [{"name": "P", "value": "/p"}],
             "methods": []}]},
    ]}


def test_constants_follow_extends_and_interfaces():
    facts = PhpFacts(_facts_payload())
    assert facts.constant("A\\Child", "P") == "/p"
    assert literal_str({"const": {"class": "A\\Child", "name": "P"}}, facts) == "/p"
    assert literal_str({"const": {"class": "A\\Child", "name": "NOPE"}}, facts) is None
    assert facts.ancestors("A\\Child") == ["A\\Base"]
    assert literal_strings({"array": [{"key": None, "value": "GET"}, {"key": None, "value": {"expr": "$m"}}]}) == ["GET"]


def test_unknown_schema_is_rejected():
    with pytest.raises(ValueError):
        PhpFacts({"schema": "php-facts-0", "files": []})


def test_store_round_trip_and_stored_facts_skip_the_export(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTS_DIR", str(tmp_path / "facts"))
    sha = "a" * 64
    assert store_facts(sha, "php", _facts_payload())
    assert load_facts(sha, "php")["files"][0]["path"] == "a.php"
    assert not store_facts("not-a-sha", "php", _facts_payload())
    monkeypatch.setattr("agent.core.facts.export_php_facts", lambda root: pytest.fail("export must not run"))
    facts = facts_for_workspace(tmp_path, sha)
    assert facts.class_("A\\Base") is not None


def test_workspace_without_php_has_no_facts(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTS_DIR", str(tmp_path / "facts"))
    (tmp_path / "main.go").write_text("package main")
    assert facts_for_workspace(tmp_path, "b" * 64) is None


# ─── revision context ─────────────────────────────────────────────────────────

def test_context_finds_the_application_root_and_never_reads_secrets(project):
    assert application_root(project) == project / "app"
    context = RevisionContext.from_directory(project, facts_loader=lambda language: "facts:" + language)
    assert "src/Controller/UserController.php" in context.files
    assert context.read_text(".env") is None and is_secret_path(".env") and not is_secret_path(".env.example")
    assert context.read_text("../../etc/passwd") is None
    assert context.glob(".php", under="src") == ["src/Controller/BaseController.php", "src/Controller/UserController.php"]
    assert context.facts("php") == "facts:php"


# ─── 6c/6d: providers and the registry ──────────────────────────────────────

class _Plugin:
    language = "php"

    def __init__(self, name, *, detected=True, capabilities=(), records=None, error=None):
        self.name = name
        self.capabilities = tuple(capabilities)
        self._detected = detected
        self._records = records or []
        self._error = error

    def detect(self, project):
        return DetectionResult(name=self.name, confidence=0.9 if self._detected else 0.0)

    def analyze(self, workspace):
        raise NotImplementedError

    def inspect_runtime(self, runtime):
        return RuntimeInspection(name=self.name, version=None, capabilities=self.capabilities, detail="")

    def get_tools(self):
        return []

    def get_validators(self):
        return []


class _Provider(_Plugin):
    def provide(self, capability, context):
        if self._error:
            raise self._error
        return self._records


def _route(framework="alpha"):
    return RouteRecord(framework=framework, methods=("GET",), path="/x", handler="A::x", file="a.php", line=3)


def _registry(*plugins):
    registry = PluginRegistry()
    for plugin in plugins:
        registry.register_framework(plugin)
    return registry


def test_collect_merges_providers_and_names_frameworks_that_cannot_provide(project):
    context = RevisionContext.from_directory(project, facts_loader=lambda _l: None)
    registry = _registry(
        _Provider("alpha", capabilities=[Capability.ROUTE_ANALYSIS], records=[_route()]),
        _Plugin("beta", capabilities=[Capability.ROUTE_ANALYSIS]),              # advertises, no provider
        _Provider("gamma", capabilities=[]),                                     # provider, not advertised
        _Provider("delta", capabilities=[Capability.ROUTE_ANALYSIS],
                  error=CapabilityUnavailable("facts export failed")),
        _Provider("epsilon", capabilities=[Capability.ROUTE_ANALYSIS], records=["not a record"]),
        _Provider("zeta", detected=False, capabilities=[Capability.ROUTE_ANALYSIS], records=[_route("zeta")]),
    )
    result = registry.collect(Capability.ROUTE_ANALYSIS, context)
    assert result.providers == ["alpha"] and result.records == [_route()]
    assert result.detected == ["alpha", "beta", "gamma", "delta", "epsilon"]
    reasons = {gap["framework"]: gap["reason"] for gap in result.gaps}
    assert set(reasons) == {"beta", "gamma", "delta", "epsilon"}
    assert reasons["delta"] == "facts export failed"
    assert result.status == "partial"
    assert result.coverage()["records"] == 1


def test_collect_without_any_provider_is_unavailable(project):
    context = RevisionContext.from_directory(project, facts_loader=lambda _l: None)
    result = _registry(_Plugin("beta")).collect(Capability.TRANSLATIONS, context)
    assert result.status == "unavailable" and result.records == []


# ─── 6e: framework-neutral checks and coverage ──────────────────────────────

def _route_without_guard_check():
    def run(collections):
        guarded = {g.handler for g in collections[Capability.ROUTE_AUTHORIZATION].records
                   if isinstance(g, GuardRecord)}
        return [{"file": r.file, "line": r.line, "rule": "route-without-authorization", "category": "security",
                 "severity": "warning", "message": f"{r.path} has no authorization"}
                for r in collections[Capability.ROUTE_ANALYSIS].records if r.handler not in guarded]
    return FrameworkCheck("route-authorization", "test-1",
                          (Capability.ROUTE_ANALYSIS, Capability.ROUTE_AUTHORIZATION), run)


def test_checks_read_records_from_any_framework_and_report_gaps(project):
    context = RevisionContext.from_directory(project, facts_loader=lambda _l: None)
    guard = GuardRecord(framework="alpha", handler="A::y", route=None, kind="attribute", requirement="ROLE_X",
                        public=False, file="a.php", line=9)

    class Alpha(_Provider):
        def provide(self, capability, ctx):
            return [_route()] if capability == Capability.ROUTE_ANALYSIS else [guard]

    registry = _registry(Alpha("alpha", capabilities=[Capability.ROUTE_ANALYSIS, Capability.ROUTE_AUTHORIZATION]),
                         _Plugin("beta", capabilities=[Capability.ROUTE_ANALYSIS]))
    findings, coverage = run_framework_checks(context, [_route_without_guard_check()], registry)
    assert [(f["rule"], f["tool"], f["tool_version"]) for f in findings] == [
        ("route-without-authorization", "route-authorization", "test-1")]
    assert coverage["checks"][0]["status"] == "partial"
    by_capability = {row["capability"]: row for row in coverage["capabilities"]}
    assert by_capability["route_analysis"]["gaps"][0]["framework"] == "beta"


def test_check_without_providers_is_unavailable_and_failing_check_is_reported(project):
    context = RevisionContext.from_directory(project, facts_loader=lambda _l: None)
    findings, coverage = run_framework_checks(context, [_route_without_guard_check()], _registry(_Plugin("beta")))
    assert findings == [] and coverage["checks"][0]["status"] == "unavailable"

    def boom(_collections):
        raise RuntimeError("bug")

    registry = _registry(_Provider("alpha", capabilities=[Capability.CONFIG_RISKS],
                                   records=[ConfigRiskRecord("alpha", "r", "security", "warning", "m", "a", 1)]))
    _, coverage = run_framework_checks(context, [FrameworkCheck("broken", "1", (Capability.CONFIG_RISKS,), boom)],
                                       registry)
    assert coverage["checks"][0]["status"] == "failed"


def test_no_registered_checks_changes_nothing():
    assert run_on_archive(b"not a zip", [], checks=[]) == ([], {"checks": [], "capabilities": []})


def test_archive_runner_extracts_inventoried_source_and_config_but_not_secrets(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("app/composer.json", "{}")
        archive.writestr("app/src/A.php", "<?php")
        archive.writestr("app/config/packages/security.yaml", "security: {}")
        archive.writestr("app/.env", "SECRET=1")
        archive.writestr("app/vendor/x/composer.json", "{}")
    seen = {}

    def run(collections):
        return []

    class Spy(PluginRegistry):
        def collect(self, capability, context, min_confidence=0.5):
            seen["files"] = context.files
            return super().collect(capability, context, min_confidence)

    run_on_archive(buffer.getvalue(), ["app/src/A.php"],
                   checks=[FrameworkCheck("spy", "1", (Capability.TEMPLATES,), run)], registry=Spy())
    assert seen["files"] == ("composer.json", "config/packages/security.yaml", "src/A.php")
