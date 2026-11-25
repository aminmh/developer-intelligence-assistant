"""Collectors for process, testing, documentation, localization and accessibility (gap-closing plan, Phase 3)."""

import json

import pytest

from agent.core.capability import Capability
from agent.core.records import RouteRecord, TemplateRecord, TestRecord, TranslationRecord
from agent.core.registry import CapabilityCollection
from agent.core.revision import RevisionContext
from code_quality import (accessibility_checks, documentation_checks, localization_checks, process_checks,
                          testing_checks)
from code_quality.framework_evidence import run_framework_checks


def _context(tmp_path, files: dict[str, str]) -> RevisionContext:
    for path, text in files.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    return RevisionContext.from_directory(tmp_path, facts_loader=lambda _language: None)


def _collection(capability, records=(), providers=("fixture",)):
    return CapabilityCollection(capability=capability, records=list(records), providers=list(providers),
                                detected=list(providers))


def _rules(findings):
    return {(f["rule"], f["file"], f["line"], f["severity"]) for f in findings}


COMPOSER = json.dumps({"require": {"php": ">=8.2"}, "scripts": {"phpstan": "phpstan analyse", "test": "phpunit"}})


# ── process ──────────────────────────────────────────────────────────────────

def test_process_reports_pipelines_ignored_checks_secret_copies_and_version_conflicts(tmp_path):
    context = _context(tmp_path, {
        "composer.json": COMPOSER, ".php-version": "8.2\n",
        ".gitlab-ci.yml": "stages: [test]\nimage: php:8.3-cli\nphpstan:\n  stage: test\n  script: [vendor/bin/phpstan]\n"
                          "  allow_failure: true\nunit:\n  stage: test\n  script:\n    - vendor/bin/phpunit || true\n",
        "Dockerfile": "FROM php:8.2-fpm\nCOPY auth.json composer.json ./\nCOPY . /app\n",
        ".dockerignore": "vendor\n.env.local\n",
    })
    findings, summary = process_checks.run({}, context)
    assert _rules(findings) == {
        ("process-build-context-includes-secrets", "Dockerfile", 3, "warning"),
        ("process-ci-pipeline", ".gitlab-ci.yml", 1, "info"),
        ("process-container-build", "Dockerfile", 1, "info"),
        ("process-ignored-check-failure", ".gitlab-ci.yml", 3, "warning"),
        ("process-ignored-check-failure", ".gitlab-ci.yml", 10, "warning"),
        ("process-secret-copied-into-image", "Dockerfile", 2, "error"),
        ("process-toolchain-version-conflict", ".gitlab-ci.yml", 2, "warning"),
    }
    assert summary["ci_files"] == 1 and "php 8.2 (.php-version)" in summary["toolchains"]
    assert all(f["category"] == "process" for f in findings)


def test_process_without_ci_is_a_warning_and_without_evidence_has_a_reason(tmp_path):
    findings, _ = process_checks.run({}, _context(tmp_path / "a", {"composer.json": COMPOSER}))
    assert _rules(findings) == {("process-no-ci", "composer.json", 1, "warning")}
    findings, summary = process_checks.run({}, _context(tmp_path / "b", {"src/app.py": "print(1)\n"}))
    assert findings == [] and "cannot be assessed" in summary["reason"]


def test_dockerignore_that_excludes_env_files_is_fine(tmp_path):
    context = _context(tmp_path, {"Dockerfile": "FROM node:20\nCOPY . .\n", ".dockerignore": "**/.env*\n", "package.json": "{}"})
    findings, _ = process_checks.run({}, context)
    assert "process-build-context-includes-secrets" not in {f["rule"] for f in findings}


# ── testing ──────────────────────────────────────────────────────────────────

