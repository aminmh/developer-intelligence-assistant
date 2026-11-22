"""Laravel routes from PHP facts (capability ``route_analysis``).

Decision: framework-knowledge-lives-in-plugins-over-language-facts. No PHP is parsed
here: the route DSL is read from the facts' recorded calls, where each call carries
its fluent chain and the call whose closure contains it (``parent``).

Route files are found the way Laravel loads them: ``bootstrap/app.php``
``->withRouting(web:, api:, apiPrefix:)`` (Laravel 11+), ``Route::...->group(<file>)``
in ``bootstrap/app.php`` or a service provider (``then:`` closures,
``RouteServiceProvider``), and ``require``/``include`` inside route files. With none
of these, ``routes/web.php`` (``web``) and ``routes/api.php`` (``/api``, ``api``) are
assumed. Group attributes (``prefix``, ``middleware``, ``withoutMiddleware``,
``name``, ``controller``, ``namespace``, ``Route::group([...])``) apply to the
routes inside; verbs, ``match``, ``any``, ``view``, ``redirect``, ``resource`` /
``apiResource`` (nested, ``only`` / ``except``) and ``resources`` define them.
A later definition of the same method and path replaces an earlier one, as in
Laravel. Each route points at its definition in the route file.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, replace
from typing import Any, Iterable

from agent.core.facts import PhpFacts, argument, literal_items, literal_str, literal_strings
from agent.core.records import RouteRecord
from agent.core.revision import RevisionContext

FRAMEWORK = "laravel"
ANY_METHOD = "ANY"
ROUTE_FACADES = frozenset({"Illuminate\\Support\\Facades\\Route", "Route"})
DEFAULT_CONTROLLER_NAMESPACE = "App\\Http\\Controllers"
_VERBS = {"get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH", "delete": "DELETE", "options": "OPTIONS"}
_GET_ONLY = {"view", "redirect", "permanentRedirect"}
_RESOURCE_ACTIONS = (  # (action, methods, suffix); suffix "{p}" is the resource parameter
    ("index", ("GET",), ""), ("create", ("GET",), "/create"), ("store", ("POST",), ""),
    ("show", ("GET",), "/{p}"), ("edit", ("GET",), "/{p}/edit"), ("update", ("PUT", "PATCH"), "/{p}"),
    ("destroy", ("DELETE",), "/{p}"),
)
_API_RESOURCE_ACTIONS = ("index", "store", "show", "update", "destroy")
_PROVIDER_PREFIXES = ("app/Providers/", "bootstrap/")


@dataclass(frozen=True)
class GroupAttributes:
    prefix: str = ""
    middleware: tuple[str, ...] = ()
    without: tuple[str, ...] = ()
    name: str = ""
    controller: str | None = None
    namespace: str | None = None


def join(*parts: str) -> str:
    return "/" + "/".join(part.strip("/") for part in parts if part and part.strip("/"))


def singular(word: str) -> str:
    """Laravel's resource parameter name: ``pizza-sizes`` → ``pizza_size``."""
    word = word.replace("-", "_")
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _steps(call: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"name": call.get("name"), "args": call.get("args") or [], "line": call.get("line")}, *(call.get("chain") or [])]


def _is_route_builder(call: dict[str, Any]) -> bool:
    return call.get("kind") == "static" and call.get("class") in ROUTE_FACADES


def _strings(args: list[dict[str, Any]], facts: PhpFacts) -> tuple[str, ...]:
    return tuple(text.lstrip("\\") for arg in args for text in literal_strings(arg.get("value"), facts))


def file_argument(value: Any, current_file: str) -> str | None:
    """A route-file path argument: ``base_path('routes/x.php')``, ``__DIR__.'/x.php'`` or a literal."""
    if isinstance(value, dict) and isinstance(value.get("call"), dict):
        call = value["call"]
        if call.get("name") in ("base_path", "app_path") and call.get("args"):
            inner = literal_str(call["args"][0].get("value"))
            if inner is not None:
                return posixpath.normpath(("app/" if call["name"] == "app_path" else "") + inner.lstrip("/"))
        return None
    if isinstance(value, dict) and isinstance(value.get("concat"), list):
        parts = value["concat"]
        if parts and isinstance(parts[0], dict) and parts[0].get("magic") == "__DIR__":
            rest = [literal_str(part) for part in parts[1:]]
            if all(part is not None for part in rest):
                return posixpath.normpath(posixpath.join(posixpath.dirname(current_file), "".join(rest).lstrip("/")))
        return None
    text = literal_str(value)
    return posixpath.normpath(text.lstrip("/")) if text else None


