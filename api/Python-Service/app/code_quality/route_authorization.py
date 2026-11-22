"""Routes that no authorization rule protects: a framework-neutral check.

Reads only normalized records (decision: framework-knowledge-lives-in-plugins-over-language-facts):
``RouteRecord`` (capability ``route_analysis``), ``GuardRecord`` and ``AccessRuleRecord``
(capability ``route_authorization``) from whichever framework plugins the revision uses.

A route is **protected** by a non-public guard on its handler (or the handler's class,
``Class::*``) or on the route itself (``/path`` or ``METHOD /path``), or by the deciding access rule with requirements;
**public** when every guard explicitly allows anonymous access, or the deciding rule is
public and nothing protects it, or when nothing protects it and the project declared it public
(``code_quality/public_routes.py``); otherwise **unprotected**, which becomes a Security finding
at the handler. Access rules are evaluated layer by layer, first non-conditional match per
layer, against the route path with placeholders filled in. A finding means "no declared
rule", not "exploitable": checks done inside services, listeners or bundles are not seen.
"""

from __future__ import annotations

import re
from typing import Any

from agent.core.capability import Capability
from agent.core.records import AccessRuleRecord, GuardRecord, RouteRecord
from code_quality.public_routes import matching_pattern

NAME = "route-authorization"
VERSION = "route-authorization-2"
RULE_ID = "route-without-authorization"
MAX_FINDINGS = 150
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_PLACEHOLDER = re.compile(r"\{[^}/]*\}")


def _sample(template: str) -> str:
    return _PLACEHOLDER.sub("1", template)


def _matches(pattern: str, path: str) -> bool | None:
    if not pattern:
        return True
    try:
        return re.search(pattern, path) is not None
    except re.error:
        return None


def _method_applies(rule_methods: tuple[str, ...], method: str) -> bool | None:
    if not rule_methods:
        return True
    if method == "ANY":
        return None  # a rule limited to some methods neither covers nor skips an any-method route
    return method in rule_methods


def _deciding_rules(rules: list[AccessRuleRecord], path: str, method: str) -> list[AccessRuleRecord]:
    """The first matching rule of each layer, stopping at the first one that decides."""
    chosen = []
    for layer in sorted({rule.layer for rule in rules}):
        match = next((rule for rule in sorted((r for r in rules if r.layer == layer), key=lambda r: r.order)
                      if not rule.conditional and _matches(rule.path_pattern, path) is True
                      and _method_applies(rule.methods, method) is True), None)
        if match is None:
            continue
        chosen.append(match)
        if not match.passthrough:
            break
    return chosen


def decide(route: RouteRecord, method: str, guards: list[GuardRecord], rules: list[AccessRuleRecord],
           declared_public: list[str] | None = None) -> dict[str, Any]:
    owner = (route.handler or "").partition("::")[0]
    own = [g for g in guards if g.handler and (g.handler == route.handler or g.handler == f"{owner}::*")]
    own += [g for g in guards if g.route and g.route in (route.path, f"{method} {route.path}")]
    mechanisms = sorted({g.kind for g in own if not g.public})
    public_reason = None
    if own and all(g.public for g in own):
        public_reason = f"{own[0].kind} {own[0].requirement} allows anonymous access"
    requirements: list[str] = []
    for rule in _deciding_rules(rules, _sample(route.path), method):
        if rule.public:
            if not mechanisms:
                public_reason = f"{rule.label or 'access rule'} allows anonymous access"
        elif rule.requirements:
            mechanisms.append("access-rule")
            requirements = list(rule.requirements)
    declared = None
    if not mechanisms and not public_reason and declared_public:
        declared = matching_pattern(method, route.path, declared_public)
        if declared:
            public_reason = f"declared public in project settings ('{declared}')"
    status = "protected" if mechanisms else ("public" if public_reason else "unprotected")
    return {"declared_public": declared, "method": method, "path": route.path, "handler": route.handler, "framework": route.framework,
            "file": route.file, "line": route.line, "status": status, "mechanisms": mechanisms,
            "requirements": requirements, "public_reason": public_reason}


def _group(path: str) -> str:
    first = path.strip("/").split("/", 1)[0]
    return "/" + first if first else "/"


def evaluate(collections: dict[Capability, Any], declared_public: list[str] | None = None) -> list[dict[str, Any]]:
    """One decision per route and method."""
    routes = [r for r in collections[Capability.ROUTE_ANALYSIS].records if isinstance(r, RouteRecord)]
    authorization = collections[Capability.ROUTE_AUTHORIZATION].records
    guards = [r for r in authorization if isinstance(r, GuardRecord)]
    rules = [r for r in authorization if isinstance(r, AccessRuleRecord)]
    return [decide(route, method, guards, rules, declared_public) for route in routes for method in route.methods]


def run(collections: dict[Capability, Any], settings: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    declared_public = list((settings or {}).get("public_routes") or [])
    rows = evaluate(collections, declared_public)
    findings = []
    for row in rows:
        if row["status"] != "unprotected":
            continue
        owner, _, action = (row["handler"] or "").partition("::")
        handler = f"{owner.rsplit(chr(92), 1)[-1]}::{action}" if action else (owner or "unknown handler")
        findings.append({
            "file": row["file"], "line": row["line"], "rule": RULE_ID, "category": "security",
            "severity": "warning" if row["method"] in _SAFE_METHODS else "error",
            "message": (f"{row['method']} {row['path']} ({handler}) has no authorization guard on its handler "
                        "and no access rule that requires a role."),
        })

    def counts(selected: list[dict[str, Any]]) -> dict[str, int]:
        return {"total": len(selected),
                "protected": sum(r["status"] == "protected" for r in selected),
                "public": sum(r["status"] == "public" for r in selected),
                "unprotected": sum(r["status"] == "unprotected" for r in selected),
                "access_rule_only": sum(r["mechanisms"] == ["access-rule"] for r in selected)}

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(_group(row["path"]), []).append(row)
    summary = {**counts(rows), "groups": {name: counts(items) for name, items in sorted(groups.items())},
               "frameworks": sorted({r["framework"] for r in rows})}
    if declared_public:
        used = {r["declared_public"] for r in rows if r["declared_public"]}
        summary["declared_public"] = sum(1 for r in rows if r["declared_public"])
        summary["public_route_patterns"] = declared_public
        summary["unmatched_public_route_patterns"] = [p for p in declared_public if p not in used]
    if len(findings) > MAX_FINDINGS:
        summary["findings_truncated"] = len(findings) - MAX_FINDINGS
    return findings[:MAX_FINDINGS], summary


def check():
    from code_quality.framework_evidence import FrameworkCheck

    return FrameworkCheck(NAME, VERSION, (Capability.ROUTE_ANALYSIS, Capability.ROUTE_AUTHORIZATION), run,
                          accepts_settings=True)