def test_testing_inventory_and_handlers_no_test_reaches(tmp_path):
    context = _context(tmp_path, {"composer.json": COMPOSER, "phpunit.xml.dist": "<phpunit/>", "src/A.php": "<?php\n"})
    tests = [TestRecord("php", "tests/Functional/BookingTest.php", "App\\Tests\\BookingTest", ("testCreate", "testList"),
                        references=("/api/bookings/42", "App\\Entity\\Booking")),
             TestRecord("php", "tests/Unit/EmptyTest.php", "App\\Tests\\EmptyTest", ())]
    routes = [RouteRecord("fixture", ("GET",), "/api/bookings/{id}", "App\\Controller\\BookingController::show", "src/BookingController.php", 12),
              RouteRecord("fixture", ("POST",), "/api/invoices", "App\\Controller\\InvoiceController::create", "src/InvoiceController.php", 20)]
    findings, summary = testing_checks.run({Capability.TEST_CONVENTIONS: _collection(Capability.TEST_CONVENTIONS, tests),
                                            Capability.ROUTE_ANALYSIS: _collection(Capability.ROUTE_ANALYSIS, routes)}, context)
    assert _rules(findings) == {
        ("testing-class-without-tests", "tests/Unit/EmptyTest.php", 1, "warning"),
        ("testing-handler-without-tests", "src/InvoiceController.php", 20, "warning"),
        ("testing-handler-reach", "phpunit.xml.dist", 1, "info"),
        ("testing-inventory", "phpunit.xml.dist", 1, "info"),
        ("testing-areas", "phpunit.xml.dist", 1, "info"),
    }
    assert summary == {"test_classes": 2, "test_methods": 2, "handler_classes": 2, "handlers_without_tests": 1}
    assert all("tests were not executed" in f["message"] for f in findings)


def test_testing_without_tests_or_providers(tmp_path):
    context = _context(tmp_path, {"composer.json": COMPOSER, "src/A.php": "<?php\n"})
    empty = {Capability.TEST_CONVENTIONS: _collection(Capability.TEST_CONVENTIONS),
             Capability.ROUTE_ANALYSIS: _collection(Capability.ROUTE_ANALYSIS)}
    findings, _ = testing_checks.run(empty, context)
    assert _rules(findings) == {("testing-no-tests", "composer.json", 1, "error")}
    none = {capability: _collection(capability, providers=()) for capability in empty}
    findings, summary = testing_checks.run(none, context)
    assert findings == [] and "cannot be assessed" in summary["reason"]


def test_phpunit_configuration_against_the_installed_phpunit(tmp_path):
    from agent.plugins.languages.php.testing import provide_test_config_risks

    lock = json.dumps({"packages": [], "packages-dev": [{"name": "phpunit/phpunit", "version": "9.6.13"}]})
    config = ('<?xml version="1.0"?>\n<phpunit failOnDeprecation="false"\n         bootstrap="tests/bootstrap.php">\n'
              '  <source><include><directory>src</directory></include></source>\n'
              '  <extensions>\n    <bootstrap class="X\\Extension"/>\n  </extensions>\n</phpunit>\n')
    risks = provide_test_config_risks(_context(tmp_path / "old", {"composer.lock": lock, "phpunit.xml.dist": config}))
    risks = [r for r in risks if r.rule == "phpunit-config-version-mismatch"]  # src/ is absent here too (its own test)
    assert sorted((r.rule, r.line, r.details["setting"]) for r in risks) == [
        ("phpunit-config-version-mismatch", 2, "failOnDeprecation"),
        ("phpunit-config-version-mismatch", 4, "<source>"),
        ("phpunit-config-version-mismatch", 5, "<extensions><bootstrap>"),
    ]
    assert all(r.category == "testing" and r.severity == "error" and "9.6.13" in r.message for r in risks)
    lock11 = json.dumps({"packages-dev": [{"name": "phpunit/phpunit", "version": "11.4.0"}]})
    risks = provide_test_config_risks(_context(tmp_path / "new", {"composer.lock": lock11, "phpunit.xml": config.replace(
        "<source>", "<listeners/><source>")}))
    assert [r.details["setting"] for r in risks if r.rule == "phpunit-config-version-mismatch"] == ["<listeners>"]


def test_the_php_plugin_provides_test_config_risks_through_the_registry(tmp_path):
    from agent import default_registry

    lock = json.dumps({"packages-dev": [{"name": "phpunit/phpunit", "version": "9.6.0"}]})
    context = _context(tmp_path, {"composer.json": COMPOSER, "composer.lock": lock, "phpunit.xml": "<phpunit><source/></phpunit>"})
    collection = default_registry().collect(Capability.CONFIG_RISKS, context)
    assert "php" in collection.providers
    assert [r.rule for r in collection.records if r.framework == "php"] == ["phpunit-config-version-mismatch"]