def _apply(attrs: GroupAttributes, name: str, args: list[dict[str, Any]], facts: PhpFacts) -> GroupAttributes:
    """One group attribute step (``prefix``, ``middleware`` …); unknown steps leave attributes unchanged."""
    first = argument(args, 0)
    if name == "prefix":
        return replace(attrs, prefix=join(attrs.prefix, literal_str(first, facts) or ""))
    if name == "middleware":
        return replace(attrs, middleware=attrs.middleware + _strings(args, facts))
    if name == "withoutMiddleware":
        return replace(attrs, without=attrs.without + _strings(args, facts))
    if name in ("name", "as"):
        return replace(attrs, name=attrs.name + (literal_str(first, facts) or ""))
    if name == "controller":
        controller = literal_str(first, facts)
        return replace(attrs, controller=controller.lstrip("\\") if controller else attrs.controller)
    if name == "namespace":
        namespace = literal_str(first, facts)
        if namespace:
            joined = namespace if namespace.startswith("\\") or not attrs.namespace else f"{attrs.namespace}\\{namespace}"
            return replace(attrs, namespace=joined.strip("\\"))
    return attrs


def _apply_options(attrs: GroupAttributes, options: Any, facts: PhpFacts) -> GroupAttributes:
    """``Route::group(['prefix' => …, 'middleware' => …], …)``."""
    for key, value in literal_items(options):
        step = {"as": "name"}.get(literal_str(key) or "", literal_str(key) or "")
        attrs = _apply(attrs, step, [{"name": None, "value": value}], facts)
    return attrs


def handler(value: Any, attrs: GroupAttributes, facts: PhpFacts) -> str | None:
    """``Class::method`` for ``[X::class, 'm']``, ``X::class`` (invokable), ``'X@m'`` or ``'m'`` in a controller group."""
    items = literal_items(value)
    if items:
        owner = literal_str(items[0][1], facts)
        action = literal_str(items[1][1], facts) if len(items) > 1 else "__invoke"
        return f"{owner.lstrip(chr(92))}::{action}" if owner and action else None
    if isinstance(value, dict) and isinstance(value.get("class"), str):
        return f"{value['class'].lstrip(chr(92))}::__invoke"
    text = literal_str(value, facts)
    if not text:
        return None
    if "@" in text:
        owner, _, action = text.partition("@")
        if "\\" not in owner.strip("\\"):
            owner = f"{attrs.namespace or DEFAULT_CONTROLLER_NAMESPACE}\\{owner}"
        return f"{owner.lstrip(chr(92))}::{action}"
    return f"{attrs.controller}::{text}" if attrs.controller else None


def _resource(verb: str, args: list[dict[str, Any]], attrs: GroupAttributes, facts: PhpFacts) -> list[dict[str, Any]]:
    name = literal_str(argument(args, 0), facts) or ""
    controller = literal_str(argument(args, 1), facts)
    if not name or not controller:
        return []
    segments = [segment for segment in name.strip("/").split(".") if segment]
    base = "/".join(f"{segment}/{{{singular(segment)}}}" for segment in segments[:-1])
    base = join(base, segments[-1])
    parameter = "{" + singular(segments[-1].rsplit("/", 1)[-1]) + "}"
    allowed = _API_RESOURCE_ACTIONS if verb == "apiResource" else tuple(a for a, _m, _s in _RESOURCE_ACTIONS)
    options = dict((literal_str(k) or "", v) for k, v in literal_items(argument(args, 2)))
    if "only" in options:
        allowed = tuple(a for a in allowed if a in literal_strings(options["only"], facts))
    if "except" in options:
        allowed = tuple(a for a in allowed if a not in literal_strings(options["except"], facts))
    return [{"methods": methods, "uri": base + suffix.replace("{p}", parameter), "action": action,
             "handler": f"{controller.lstrip(chr(92))}::{action}"}
            for action, methods, suffix in _RESOURCE_ACTIONS if action in allowed]


