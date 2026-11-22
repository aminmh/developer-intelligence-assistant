"""Framework configuration risks from each plugin's production guidance (gap-closing plan, task 11)."""

import json
import shutil
from pathlib import Path

import pytest

from agent import default_registry
from agent.core.capability import Capability
from agent.core.revision import RevisionContext
from code_quality import config_risks


def _php_cli_available() -> bool:
    from infra.settings import settings

    return shutil.which("php") is not None and Path(settings.php_cli_root, "bin", "project-inspect").is_file()


pytestmark = pytest.mark.skipif(not _php_cli_available(), reason="PHP CLI tools are not installed here")


def _findings(tmp_path, files: dict[str, str]) -> list[tuple[str, str, int, str]]:
    root = tmp_path / "rev"
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)
    collections = {Capability.CONFIG_RISKS: default_registry().collect(Capability.CONFIG_RISKS, RevisionContext.from_directory(root))}
    findings, _summary = config_risks.run(collections)
    assert all(".env" not in f["file"] for f in findings)
    return sorted((f["rule"], f["file"], f["line"], f["severity"]) for f in findings)


def _laravel(require=None, require_dev=None, app_config=None, providers=None, **extra):
    return {
        "composer.json": json.dumps({"require": {"laravel/framework": "^12.0", **(require or {})},
                                     "require-dev": require_dev or {}}),
        "config/app.php": app_config or "<?php\n\nreturn [\n    'debug' => (bool) env('APP_DEBUG', false),\n    'key' => env('APP_KEY'),\n];\n",
        "bootstrap/providers.php": providers or "<?php\n\nreturn [\n    App\\Providers\\AppServiceProvider::class,\n];\n",
        "app/Providers/AppServiceProvider.php": "<?php\nnamespace App\\Providers;\nclass AppServiceProvider {}\n",
        **extra,
    }


def test_a_well_configured_laravel_app_has_no_risks(tmp_path):
    assert _findings(tmp_path, _laravel(require_dev={"laravel/telescope": "^5.0"})) == []


def test_laravel_debug_key_and_env_outside_config(tmp_path):
    files = _laravel(
        app_config="<?php\n\nreturn [\n    'name' => 'Shop',\n    'debug' => (bool) env('APP_DEBUG', true),\n"
                   "    'key' => 'base64:c2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0MTI=',\n];\n",
        **{"app/Services/Mailer.php": "<?php\nnamespace App\\Services;\nclass Mailer {\n    public function from() {\n"
                                      "        return env('MAIL_FROM') ?: env('MAIL_FALLBACK');\n    }\n}\n",
           "tests/Feature/MailTest.php": "<?php\nenv('IGNORED_IN_TESTS');\n"})
    assert _findings(tmp_path, files) == [
        ("laravel-app-key-committed", "config/app.php", 6, "error"),
        ("laravel-debug-enabled-by-default", "config/app.php", 5, "warning"),
        ("laravel-env-outside-config", "app/Services/Mailer.php", 5, "warning"),
    ]
    hard_coded = _laravel(app_config="<?php\nreturn [\n    'debug' => true,\n];\n")
    assert _findings(tmp_path / "b", hard_coded) == [("laravel-debug-enabled-by-default", "config/app.php", 3, "error")]


def test_laravel_debug_tools(tmp_path):
    telescope_provider = ("<?php\nnamespace App\\Providers;\nuse Laravel\\Telescope\\TelescopeApplicationServiceProvider;\n"
                          "class TelescopeServiceProvider extends TelescopeApplicationServiceProvider {}\n")
    everywhere = _laravel(require_dev={"laravel/telescope": "^5.0"},
                          providers="<?php\n\nreturn [\n    App\\Providers\\AppServiceProvider::class,\n"
                                    "    App\\Providers\\TelescopeServiceProvider::class,\n];\n",
                          **{"app/Providers/TelescopeServiceProvider.php": telescope_provider})
    assert _findings(tmp_path, everywhere) == [
        ("laravel-dev-provider-registered-everywhere", "bootstrap/providers.php", 5, "warning")]
    production = _laravel(require={"barryvdh/laravel-debugbar": "^3.0"})
    [risk] = _findings(tmp_path / "b", production)
    assert risk[:2] == ("laravel-debug-tool-in-production", "composer.json") and risk[3] == "warning"


SYMFONY_COMPOSER = json.dumps({"require": {"symfony/framework-bundle": "7.1.*"}})
SAFE_BUNDLES = ("<?php\n\nreturn [\n    Symfony\\Bundle\\FrameworkBundle\\FrameworkBundle::class => ['all' => true],\n"
                "    Symfony\\Bundle\\WebProfilerBundle\\WebProfilerBundle::class => ['dev' => true, 'test' => true],\n];\n")


def _symfony(**files):
    return {"composer.json": SYMFONY_COMPOSER, "config/bundles.php": SAFE_BUNDLES,
            "config/packages/framework.yaml": "framework:\n    secret: '%env(APP_SECRET)%'\n",
            "config/packages/security.yaml": "security:\n    password_hashers:\n        App\\Entity\\User:\n            algorithm: auto\n"
                                             "when@test:\n    security:\n        password_hashers:\n            App\\Entity\\User:\n"
                                             "                algorithm: plaintext\n",
            **files}


def test_a_well_configured_symfony_app_has_no_risks_and_test_settings_are_ignored(tmp_path):
    assert _findings(tmp_path, _symfony(**{"config/packages/test/framework.yaml": "framework:\n    secret: test-secret\n"})) == []


def test_symfony_profiler_secret_and_hashers(tmp_path):
    files = _symfony(**{
        "config/bundles.php": SAFE_BUNDLES.replace("['dev' => true, 'test' => true]", "['all' => true]"),
        "config/packages/framework.yaml": "framework:\n    secret: 'ThisTokenIsNotSoSecretChangeIt'\n",
        "config/packages/security.yaml": "security:\n    password_hashers:\n        App\\Entity\\Admin: sha1\n"
                                         "        App\\Entity\\User:\n            algorithm: md5\n",
    })
    assert _findings(tmp_path, files) == [
        ("symfony-profiler-outside-dev", "config/bundles.php", 5, "error"),
        ("symfony-secret-committed", "config/packages/framework.yaml", 2, "error"),
        ("symfony-weak-password-hasher", "config/packages/security.yaml", 3, "error"),
        ("symfony-weak-password-hasher", "config/packages/security.yaml", 5, "error"),
    ]


def test_messages_never_contain_secret_values(tmp_path):
    files = _symfony(**{"config/packages/framework.yaml": "framework:\n    secret: 'ThisTokenIsNotSoSecretChangeIt'\n"})
    root = tmp_path / "rev"
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)
    collections = {Capability.CONFIG_RISKS: default_registry().collect(Capability.CONFIG_RISKS, RevisionContext.from_directory(root))}
    findings, _ = config_risks.run(collections)
    assert findings and all("ThisTokenIsNotSoSecret" not in f["message"] for f in findings)


def test_the_check_never_names_a_framework():
    import inspect

    source = inspect.getsource(config_risks).lower()
    assert "laravel" not in source and "symfony" not in source
