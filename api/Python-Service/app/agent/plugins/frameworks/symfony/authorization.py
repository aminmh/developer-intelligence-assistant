"""Symfony authorization guards and access rules (capability ``route_authorization``).

Guards come from PHP facts: ``#[IsGranted]`` / ``#[Security]`` attributes and
``@IsGranted`` / ``@Security`` annotations on an action or its class (a class
guard covers every action, emitted with handler ``Class::*``), a
``$this->denyAccessUnlessGranted(...)`` call in the action, or an
``isGranted(...)`` check paired with an access-denied exception.

Access rules come from ``config/packages/security.yaml`` as two layers:
firewalls (layer 0; ``security: false`` is public, any other firewall only
enables authentication and passes the decision on) and ``access_control``
(layer 1; first match wins). ``PUBLIC_ACCESS`` and ``IS_AUTHENTICATED_ANONYMOUSLY``
are public; ``allow_if`` and other roles protect. Rules that also depend on host,
IP, port or a request matcher are marked conditional.
"""

from __future__ import annotations

from typing import Any

from agent.core.facts import PhpFacts, argument, literal_str
from agent.core.records import AccessRuleRecord, GuardRecord
from agent.core.revision import RevisionContext
from agent.plugins.frameworks.symfony.routes import FRAMEWORK, load_yaml

PUBLIC_ROLES = frozenset({"PUBLIC_ACCESS", "IS_AUTHENTICATED_ANONYMOUSLY"})
SECURITY_FILES = ("config/packages/security.yaml", "config/packages/security.yml")
_GUARD_ATTRIBUTES = {
    "Symfony\\Component\\Security\\Http\\Attribute\\IsGranted": "IsGranted",
    "Symfony\\Component\\Security\\Http\\Attribute\\Security": "Security",
    "Sensio\\Bundle\\FrameworkExtraBundle\\Configuration\\IsGranted": "IsGranted",
    "Sensio\\Bundle\\FrameworkExtraBundle\\Configuration\\Security": "Security",
}
_CONDITIONAL_KEYS = {"host", "ips", "ip", "port", "attributes", "route", "request_matcher", "requires_channel"}
_DENIED = ("createAccessDeniedException",)


def _attribute_guards(node: dict[str, Any], facts: PhpFacts) -> list[tuple[str, int, str]]:
    """``(requirement, line, kind)`` for each guard attribute or annotation on a class or method."""
    found = []
    for attribute in node.get("attributes") or []:
        if attribute.get("name") in _GUARD_ATTRIBUTES:
            args = attribute.get("args") or []
            value = argument(args, 0, "attribute") or argument(args, 0, "expression")
            found.append((literal_str(value, facts) or "", int(attribute.get("line") or node["line"]), "attribute"))
    for annotation in node.get("annotations") or []:
        name = str(annotation.get("name", "")).rsplit("\\", 1)[-1]
        if name in ("IsGranted", "Security"):
            raw = (annotation.get("args") or "").strip()
            requirement = raw.split('"')[1] if raw.count('"') >= 2 else raw
            found.append((requirement, int(annotation.get("line") or node["line"]), "annotation"))
    return found


def provide_guards(facts: PhpFacts) -> list[GuardRecord]:
    guards: list[GuardRecord] = []
    for cls, file in facts.classes():
        path = str(file.get("path", ""))
        for requirement, line, kind in _attribute_guards(cls, facts):
            guards.append(GuardRecord(framework=FRAMEWORK, handler=f"{cls['fqn']}::*", route=None, kind=kind,
                                      requirement=requirement, public=requirement in PUBLIC_ROLES,
                                      file=path, line=line))
        for method in cls.get("methods") or []:
            for requirement, line, kind in _attribute_guards(method, facts):
                guards.append(GuardRecord(framework=FRAMEWORK, handler=f"{cls['fqn']}::{method['name']}", route=None,
                                          kind=kind, requirement=requirement, public=requirement in PUBLIC_ROLES,
                                          file=path, line=line))
    # Calls inside actions: denyAccessUnlessGranted, or isGranted with an access-denied exception.
    by_scope: dict[str, list[dict[str, Any]]] = {}
    paths: dict[str, str] = {}
    for call, file in facts.calls():
        scope = call.get("scope")
        if scope:
            by_scope.setdefault(scope, []).append(call)
            paths[scope] = str(file.get("path", ""))
    for scope, calls in sorted(by_scope.items()):
        denied = any(c.get("name") in _DENIED or str(c.get("class") or "").endswith("AccessDeniedException")
                     for c in calls)
        for call in calls:
            name = call.get("name")
            if call.get("kind") != "method" or call.get("receiver") != "this":
                continue
            if name == "denyAccessUnlessGranted" or (name == "isGranted" and denied):
                requirement = literal_str(argument(call.get("args") or [], 0), facts) or ""
                guards.append(GuardRecord(framework=FRAMEWORK, handler=scope, route=None, kind="call",
                                          requirement=requirement, public=requirement in PUBLIC_ROLES,
                                          file=paths[scope], line=int(call.get("line") or 1)))
    return guards


def _roles(rule: dict[str, Any]) -> tuple[str, ...]:
    roles = rule.get("roles") or rule.get("role") or []
    if isinstance(roles, str):
        roles = [part.strip() for part in roles.split(",")]
    return tuple(str(role) for role in roles if str(role).strip())


def _methods(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(str(item).upper() for item in ([value] if isinstance(value, str) else value))


def security_file(context: RevisionContext) -> str | None:
    return next((name for name in SECURITY_FILES if context.exists(name)), None)


def provide_access_rules(context: RevisionContext) -> list[AccessRuleRecord]:
    name = security_file(context)
    data = load_yaml(context.read_text(name, 512_000)) if name else None
    security = data.get("security") if isinstance(data, dict) else None
    if not isinstance(security, dict):
        return []
    text = context.read_text(name, 512_000) or ""
    lines = text.splitlines()

    def line_of(needle: str) -> int:
        return next((index + 1 for index, row in enumerate(lines) if needle in row), 1)

    rules: list[AccessRuleRecord] = []
    firewalls = security.get("firewalls") if isinstance(security.get("firewalls"), dict) else {}
    for order, (firewall, entry) in enumerate(firewalls.items()):
        if not isinstance(entry, dict):
            continue
        public = entry.get("security") is False
        rules.append(AccessRuleRecord(
            framework=FRAMEWORK, order=order, path_pattern=str(entry.get("pattern") or ""),
            methods=_methods(entry.get("methods")), requirements=(), public=public,
            conditional=any(key in entry for key in ("host", "request_matcher")), file=name or "",
            line=line_of(f"{firewall}:"), layer=0, passthrough=not public,
            label=f"firewall '{firewall}'" + (" (security: false)" if public else "")))
    for order, rule in enumerate(r for r in security.get("access_control") or [] if isinstance(r, dict)):
        roles = _roles(rule)
        requirements = roles + ((f"allow_if: {rule['allow_if']}",) if rule.get("allow_if") else ())
        public = bool(roles) and set(roles) <= PUBLIC_ROLES and not rule.get("allow_if")
        rules.append(AccessRuleRecord(
            framework=FRAMEWORK, order=order, path_pattern=str(rule.get("path") or ""),
            methods=_methods(rule.get("methods")),
            requirements=tuple(r for r in requirements if r not in PUBLIC_ROLES), public=public,
            conditional=any(key in rule for key in _CONDITIONAL_KEYS), file=name or "",
            line=line_of(str(rule.get("path") or "access_control")), layer=1,
            label=f"access_control rule {order + 1}"))
    return rules
