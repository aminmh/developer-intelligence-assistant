"""Symfony configuration risks (capability ``config_risks``; plan task 11).

Rules come from Symfony's own production guidance and read only committed configuration
(``config/bundles.php`` through PHP facts, ``config/packages`` YAML), never ``.env``:

* ``symfony-profiler-outside-dev`` — the web profiler bundle is enabled for ``all`` or ``prod``
  (Profiler docs: never enable the profiler in production).
* ``symfony-secret-committed`` — ``framework.secret`` is a literal instead of ``%env(...)%``
  (Deployment docs: the production secret is random and never committed). Never reported.
* ``symfony-weak-password-hasher`` — a password hasher uses ``plaintext`` or a bare message
  digest (``md5``, ``sha1``, ``sha256``, ``sha512``) outside tests (Password hashing docs: use ``auto``).

Settings under ``when@test`` or ``config/packages/test/`` are left out. Source text is read
only to point each finding at its line.
"""

from __future__ import annotations

from typing import Any, Iterator

from agent.core.facts import PhpFacts, literal_items, literal_str
from agent.core.records import ConfigRiskRecord
from agent.core.revision import RevisionContext
from agent.plugins.frameworks.symfony.routes import FRAMEWORK, load_yaml

BUNDLES = "config/bundles.php"
PROFILER_BUNDLE = "Symfony\\Bundle\\WebProfilerBundle\\WebProfilerBundle"
WEAK_HASHERS = frozenset({"plaintext", "md5", "sha1", "sha256", "sha512"})
TEST_ENVIRONMENTS = frozenset({"test"})


def _line(text: str | None, needle: str) -> int:
    for index, row in enumerate((text or "").splitlines(), 1):
        if needle in row:
            return index
    return 1


def _risk(rule: str, severity: str, message: str, file: str, line: int, **details: Any) -> ConfigRiskRecord:
    return ConfigRiskRecord(framework=FRAMEWORK, rule=rule, category="security", severity=severity, message=message,
                            file=file, line=line, details=details)


def _profiler(context: RevisionContext, facts: PhpFacts) -> list[ConfigRiskRecord]:
    for key, value in literal_items((facts.file(BUNDLES) or {}).get("returns")):
        if (literal_str(key) or "").lstrip("\\") != PROFILER_BUNDLE:
            continue
        environments = {literal_str(k): v for k, v in literal_items(value)}
        enabled = sorted(name for name in ("all", "prod") if environments.get(name) is True)
        if enabled:
            return [_risk("symfony-profiler-outside-dev", "error",
                          f"The web profiler bundle is enabled for {' and '.join(enabled)}; in production it exposes "
                          "requests, configuration and environment values. Enable it for dev and test only.",
                          BUNDLES, _line(context.read_text(BUNDLES), "WebProfilerBundle"))]
    return []


def _sections(context: RevisionContext) -> Iterator[tuple[str, str | None, dict[str, Any], str | None]]:
    """``(file, environment or None, configuration, text)`` for every config/packages YAML document and when@ block."""
    for path in sorted(f for f in context.files if f.startswith("config/packages/") and f.endswith((".yaml", ".yml"))):
        text = context.read_text(path, 512_000)
        data = load_yaml(text)
        if not isinstance(data, dict):
            continue
        parts = path.split("/")
        folder_env = parts[2] if len(parts) == 4 else None    # config/packages/<env>/x.yaml (older layout)
        yield path, folder_env, data, text
        for key, block in data.items():
            if str(key).startswith("when@") and isinstance(block, dict):
                yield path, str(key)[5:], block, text


def _secret_and_hashers(context: RevisionContext) -> list[ConfigRiskRecord]:
    risks = []
    for path, environment, config, text in _sections(context):
        if environment in TEST_ENVIRONMENTS:
            continue
        framework = config.get("framework")
        secret = framework.get("secret") if isinstance(framework, dict) else None
        if isinstance(secret, str) and secret.strip() and not secret.strip().startswith("%env("):
            risks.append(_risk("symfony-secret-committed", "error",
                               "framework.secret is committed as a literal; it signs URIs and remember-me cookies. "
                               "Read it from %env(APP_SECRET)% and rotate the committed value.",
                               path, _line(text, "secret:"), environment=environment))
        security = config.get("security")
        hashers = {}
        if isinstance(security, dict):
            for section in ("password_hashers", "encoders"):
                if isinstance(security.get(section), dict):
                    hashers.update(security[section])
        for user_class, hasher in hashers.items():
            algorithm = hasher if isinstance(hasher, str) else (hasher.get("algorithm") if isinstance(hasher, dict) else None)
            if isinstance(algorithm, str) and algorithm.lower() in WEAK_HASHERS:
                risks.append(_risk("symfony-weak-password-hasher", "error",
                                   f"Passwords for {str(user_class).rsplit(chr(92), 1)[-1]} are hashed with "
                                   f"'{algorithm}', which is fast to brute-force or not hashed at all. Use 'auto'.",
                                   path, _line(text, f"algorithm: {algorithm}") if f"algorithm: {algorithm}" in (text or "")
                                   else _line(text, f": {algorithm}"),
                                   environment=environment, algorithm=algorithm))
    return risks


def provide_config_risks(context: RevisionContext, facts: PhpFacts) -> list[ConfigRiskRecord]:
    return [*_profiler(context, facts), *_secret_and_hashers(context)]