def _define(name: str, args: list[dict[str, Any]], attrs: GroupAttributes, facts: PhpFacts) -> list[dict[str, Any]] | None:
    """Routes one verb step defines; None when the step is not a route definition."""
    def uri(value: Any) -> str:
        text = literal_str(value, facts)
        return text if text is not None else "{unresolved}"

    if name in _VERBS:
        return [{"methods": (_VERBS[name],), "uri": uri(argument(args, 0)), "handler": handler(argument(args, 1), attrs, facts)}]
    if name == "any":
        return [{"methods": (ANY_METHOD,), "uri": uri(argument(args, 0)), "handler": handler(argument(args, 1), attrs, facts)}]
    if name == "match":
        methods = tuple(m.upper() for m in literal_strings(argument(args, 0), facts)) or (ANY_METHOD,)
        return [{"methods": methods, "uri": uri(argument(args, 1)), "handler": handler(argument(args, 2), attrs, facts)}]
    if name in _GET_ONLY:
        return [{"methods": ("GET",), "uri": uri(argument(args, 0)), "handler": None}]
    if name in ("resource", "apiResource"):
        return _resource(name, args, attrs, facts)
    if name in ("resources", "apiResources"):
        single = name[:-1]
        return [route for key, value in literal_items(argument(args, 0))
                for route in _resource(single, [{"name": None, "value": key}, {"name": None, "value": value}], attrs, facts)]
    return None


def _modify(routes: list[dict[str, Any]], name: str, args: list[dict[str, Any]], facts: PhpFacts) -> list[dict[str, Any]]:
    """A step after a route definition: ``->name()``, ``->middleware()``, ``->only()`` …"""
    if name == "middleware":
        for route in routes:
            route["middleware"] = route.get("middleware", ()) + _strings(args, facts)
    elif name == "withoutMiddleware":
        for route in routes:
            route["without"] = route.get("without", ()) + _strings(args, facts)
    elif name == "can":
        ability = ",".join(_strings(args, facts))
        for route in routes:
            route["middleware"] = route.get("middleware", ()) + (f"can:{ability}",)
    elif name == "name":
        for route in routes:
            route["name"] = literal_str(argument(args, 0), facts)
    elif name in ("only", "except"):
        listed = set(_strings(args, facts))
        return [r for r in routes if "action" not in r or ((r["action"] in listed) == (name == "only"))]
    return routes


