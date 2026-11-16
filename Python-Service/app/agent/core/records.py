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
    route: str | None                  # route path when the guard is attached to a route, not a handler
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


def record_dict(record: Any) -> dict[str, Any]:
    return asdict(record)
