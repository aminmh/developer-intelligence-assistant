"""Laravel configuration risks (capability ``config_risks``; plan task 11).

Rules come from Laravel's own production guidance and read only committed configuration
(PHP facts and ``composer.json``), never ``.env``:

* ``laravel-debug-enabled-by-default`` — ``config/app.php`` hard-codes ``debug`` on, or
  ``APP_DEBUG`` defaults to true when unset (Configuration docs: debug must be off in production).
* ``laravel-app-key-committed`` — ``config/app.php`` holds a literal application key instead of
  ``env('APP_KEY')`` (the key encrypts sessions and cookies). The value is never reported.
* ``laravel-env-outside-config`` — ``env()`` called outside ``config/``; once configuration is
  cached it no longer reads ``.env`` and returns null (Configuration docs).
* ``laravel-debug-tool-in-production`` — Telescope or Debugbar is a production dependency
  (``require``), so it ships and runs in production.
* ``laravel-dev-provider-registered-everywhere`` — a provider built on a dev-only debug tool is
  registered for every environment: a production install without dev packages cannot boot,
  and with them the tool runs in production (Telescope docs: local-only installation).

Source text is read only to point each finding at its line.
"""

from __future__ import annotations

import json
from typing import Any

from agent.core.facts import PhpFacts, argument, literal_items, literal_str, short_name
from agent.core.records import ConfigRiskRecord
from agent.core.revision import RevisionContext

FRAMEWORK = "laravel"
APP_CONFIG = "config/app.php"
PROVIDER_FILES = ("bootstrap/providers.php", APP_CONFIG)
# Debug tools: package → (namespace prefix, display name).
DEBUG_TOOLS = {
    "laravel/telescope": ("Laravel\\Telescope\\", "Laravel Telescope"),
    "barryvdh/laravel-debugbar": ("Barryvdh\\Debugbar\\", "Laravel Debugbar"),
}
_TEST_DIRS = {"test", "tests"}


def _line(text: str | None, needle: str) -> int:
    for index, row in enumerate((text or "").splitlines(), 1):
        if needle in row:
            return index
    return 1


def _config(facts: PhpFacts, path: str) -> dict[str, Any]:
    file = facts.file(path) or {}
    return {key: value for key, value in ((literal_str(k), v) for k, v in literal_items(file.get("returns"))) if key}


def _env_call(value: Any) -> tuple[str | None, Any] | None:
    call = value.get("call") if isinstance(value, dict) else None
    if not isinstance(call, dict) or str(call.get("name", "")).lstrip("\\") != "env":
        return None
    args = call.get("args") or []
    return literal_str(argument(args, 0)), argument(args, 1)


def _risk(rule: str, category: str, severity: str, message: str, file: str, line: int, **details: Any) -> ConfigRiskRecord:
    return ConfigRiskRecord(framework=FRAMEWORK, rule=rule, category=category, severity=severity, message=message,
                            file=file, line=line, details=details)


def _app_config(context: RevisionContext, facts: PhpFacts) -> list[ConfigRiskRecord]:
    app = _config(facts, APP_CONFIG)
    text = context.read_text(APP_CONFIG) if app else None
    risks = []
    debug = app.get("debug")
    env = _env_call(debug)
    if debug is True:
        risks.append(_risk("laravel-debug-enabled-by-default", "security", "error",
                           "config/app.php hard-codes debug on; production then shows stack traces and configuration "
                           "values to visitors. Read it from APP_DEBUG with a false default.",
                           APP_CONFIG, _line(text, "'debug'")))
    elif env and env[1] is True:
        risks.append(_risk("laravel-debug-enabled-by-default", "security", "warning",
                           f"{env[0] or 'The debug variable'} defaults to true when it is unset, so a production server "
                           "missing it shows stack traces and configuration values. Default it to false.",
                           APP_CONFIG, _line(text, "'debug'")))
    key = app.get("key")
    if isinstance(key, str) and key.strip():
        risks.append(_risk("laravel-app-key-committed", "security", "error",
                           "config/app.php commits the application key, which encrypts sessions and cookies. Read it "
                           "from APP_KEY and rotate the committed key.", APP_CONFIG, _line(text, "'key'")))
    return risks


def _env_outside_config(facts: PhpFacts) -> list[ConfigRiskRecord]:
    first: dict[str, tuple[int, int]] = {}
    for call, file in facts.calls():
        path = str(file.get("path", ""))
        if (call.get("kind") != "function" or str(call.get("name", "")).lstrip("\\") != "env"
                or path.startswith("config/") or _TEST_DIRS & {part.lower() for part in path.split("/")[:-1]}):
            continue
        line, count = first.get(path, (int(call.get("line") or 1), 0))
        first[path] = (min(line, int(call.get("line") or 1)), count + 1)
    return [_risk("laravel-env-outside-config", "reliability", "warning",
                  f"env() is called outside config/ ({count} {'call' if count == 1 else 'calls'} in this file); once "
                  "configuration is cached it no longer reads .env and returns null. Read the value through config().",
                  path, line, calls=count)
            for path, (line, count) in sorted(first.items())]


def _providers(facts: PhpFacts) -> list[tuple[str, str]]:
    """``(provider class, file)`` registered in bootstrap/providers.php or config/app.php ``providers``."""
    found = []
    for path in PROVIDER_FILES:
        file = facts.file(path) or {}
        values = literal_items(file.get("returns")) if path != APP_CONFIG else literal_items(_config(facts, path).get("providers"))
        found += [(name.lstrip("\\"), path) for _k, value in values if (name := literal_str(value))]
    return found


def _debug_tools(context: RevisionContext, facts: PhpFacts) -> list[ConfigRiskRecord]:
    text = context.read_text("composer.json")
    try:
        composer = json.loads(text or "{}")
    except ValueError:
        return []
    require, require_dev = composer.get("require") or {}, composer.get("require-dev") or {}
    risks = []
    providers = _providers(facts)
    for package, (namespace, label) in DEBUG_TOOLS.items():
        if package in require:
            risks.append(_risk("laravel-debug-tool-in-production", "security", "warning",
                               f"{label} ({package}) is a production dependency, so it ships and runs in production. "
                               "Require it with --dev and register it only for the local environment.",
                               "composer.json", _line(text, f'"{package}"'), package=package))
        elif package in require_dev:
            for provider, path in providers:
                lineage = [provider, *facts.ancestors(provider)]
                if any(cls.startswith(namespace) for cls in lineage):
                    provider_text = context.read_text(path)
                    risks.append(_risk(
                        "laravel-dev-provider-registered-everywhere", "reliability", "warning",
                        f"{short_name(provider)} is built on {label}, a dev-only package, but {path} registers it for "
                        "every environment: a production install without dev packages cannot boot, and with them "
                        f"{label} runs in production. Register it only when the environment is local.",
                        path, _line(provider_text, short_name(provider)), package=package, provider=provider))
    return risks


def provide_config_risks(context: RevisionContext, facts: PhpFacts) -> list[ConfigRiskRecord]:
    return [*_app_config(context, facts), *_env_outside_config(facts), *_debug_tools(context, facts)]