# ── documentation ────────────────────────────────────────────────────────────

def test_documentation_references_are_checked_against_the_revision(tmp_path):
    readme = ("# Shop\n\nNeeds PHP 8.3.\n\n```bash\nmake up\nmake deploy\ncomposer phpstan\ncomposer lint\ncomposer install\n```\n\n"
              "Config lives in `config/app.yaml`; handlers in `src/Missing.php`. See `vendor/x/y.php` and `public/logo.png`.\n")
    context = _context(tmp_path, {"README.md": readme, "Makefile": "up:\n\tdocker compose up\n", "composer.json": COMPOSER,
                                  ".php-version": "8.2\n", "config/app.yaml": "a: 1\n", "docs/adr/0001.md": "# ADR\n"})
    findings, summary = documentation_checks.run({}, context)
    assert _rules(findings) == {
        ("documentation-missing-path", "README.md", 13, "warning"),
        ("documentation-stale-command", "README.md", 7, "warning"),
        ("documentation-stale-command", "README.md", 9, "warning"),
        ("documentation-version-mismatch", "README.md", 3, "warning"),
        ("documentation-file", "README.md", 1, "info"),
        ("documentation-file", "docs/adr/0001.md", 1, "info"),
    }
    assert summary["stale_references"] == 4


def test_missing_readme_is_reported(tmp_path):
    findings, _ = documentation_checks.run({}, _context(tmp_path, {"composer.json": COMPOSER}))
    assert _rules(findings) == {("documentation-no-readme", "composer.json", 1, "warning")}


# ── localization ─────────────────────────────────────────────────────────────

def test_localization_parity_placeholders_identical_values_and_hardcoded_text(tmp_path):
    en = "greeting: 'Hello %name%'\nsave: Save\nterms: Terms and conditions\n"
    nl = "greeting: 'Hallo'\nterms: Terms and conditions\n"
    template = ("<h1>{{ 'greeting'|trans }}</h1>\n<p>Book your lesson now</p>\n<input placeholder=\"Search lessons\">\n"
                "<script>var x = 'not text';</script>\n<span>{{ price|number_format(2) }}</span>\n")
    context = _context(tmp_path, {"translations/messages.en.yml": en, "translations/messages.nl.yml": nl,
                                  "templates/home.html.twig": template})
    records = [TranslationRecord("fixture", "messages", "en", "translations/messages.en.yml",
                                 (("greeting", "Hello %name%"), ("save", "Save"), ("terms", "Terms and conditions"))),
               TranslationRecord("fixture", "messages", "nl", "translations/messages.nl.yml",
                                 (("greeting", "Hallo"), ("terms", "Terms and conditions")))]
    templates = [TemplateRecord("fixture", "twig", "templates/home.html.twig")]
    findings, summary = localization_checks.run({Capability.TRANSLATIONS: _collection(Capability.TRANSLATIONS, records),
                                                 Capability.TEMPLATES: _collection(Capability.TEMPLATES, templates)}, context)
    assert _rules(findings) == {
        ("localization-placeholder-mismatch", "translations/messages.nl.yml", 1, "error"),
        ("localization-hardcoded-text", "templates/home.html.twig", 2, "warning"),
        ("localization-missing-keys", "translations/messages.nl.yml", 1, "warning"),
        ("localization-identical-messages", "translations/messages.nl.yml", 2, "info"),
        ("localization-inventory", "translations/messages.en.yml", 1, "info"),
        ("localization-locale-blind-number", "templates/home.html.twig", 5, "info"),
    }
    assert summary["hardcoded_texts"] == 2 and summary["locales"] == ["en", "nl"]


def test_without_catalogues_localization_is_unassessed(tmp_path):
    findings, summary = localization_checks.run({Capability.TRANSLATIONS: _collection(Capability.TRANSLATIONS),
                                                 Capability.TEMPLATES: _collection(Capability.TEMPLATES)}, _context(tmp_path, {}))
    assert findings == [] and "not assessed" in summary["reason"]


