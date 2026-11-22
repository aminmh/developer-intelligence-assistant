"""Laravel authorization guards (capability ``route_authorization``).

Decision: framework-knowledge-lives-in-plugins-over-language-facts. Everything is read
from PHP facts; no PHP is parsed here.

Guards:

* **Route middleware** (``kind="middleware"``, keyed ``"METHOD /path"``): a middleware
  name resolves through project aliases (``bootstrap/app.php`` ``$middleware->alias()``
  or ``app/Http/Kernel.php`` ``$middlewareAliases`` / ``$routeMiddleware``), Laravel's
  default aliases and middleware groups to a class. A class protects when it is (or
  extends) a known authentication or authorization middleware (``auth``, ``can``,
  ``auth.basic``, ``verified``, ``signed``, ``password.confirm``, Sanctum abilities,
  spatie/laravel-permission), or when its ``handle()`` denies with 401/403
  (``abort(403)``, ``response()->json(..., 401)``, an authorization exception) or
  calls the ``Gate``. ``guest`` explicitly allows anonymous access.
* **Controller middleware** (``kind="middleware"``, on the handler): ``$this->middleware(...)``
  in a constructor (with ``->only()`` / ``->except()``) and ``new Middleware(...)`` in a
  ``HasMiddleware::middleware()`` method.
* **Calls in the action** (``kind="call"``): ``$this->authorize*()``, ``Gate::authorize()``,
  ``Gate::allows/denies/check()`` paired with a denial, ``abort(401|403)`` or
  ``abort_if/abort_unless(..., 401|403)``, and ``$this->authorizeResource()`` in a
  constructor for the resource actions.

Laravel has no path-pattern access rules, so no ``AccessRuleRecord`` is produced.
Not seen: Form Request ``authorize()`` methods, policies applied only through model
binding, and middleware returned as plain strings from ``HasMiddleware::middleware()``.
"""

from __future__ import annotations

from typing import Any, Iterable

from agent.core.facts import PhpFacts, argument, literal_items, literal_str, literal_strings
from agent.core.records import GuardRecord, RouteRecord
from agent.plugins.frameworks.laravel.routes_from_facts import FRAMEWORK

DEFAULT_ALIASES = {
    "auth": "Illuminate\\Auth\\Middleware\\Authenticate",
    "auth.basic": "Illuminate\\Auth\\Middleware\\AuthenticateWithBasicAuth",
    "auth.session": "Illuminate\\Session\\Middleware\\AuthenticateSession",
    "can": "Illuminate\\Auth\\Middleware\\Authorize",
    "guest": "Illuminate\\Auth\\Middleware\\RedirectIfAuthenticated",
    "password.confirm": "Illuminate\\Auth\\Middleware\\RequirePassword",
    "signed": "Illuminate\\Routing\\Middleware\\ValidateSignature",
    "verified": "Illuminate\\Auth\\Middleware\\EnsureEmailIsVerified",
}
PROTECTING_CLASSES = frozenset({
    "Illuminate\\Auth\\Middleware\\Authenticate",
    "Illuminate\\Auth\\Middleware\\AuthenticateWithBasicAuth",
    "Illuminate\\Auth\\Middleware\\Authorize",
    "Illuminate\\Auth\\Middleware\\RequirePassword",
    "Illuminate\\Auth\\Middleware\\EnsureEmailIsVerified",
    "Illuminate\\Routing\\Middleware\\ValidateSignature",
    "Laravel\\Sanctum\\Http\\Middleware\\CheckAbilities",
    "Laravel\\Sanctum\\Http\\Middleware\\CheckForAnyAbility",
    "Spatie\\Permission\\Middleware\\RoleMiddleware",
    "Spatie\\Permission\\Middleware\\PermissionMiddleware",
    "Spatie\\Permission\\Middleware\\RoleOrPermissionMiddleware",
    "Spatie\\Permission\\Middlewares\\RoleMiddleware",
    "Spatie\\Permission\\Middlewares\\PermissionMiddleware",
    "Spatie\\Permission\\Middlewares\\RoleOrPermissionMiddleware",
})
PUBLIC_CLASSES = frozenset({"Illuminate\\Auth\\Middleware\\RedirectIfAuthenticated"})
_DENIAL_STATUSES = {401, 403}
_DENIAL_EXCEPTIONS = ("AuthorizationException", "AuthenticationException", "AccessDeniedHttpException",
                      "UnauthorizedHttpException")
_GATE = {"Illuminate\\Support\\Facades\\Gate", "Gate"}
_RESOURCE_ACTIONS = ("index", "create", "store", "show", "edit", "update", "destroy")
_CONTROLLER_MIDDLEWARE = "Illuminate\\Routing\\Controllers\\Middleware"