class _RouteFileReader:
    def __init__(self, facts: PhpFacts) -> None:
        self.facts = facts
        self.records: dict[tuple[str, str], RouteRecord] = {}   # (method, path) → route
        self.loaded: set[str] = set()

    def read(self, path: str, base: GroupAttributes, stack: tuple[str, ...] = (), routes_only: bool = False) -> None:
        """Every route defined in ``path`` (and the files it loads) under ``base`` attributes."""
        if path in stack or self.facts.file(path) is None:
            return
        stack = (*stack, path)
        if not routes_only:
            self.loaded.add(path)
        calls = {call["id"]: call for call, _file in self.facts.calls(path) if isinstance(call.get("id"), int)}
        inner: dict[int, GroupAttributes] = {}

        def attributes_inside(call_id: int | None) -> GroupAttributes:
            """Attributes for code inside the closure argument of call ``call_id``."""
            if call_id is None or call_id not in calls:
                return base
            if call_id not in inner:
                call = calls[call_id]
                inner[call_id] = self._chain(call, attributes_inside(call.get("parent")), path, stack)[0]
            return inner[call_id]

        for call in calls.values():
            if _is_route_builder(call) and call["id"] not in inner:
                attrs, routes = self._chain(call, attributes_inside(call.get("parent")), path, stack)
                inner[call["id"]] = attrs
                self._add(routes, path)
        for include in self.facts.includes(path):
            target = file_argument(include.get("path"), path)
            if target:
                self.read(target, attributes_inside(include.get("parent")), stack)

    def _chain(self, call: dict[str, Any], outer: GroupAttributes, path: str,
               stack: tuple[str, ...]) -> tuple[GroupAttributes, list[dict[str, Any]]]:
        """``(attributes inside its group closure, routes it defines)`` for one fluent chain."""
        if not _is_route_builder(call):
            return outer, []
        attrs, routes = outer, []
        for step in _steps(call):
            name, args = step.get("name") or "", step.get("args") or []
            if routes:
                routes = _modify(routes, name, args, self.facts)
                continue
            if name == "group":
                first = argument(args, 0)
                target = first
                if literal_items(first) and argument(args, 1) is not None:
                    attrs, target = _apply_options(attrs, first, self.facts), argument(args, 1)
                if not (isinstance(target, dict) and target.get("closure")):
                    file = file_argument(target, path)
                    if file:
                        self.read(file, attrs, stack)
                return attrs, []
            defined = _define(name, args, attrs, self.facts)
            if defined is not None:
                for route in defined:
                    route.update(line=int(step.get("line") or call.get("line") or 1), attrs=attrs)
                routes = defined
                continue
            attrs = _apply(attrs, name, args, self.facts)
        return attrs, routes

    def _add(self, routes: list[dict[str, Any]], path: str) -> None:
        for route in routes:
            attrs: GroupAttributes = route["attrs"]
            without = set(attrs.without) | set(route.get("without", ()))
            middleware = tuple(m for m in (*attrs.middleware, *route.get("middleware", ())) if m not in without)
            name = route.get("name")
            record = RouteRecord(framework=FRAMEWORK, methods=tuple(route["methods"]), path=join(attrs.prefix, route["uri"]),
                                 handler=route.get("handler"), file=path, line=route["line"],
                                 name=(attrs.name + name) if name else None, middleware=tuple(dict.fromkeys(middleware)))
            for method in record.methods:
                old = self.records.get((method, record.path))
                if old is not None and old is not record:  # a later definition replaces that method only
                    narrowed = replace(old, methods=tuple(m for m in old.methods if m != method))
                    for key, value in list(self.records.items()):
                        if value is old:
                            self.records[key] = narrowed
                self.records[(method, record.path)] = record

    def routes(self) -> list[RouteRecord]:
        unique = {id(record): record for record in self.records.values()}
        return sorted(unique.values(), key=lambda r: (r.path, r.methods, r.handler or ""))


def _with_routing(facts: PhpFacts) -> list[tuple[str, GroupAttributes]]:
    """Route files named in ``bootstrap/app.php`` ``->withRouting(...)`` (Laravel 11+)."""
    entries: list[tuple[str, GroupAttributes]] = []
    for call, _file in facts.calls("bootstrap/app.php"):
        for step in _steps(call):
            if step.get("name") != "withRouting":
                continue
            args = step.get("args") or []
            api_prefix = literal_str(argument(args, 99, "apiPrefix"), facts)
            for key, attrs in (("web", GroupAttributes(middleware=("web",))),
                               ("api", GroupAttributes(prefix=join(api_prefix if api_prefix is not None else "api"),
                                                       middleware=("api",)))):
                value = argument(args, 99, key)
                for item in ([v for _k, v in literal_items(value)] or [value]) if value is not None else []:
                    target = file_argument(item, "bootstrap/app.php")
                    if target:
                        entries.append((target, attrs))
    return entries


def provide_routes(context: RevisionContext, facts: PhpFacts) -> list[RouteRecord]:
    reader = _RouteFileReader(facts)
    for path, attrs in _with_routing(facts):
        reader.read(path, attrs)
    # Route::...->group(<file>) in bootstrap/app.php (then: closures) and service providers.
    for file in facts.files():
        path = str(file.get("path", ""))
        if path.startswith(_PROVIDER_PREFIXES) and path.endswith(".php"):
            reader.read(path, GroupAttributes(), routes_only=True)
    if not reader.loaded:
        reader.read("routes/web.php", GroupAttributes(middleware=("web",)))
        reader.read("routes/api.php", GroupAttributes(prefix="/api", middleware=("api",)))
    return reader.routes()


def route_table(records: Iterable[RouteRecord]) -> list[dict[str, Any]]:
    """The ``artisan route:list`` shaped rows the plugin's structural analysis reports."""
    rows = []
    for record in records:
        owner, _, action = (record.handler or "").partition("::")
        for method in record.methods:
            rows.append({"method": method, "uri": record.path,
                         "action": (f"{owner}@{action}" if action != "__invoke" else owner) if record.handler else "closure",
                         "name": record.name, "middleware": list(record.middleware), "file": record.file,
                         "line": record.line, "source": "facts"})
    return rows