def test_symfony_catalogues_from_yaml_and_xliff(tmp_path):
    from agent.plugins.frameworks.symfony.localization import provide_templates, provide_translations

    xlf = ('<?xml version="1.0"?><xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2"><file><body>'
           '<trans-unit id="1" resname="save"><source>save</source><target>Opslaan</target></trans-unit></body></file></xliff>')
    context = _context(tmp_path, {"translations/messages.en_US.yml": "auth:\n  login: Log in\n",
                                  "translations/validators.nl_NL.xlf": xlf, "templates/email/welcome.html.twig": "<p/>"})
    assert [(r.domain, r.locale, r.messages) for r in provide_translations(context)] == [
        ("messages", "en_US", (("auth.login", "Log in"),)), ("validators", "nl_NL", (("save", "Opslaan"),))]
    assert [(t.engine, t.kind) for t in provide_templates(context)] == [("twig", "email")]


# ── accessibility ────────────────────────────────────────────────────────────

def test_accessibility_lang_alt_tables_and_contrast(tmp_path):
    page = ('<html>\n<body>\n<img src="a.png">\n<img src="b.png" alt="">\n<table><tr><td>1</td></tr></table>\n'
            '<table role="presentation"><tr><td>layout</td></tr></table>\n'
            '<p style="color: #777777; background-color: #ffffff">low</p>\n<p style="color:#000;background:#fff">ok</p>\n'
            '<style>.muted { color: rgb(170,170,170); background: white }</style>\n</body>\n</html>\n')
    context = _context(tmp_path, {"public/index.html": page, "templates/base.html.twig": '<html lang="{{ app.request.locale }}"></html>'})
    templates = [TemplateRecord("fixture", "twig", "templates/base.html.twig")]
    findings, summary = accessibility_checks.run({Capability.TEMPLATES: _collection(Capability.TEMPLATES, templates)}, context)
    assert _rules(findings) == {
        ("accessibility-image-without-alt", "public/index.html", 3, "error"),
        ("accessibility-missing-lang", "public/index.html", 1, "error"),
        ("accessibility-low-contrast", "public/index.html", 7, "warning"),
        ("accessibility-low-contrast", "public/index.html", 9, "warning"),
        ("accessibility-table-without-headers", "public/index.html", 5, "warning"),
        ("accessibility-inventory", "public/index.html", 1, "info"),
    }
    assert summary == {"templates_checked": 2, "engines": {"html": 1, "twig": 1}}
    assert accessibility_checks.contrast("#777777", "#ffffff") == 4.48
    assert accessibility_checks.contrast("#000", "#fff") == 21.0


# ── runner and neutrality ────────────────────────────────────────────────────

def test_evidence_checks_run_without_providers_and_carry_their_category(tmp_path):
    context = _context(tmp_path, {"composer.json": COMPOSER, "README.md": "# Shop\n"})
    findings, coverage = run_framework_checks(context, [process_checks.check(), documentation_checks.check(),
                                                        localization_checks.check()])
    rows = {row["name"]: row for row in coverage["checks"]}
    assert rows["process-evidence"]["category"] == "process" and rows["process-evidence"]["findings"] == 1
    assert rows["localization-evidence"]["summary"]["reason"].startswith("No translation catalogues")
    assert {f["tool"] for f in findings} == {"process-evidence", "documentation-evidence"}


@pytest.mark.parametrize("module", [process_checks, testing_checks, documentation_checks, localization_checks, accessibility_checks])
def test_checks_never_name_a_framework(module):
    import inspect

    source = inspect.getsource(module).lower()
    assert not any(name in source for name in ("laravel", "symfony", "django", "rails", "angular", "react", "vue"))


def test_a_test_path_reaches_a_route_by_its_last_segments():
    assert testing_checks.reaches("/api/tenant/sessions/{id}", "/tenant/sessions/42")
    assert testing_checks.reaches("/api/tenant/sessions/{id}", "/api/tenant/sessions/42?x=1")
    assert not testing_checks.reaches("/api/tenant/sessions/{id}", "/tenant/lessons/42")
    assert not testing_checks.reaches("/api/tenant/sessions", "/sessions")  # one segment is too weak a match
    assert testing_checks.reaches("/health", "/health")


def test_shell_comments_in_code_blocks_are_not_commands(tmp_path):
    readme = "# Shop\n\n```bash\nmake composer c='x'   # run composer inside the container\n```\n"
    context = _context(tmp_path, {"README.md": readme, "Makefile": "composer:\n\ttrue\n", "composer.json": COMPOSER})
    findings, _ = documentation_checks.run({}, context)
    assert "documentation-stale-command" not in {f["rule"] for f in findings}


