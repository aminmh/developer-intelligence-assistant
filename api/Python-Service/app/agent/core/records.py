"""Normalized, framework-neutral records that framework plugins provide.

Decision: @.archcore/code-quality/framework-knowledge-lives-in-plugins-over-language-facts.adr.md.
A framework plugin turns language facts and configuration into these records
(one record type per capability); Code Quality checks read only these records
and never name a framework. Paths are relative to the revision root; lines are 1-based.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class RouteRecord:
    """Capability ``route_analysis``: one HTTP route and the code that handles it."""

    framework: str
    methods: tuple[str, ...]          # ("GET",); ("ANY",) when unrestricted
    path: str                          # template, e.g. "/api/users/{id}"
    handler: str | None                # "App\\Controller\\UserController::show" when known
    file: str
    line: int
    name: str | None = None
    middleware: tuple[str, ...] = ()   # framework middleware names applied to the route


@dataclass(frozen=True)
class GuardRecord:
    """Capability ``route_authorization``: an authorization check on a route handler."""

    framework: str
    handler: str | None                # handler symbol, or None for a route-level guard
    route: str | None                  # "/path" or "METHOD /path" when the guard is attached to a route, not a handler
    kind: str                          # "attribute" | "annotation" | "call" | "middleware"
    requirement: str                   # role, permission, ability or expression as written
    public: bool                       # True when the guard explicitly allows anonymous access
    file: str
    line: int


@dataclass(frozen=True)
class AccessRuleRecord:
    """Capability ``route_authorization``: an ordered path-based access rule.

    Rules are evaluated layer by layer (lowest ``layer`` first); within a layer the
    first matching rule wins. A matching rule with requirements protects the route,
    a ``public`` one makes it explicitly public, and one with neither leaves it
    unprotected, unless it is a ``passthrough`` rule, which hands the decision to
    the next layer (for example a firewall that only enables authentication).
    """

    framework: str
    order: int
    path_pattern: str                  # regular expression as configured; "" matches every path
    methods: tuple[str, ...]           # empty means any method
    requirements: tuple[str, ...]      # roles or abilities; empty means none required
    public: bool
    conditional: bool                  # depends on host, IP, port or other request properties
    file: str
    line: int
    layer: int = 0
    passthrough: bool = False
    label: str = ""                    # how the report names the rule, e.g. "access_control rule 2"


@dataclass(frozen=True)
class TranslationRecord:
    """Capability ``translations``: one catalog of one locale."""

    framework: str
    domain: str
    locale: str
    file: str
    messages: tuple[tuple[str, str], ...]   # (key, message)


@dataclass(frozen=True)
class TemplateRecord:
    """Capability ``templates``: one view or e-mail template."""

    framework: str
    engine: str                        # "twig" | "blade" | "html" | ...
    file: str
    kind: str = "view"                 # "view" | "email" | "pdf"


@dataclass(frozen=True)
class TestRecord:
    """Capability ``test_conventions``: one test class and what it exercises."""

    framework: str
    file: str
    test_class: str
    methods: tuple[str, ...]
    references: tuple[str, ...] = ()   # symbols or route paths the tests call
    notes: tuple[str, ...] = ()        # e.g. "disables tenant filter"


@dataclass(frozen=True)
class ConfigRiskRecord:
    """Capability ``config_risks``: a risky framework setting."""

    framework: str
    rule: str
    category: str                      # assessment category, e.g. "security", "performance"
    severity: str                      # "error" | "warning" | "info"
    message: str
    file: str
    line: int
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaintStubRecord:
    """Capability ``taint_model``: a framework's taint sources and sinks for a taint analyzer.

    ``content`` is analyzer input (for ``psalm``, a ``.phpstub`` of declarations with
    ``@psalm-taint-source`` / ``@psalm-taint-sink`` annotations); it is parsed, never executed.
    """

    framework: str
    analyzer: str                      # "psalm"
    name: str                          # file name, e.g. "laravel.phpstub"
    content: str


@dataclass(frozen=True)
class RunHintRecord:
    """Capability ``run_hints``: one thing a developer needs to start the application.

    Hints are shown to the developer as a checklist; the app never runs a ``command``.
    """

    framework: str
    kind: str                          # "env" | "service" | "command" | "port" | "note"
    name: str                          # variable, service or step name
    detail: str                        # what to do or why it is needed
    file: str = ""                     # where the hint comes from, relative to the revision root
    line: int | None = None


@dataclass(frozen=True)
class ParamHintRecord:
    """Capability ``probe_hints``: a file that tells where request parameter values come from.

    The regression check reads a bounded excerpt of ``file`` and lets the model propose route
    parameters, query strings and bodies from it; the record itself carries no values.
    """

    framework: str
    kind: str                          # "seeder" | "factory" | "fixture" | "validation" | "binding"
    name: str                          # class or fixture name
    file: str                          # relative to the revision root
    line: int | None = None
    handler: str | None = None         # handler symbol the hint applies to, when known


@dataclass(frozen=True)
class TestTemplateRecord:
    """Capability ``characterization_tests``: how this project's test runner expresses a recorded HTTP exchange.

    Templates are plain text with placeholders the regression check fills; nothing here is executed.
    ``file`` and ``file_template`` take ``{name}`` and ``{tests}``; ``route_test`` takes ``{test_name}``,
    ``{title}``, ``{note}``, ``{method}``, ``{path}``, ``{body}``, ``{status}`` and ``{assertions}``;
    ``json_assertion`` takes ``{dot_path}``, ``{bracket_path}`` and ``{value}``; ``text_assertion`` takes
    ``{text}``. String placeholders arrive as single-quoted literals with backslash escapes.
    """

    framework: str
    runner: str                        # "phpunit" | "pest" | "pytest" | ...
    language: str                      # language of the generated file, for prompts and display
    file: str                          # path of the generated file, with {name}
    file_template: str
    route_test: str
    json_assertion: str
    text_assertion: str
    json_value: str                    # wraps a JSON literal into a value, with {json}
    no_body: str                       # value for a request without a body
    true_literal: str = "true"
    false_literal: str = "false"
    null_literal: str = "null"
    uses_dot_path: bool = True         # dot paths cannot address keys that contain dots
    run_command: str = ""
    setup_note: str = ""               # how to point the tests at the data the baseline was recorded on


@dataclass(frozen=True)
class PackageRecord:
    """Capability ``installed_packages``: one package a lockfile pins, as the project runs it.

    ``version`` is normalized (``7.4.18``); ``tag`` is the version as the lockfile writes it (``v7.4.18``);
    ``reference`` is the source commit when the lockfile records one. ``path`` is where the installed copy
    lives inside the revision (``vendor/symfony/http-foundation``), whether or not the upload contains it.
    """

    ecosystem: str                     # "composer" | "npm" | "pypi" | ...
    name: str
    version: str
    tag: str
    lockfile: str
    path: str
    repository: str | None = None      # "owner/repo" on GitHub when known
    reference: str | None = None       # source commit
    dev: bool = False


def record_dict(record: Any) -> dict[str, Any]:
    return asdict(record)