def _steps(call: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"name": call.get("name"), "args": call.get("args") or []}, *(call.get("chain") or [])]


def _status(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value in _DENIAL_STATUSES


def _denies(call: dict[str, Any]) -> bool:
    """``abort(403)``, ``abort_if(..., 401)``, ``response()->json(..., 403)`` or ``new AuthorizationException``."""
    if call.get("kind") == "new":
        return str(call.get("class") or "").endswith(_DENIAL_EXCEPTIONS)
    name = call.get("name")
    if call.get("kind") == "function" and name in ("abort", "abort_if", "abort_unless", "response"):
        return any(_status(arg.get("value")) for step in _steps(call) for arg in step["args"])
    return False


def _authorizes(call: dict[str, Any]) -> str | None:
    """The requirement of an authorization call that stops the request by itself, else None."""
    name = call.get("name") or ""
    first = literal_str(argument(call.get("args") or [], 0)) or ""
    if call.get("kind") == "method" and call.get("receiver") == "this" and name in ("authorize", "authorizeForUser"):
        return f"authorize('{first}')" if first else "authorize()"
    if call.get("kind") == "static" and call.get("class") in _GATE and name == "authorize":
        return f"Gate::authorize('{first}')" if first else "Gate::authorize()"
    return None


def _gate_check(call: dict[str, Any]) -> str | None:
    if call.get("kind") == "static" and call.get("class") in _GATE and call.get("name") in ("allows", "denies", "check", "any", "none"):
        return f"Gate::{call['name']}('{literal_str(argument(call.get('args') or [], 0)) or ''}')"
    return None


class MiddlewareResolver:
    """Resolve middleware names to classes and decide whether each protects a route."""

    def __init__(self, facts: PhpFacts) -> None:
        self.facts = facts
        self.aliases = dict(DEFAULT_ALIASES)
        self.groups: dict[str, list[str]] = {"web": [], "api": []}
        self._read_bootstrap()
        self._read_kernel()
        self._verdicts: dict[str, bool | None] = {}

    def _read_bootstrap(self) -> None:
        for call, _file in self.facts.calls("bootstrap/app.php"):
            if call.get("kind") != "method" or call.get("receiver") != "var:middleware":
                continue
            args, name = call.get("args") or [], call.get("name")
            if name == "alias":
                for key, value in literal_items(argument(args, 0, "aliases")):
                    alias, target = literal_str(key), literal_str(value, self.facts)
                    if alias and target:
                        self.aliases[alias] = target.lstrip("\\")
            elif name in ("web", "api"):
                for key in ("append", "prepend"):
                    self.groups[name] += [m.lstrip("\\") for m in literal_strings(argument(args, 99, key), self.facts)]
            elif name in ("group", "appendToGroup", "prependToGroup"):
                group = literal_str(argument(args, 0, "group"))
                if group:
                    self.groups.setdefault(group, []).extend(
                        m.lstrip("\\") for m in literal_strings(argument(args, 1, "middleware"), self.facts))

    def _read_kernel(self) -> None:
        kernel = self.facts.class_("App\\Http\\Kernel")
        for prop in (kernel or {}).get("properties") or []:
            if prop.get("name") in ("$middlewareAliases", "$routeMiddleware"):
                for key, value in literal_items(prop.get("default")):
                    alias, target = literal_str(key), literal_str(value, self.facts)
                    if alias and target:
                        self.aliases.setdefault(alias, target.lstrip("\\"))
            elif prop.get("name") == "$middlewareGroups":
                for key, value in literal_items(prop.get("default")):
                    if literal_str(key):
                        self.groups[literal_str(key)] = [m.lstrip("\\") for m in literal_strings(value, self.facts)]

    def verdict(self, middleware: str, seen: frozenset[str] = frozenset()) -> bool | None:
        """True when it protects, False when it explicitly allows anonymous access, None when neither."""
        base = middleware.split(":", 1)[0].lstrip("\\")
        if base in seen:
            return None
        if base in self.groups:
            verdicts = [self.verdict(member, seen | {base}) for member in self.groups[base]]
            return True if True in verdicts else (False if False in verdicts else None)
        cls = self.aliases.get(base, base)
        if cls not in self._verdicts:
            self._verdicts[cls] = self._class_verdict(cls)
        return self._verdicts[cls]

    def _class_verdict(self, cls: str) -> bool | None:
        lineage = {cls, *self.facts.ancestors(cls)}
        if lineage & PUBLIC_CLASSES:
            return False
        if lineage & PROTECTING_CLASSES:
            return True
        for owner in lineage:
            for call, _file in self._calls_in(f"{owner}::handle"):
                if _denies(call) or _authorizes(call) or _gate_check(call):
                    return True
        return None

    def _calls_in(self, scope: str) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
        file = self.facts.file_of(scope.partition("::")[0])
        if file is None:
            return []
        return [(call, file) for call, file in self.facts.calls(str(file.get("path"))) if call.get("scope") == scope]


def _route_guards(routes: list[RouteRecord], resolver: MiddlewareResolver) -> list[GuardRecord]:
    guards = []
    for route in routes:
        for middleware in route.middleware:
            verdict = resolver.verdict(middleware)
            if verdict is None:
                continue
            for method in route.methods:
                guards.append(GuardRecord(framework=FRAMEWORK, handler=None, route=f"{method} {route.path}",
                                          kind="middleware", requirement=middleware, public=verdict is False,
                                          file=route.file, line=route.line))
    return guards


def _controller_guards(facts: PhpFacts, resolver: MiddlewareResolver) -> list[GuardRecord]:
    guards: list[GuardRecord] = []
    by_scope: dict[str, list[dict[str, Any]]] = {}
    paths: dict[str, str] = {}
    for call, file in facts.calls():
        scope = call.get("scope")
        if scope and "::" in scope:
            by_scope.setdefault(scope, []).append(call)
            paths[scope] = str(file.get("path", ""))

    def actions(owner: str) -> list[str]:
        cls = facts.class_(owner) or {}
        return [m["name"] for m in cls.get("methods") or []
                if m.get("visibility", "public") == "public" and not str(m.get("name", "")).startswith("__")]

    def add(owner: str, names: list[str] | None, kind: str, requirement: str, public: bool, scope: str, line: int) -> None:
        for name in (names if names is not None else ["*"]):
            guards.append(GuardRecord(framework=FRAMEWORK, handler=f"{owner}::{name}", route=None, kind=kind,
                                      requirement=requirement, public=public, file=paths[scope], line=line))

    for scope, calls in sorted(by_scope.items()):
        owner, _, method = scope.partition("::")
        denial = any(_denies(call) for call in calls)
        for call in calls:
            line = int(call.get("line") or 1)
            steps = _steps(call)
            limits = {step["name"]: list(literal_strings(argument(step["args"], 0), facts)) for step in steps[1:]
                      if step.get("name") in ("only", "except")}
            if method == "__construct" and call.get("kind") == "method" and call.get("receiver") == "this":
                if call.get("name") == "middleware":
                    for middleware in literal_strings(argument(call.get("args") or [], 0), facts):
                        verdict = resolver.verdict(middleware)
                        if verdict is None:
                            continue
                        names = limits.get("only")
                        if "except" in limits:
                            names = [a for a in actions(owner) if a not in limits["except"]]
                        add(owner, names, "middleware", middleware, verdict is False, scope, line)
                    continue
                if call.get("name") == "authorizeResource":
                    model = literal_str(argument(call.get("args") or [], 0), facts) or ""
                    names = [a for a in actions(owner) if a in _RESOURCE_ACTIONS]
                    add(owner, names, "call", f"authorizeResource({model.rsplit(chr(92), 1)[-1]})", False, scope, line)
                    continue
            if method == "middleware" and call.get("kind") == "new" and call.get("class") == _CONTROLLER_MIDDLEWARE:
                args = call.get("args") or []
                only = literal_strings(argument(args, 1, "only"), facts) or None
                excluded = literal_strings(argument(args, 2, "except"), facts)
                names = [a for a in actions(owner) if a not in excluded] if excluded else only
                for middleware in literal_strings(argument(args, 0, "middleware"), facts):
                    verdict = resolver.verdict(middleware)
                    if verdict is not None:
                        add(owner, names, "middleware", middleware, verdict is False, scope, line)
                continue
            requirement = _authorizes(call) or (_gate_check(call) if denial else None)
            if requirement is None and _denies(call) and call.get("kind") == "function" and call.get("name") != "response":
                status = next((a.get("value") for a in call.get("args") or [] if _status(a.get("value"))), "")
                requirement = f"{call['name']}({status})"
            if requirement:
                add(owner, [method], "call", requirement, False, scope, line)
    return guards


def provide_guards(facts: PhpFacts, routes: list[RouteRecord]) -> list[GuardRecord]:
    resolver = MiddlewareResolver(facts)
    return [*_route_guards(routes, resolver), *_controller_guards(facts, resolver)]