def test_a_language_plugin_covers_frameworks_of_its_language(tmp_path):
    from agent import default_registry

    composer = json.dumps({"require": {"php": ">=8.2", "symfony/framework-bundle": "7.1.*"}})
    lock = json.dumps({"packages-dev": [{"name": "phpunit/phpunit", "version": "11.0.0"}]})
    from agent.core.facts import PHP_FACTS_SCHEMA, PhpFacts

    _context(tmp_path, {"composer.json": composer, "composer.lock": lock})
    with_facts = RevisionContext.from_directory(tmp_path, facts_loader=lambda _l: PhpFacts({"schema": PHP_FACTS_SCHEMA, "files": []}))
    tests = default_registry().collect(Capability.TEST_CONVENTIONS, with_facts)
    assert "php" in tests.providers and not any(gap["framework"] == "symfony" for gap in tests.gaps)
    without_facts = default_registry().collect(Capability.TEST_CONVENTIONS, RevisionContext.from_directory(tmp_path, facts_loader=lambda _l: None))
    assert {gap["framework"] for gap in without_facts.gaps} == {"php", "symfony"}  # nothing provided, so the framework gap stays


# ── task 30: general configuration-rot checks ────────────────────────────────

def test_docs_naming_a_bare_file_that_does_not_exist(tmp_path):
    readme = "# Shop\n\nRead `CONTEXT.md` first, then `composer.json` and `Kernel.php`.\n"
    context = _context(tmp_path, {"README.md": readme, "composer.json": COMPOSER, "src/Kernel.php": "<?php\n"})
    findings, _ = documentation_checks.run({}, context)
    stale = [f for f in findings if f["rule"] == "documentation-missing-path"]
    assert [(f["line"], "CONTEXT.md" in f["message"]) for f in stale] == [(3, True)]  # Kernel.php exists under src/


def test_scripts_that_ignore_check_failures_and_tools_that_are_not_installed(tmp_path):
    lock = json.dumps({"packages": [], "packages-dev": [{"name": "phpunit/phpunit", "bin": ["phpunit"]},
                                                         {"name": "phpstan/phpstan", "bin": ["phpstan", "phpstan.phar"]}]})
    context = _context(tmp_path, {
        "composer.json": COMPOSER, "composer.lock": lock, ".gitlab-ci.yml": "test:\n  script: [vendor/bin/phpunit]\n",
        "code-analyze.sh": "#!/bin/sh\n# vendor/bin/phpstan analyse || true\nvendor/bin/phpstan analyse src || true\n",
        "Makefile": "test:\n\tvendor/bin/paratest\n",
    })
    rows = _rules(process_checks.run({}, context)[0])
    assert ("process-ignored-check-failure", "code-analyze.sh", 3, "warning") in rows
    assert ("process-missing-tool", "Makefile", 2, "warning") in rows
    assert not any(rule == "process-missing-tool" and file != "Makefile" for rule, file, _, _ in rows)


def test_phpunit_config_paths_that_do_not_exist(tmp_path):
    from agent.plugins.languages.php.testing import provide_test_config_risks

    config = ('<phpunit>\n  <testsuites><testsuite name="u"><directory>./tests/Unit/</directory></testsuite></testsuites>\n'
              '  <source><exclude>\n    <directory>./src/Migrations</directory>\n    <file>./src/Kernel.php</file>\n'
              '  </exclude></source>\n</phpunit>\n')
    context = _context(tmp_path, {"phpunit.xml.dist": config, "tests/Unit/ATest.php": "<?php\n", "src/Kernel.php": "<?php\n"})
    risks = provide_test_config_risks(context)
    assert [(r.rule, r.line, r.details["path"]) for r in risks] == [("phpunit-config-missing-path", 4, "src/Migrations")]


def test_a_fixed_document_language_in_a_multi_locale_project(tmp_path):
    context = _context(tmp_path, {"templates/invoice.twig": '<html lang="en">\n<body></body>\n</html>\n'})
    templates = [TemplateRecord("fixture", "twig", "templates/invoice.twig")]
    catalogues = [TranslationRecord("fixture", "messages", locale, f"translations/messages.{locale}.yml", ()) for locale in ("en", "nl")]
    findings, _ = accessibility_checks.run({Capability.TEMPLATES: _collection(Capability.TEMPLATES, templates),
                                            Capability.TRANSLATIONS: _collection(Capability.TRANSLATIONS, catalogues)}, context)
    assert ("accessibility-fixed-lang", "templates/invoice.twig", 1, "warning") in _rules(findings)
    single, _ = accessibility_checks.run({Capability.TEMPLATES: _collection(Capability.TEMPLATES, templates),
                                          Capability.TRANSLATIONS: _collection(Capability.TRANSLATIONS, catalogues[:1])}, context)
    assert "accessibility-fixed-lang" not in {f["rule"] for f in single}


def test_docs_citing_a_path_tail_or_a_doc_in_another_folder_are_not_stale(tmp_path):
    readme = "# App\n\nSee `Admin/Settings.vue` and `33-order.md`.\n"
    context = _context(tmp_path, {"README.md": readme, "resources/js/pages/Admin/Settings.vue": "<template/>",
                                  "specs/33-order.md": "# Order\n", "composer.json": COMPOSER})
    findings, _ = documentation_checks.run({}, context)
    assert "documentation-missing-path" not in {f["rule"] for f in findings}


# ── Pest tests and Vue single-file components ───────────────────────────────

PEST_CART = """<?php
use App\\Models\\Cart;
use App\\Http\\Controllers\\CartController as Carts;

it('adds a pizza to the cart', function () {
    $this->postJson('/api/cart/items', [])->assertOk();
});

test("removes an item", function () {
    $this->actingAs(1)->deleteJson('/api/cart/items/{id}');
    expect(Carts::class)->not->toBeNull();
});
"""


def test_pest_files_count_as_tests_with_names_routes_and_classes(tmp_path):
    from agent.plugins.languages.php.testing import provide_pest_tests

    context = _context(tmp_path, {"tests/Feature/Cart/CartTest.php": PEST_CART, "tests/Pest.php": "<?php uses(1);",
                                  "tests/TestCase.php": "<?php", "tests/Feature/Plain.php": "<?php echo 1;"})
    records = provide_pest_tests(context, set())
    assert [(r.framework, r.file, r.methods) for r in records] == [
        ("pest", "tests/Feature/Cart/CartTest.php", ("adds a pizza to the cart", "removes an item"))]
    assert {"/api/cart/items", "/api/cart/items/{id}", "App.Http.Controllers.CartController".replace(".", "\\")} <= set(records[0].references)
    assert provide_pest_tests(context, {"tests/Feature/Cart/CartTest.php"}) == []  # a file PHPUnit already listed


def test_testing_inventory_names_pest_files_and_largest_areas(tmp_path):
    rows, summary = testing_checks.run(
        {Capability.TEST_CONVENTIONS: _collection(Capability.TEST_CONVENTIONS, [
            TestRecord("pest", "tests/Feature/Cart/CartTest.php", "tests/Feature/Cart/CartTest.php", ("adds", "removes"))]),
         Capability.ROUTE_ANALYSIS: _collection(Capability.ROUTE_ANALYSIS)}, _context(tmp_path, {"phpunit.xml.dist": "<phpunit/>"}))
    messages = " ".join(row["message"] for row in rows)
    assert "Pest file" in messages and "CartTest.php (2: adds; removes)" in messages and summary["test_methods"] == 2


def test_vue_templates_are_checked_for_accessibility(tmp_path):
    vue = ('<script setup>const a = 1</script>\n<template>\n  <div><img src="a.png"><img :alt="t" src="b.png">'
           '<img v-bind="p"></div>\n</template>\n<style>.x{color:#777;background:#888}</style>')
    context = _context(tmp_path, {"resources/js/Card.vue": vue, "resources/js/Script.vue": "<script>export default {}</script>"})
    rows, summary = accessibility_checks.run({Capability.TEMPLATES: _collection(Capability.TEMPLATES)}, context)
    found = {(r["rule"], r["file"], r["line"]) for r in rows}
    assert ("accessibility-image-without-alt", "resources/js/Card.vue", 3) in found
    assert summary["templates_checked"] == 1 and summary["engines"] == {"vue": 1}
    assert not any(r["rule"] == "accessibility-low-contrast" for r in rows)  # styles are outside the markup
