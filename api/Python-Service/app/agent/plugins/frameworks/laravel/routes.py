"""Static Laravel route-DSL parser (pure, dependency-free).

Uploaded project zips normally have NO vendor/ and NO .env, so running
``php artisan route:list`` (= ``Router::getRoutes()`` at runtime) is
impossible. Instead we statically parse ``routes/*.php`` into a route table
with the same shape artisan emits:

    {"method": "GET", "uri": "/login",
     "action": "App\\Http\\Controllers\\Auth\\AuthenticationController@showLoginForm",
     "name": "login", "middleware": ["guest"]}

Coverage — the everyday Laravel route DSL:

    Route::VERB('/uri', Action)->name('x')->middleware('y')
    Route::match(['GET','POST'], '/uri', Action)
    Route::any(...)
    Route::view('/uri', 'view.name')->name('x')
    Route::redirect('/from', '/to')
    Route::resource('/users', Ctrl::class)  (+ apiResource)
    Route::prefix('api')->group(fn)         — applied to routes inside it

Actions: ``[Ctrl::class, 'method']``, ``'Ctrl@method'``, ``'Ctrl@method'``
(qualified or not) or a closure (action = ``closure``). Routes declared via
variables / dynamic group loops are intentionally not resolved statically.
"""

from __future__ import annotations

import re
from typing import Any

_VERB = r"get|post|put|patch|delete|options|any"
_ROUTE_CALL_RE = re.compile(
    rf"Route::(?P<verb>{_VERB}|match|view|redirect|resource|apiResource)\s*\("
)
_VERB_ONLY_RE = re.compile(rf"^(?P<verb>{_VERB})$")
_METHODS_ARRAY_RE = re.compile(r"^\[\s*(?P<methods>[^\]]*)\]")
_STRING_RE = re.compile(r"^(?P<quote>['\"])(?P<val>.*?)(?P=quote)", re.DOTALL)
# NB: no ^ anchor — used with re.match(code, pos), where ^ would never match
# away from the string start.
_CHAIN_RE = re.compile(r"->(?P<name>name|middleware)\s*\(")
_CLASS_PAIR_RE = re.compile(
    r"\[\s*(?P<cls>[^\]\[]+?)::\s*class\s*,\s*['\"](?P<method>[^'\"]+)['\"]\s*\]"
)
_STRING_ACTION_RE = re.compile(
    r"['\"](?P<a>[A-Za-z_\\][A-Za-z0-9_\\]*@[A-Za-z_][A-Za-z0-9_]*)['\"]"
)
_GROUP_PREFIX_RE = re.compile(
    r"Route::prefix\s*\(\s*(?P<quote>['\"])(?P<prefix>[A-Za-z0-9_./:=-]+)(?P=quote)"
    r"\s*\)\s*"
    r"(?:\s*->[A-Za-z_]+\([^)]*\))*"  # chained modifiers (middleware…)
    r"\s*->\s*group\s*\(",
)

_RESOURCE_METHODS: list[tuple[str, str, str]] = [
    ("GET", "", "index"),
    ("POST", "", "store"),
    ("GET", "/{id}", "show"),
    ("PUT", "/{id}", "update"),
    ("PATCH", "/{id}", "update"),
    ("DELETE", "/{id}", "destroy"),
]


def _split_top_args(body: str) -> list[str]:
    """Split ``body`` on top-level commas (arrays/strings respected)."""
    args: list[str] = []
    depth = 0
    start = 0
    in_s = in_d = False
    i = 0
    while i < len(body):
        ch = body[i]
        if in_d and ch == "\\":
            i += 2
            continue
        if in_s and ch == "\\":
            i += 2
            continue
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif not in_s and not in_d:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                args.append(body[start:i].strip())
                start = i + 1
        i += 1
    tail = body[start:].strip()
    if tail:
        args.append(tail)
    return args


def _match_balanced(code: str, open_idx: int) -> int:
    """Return index of the ')' closing the '(' at ``open_idx`` (strings ok)."""
    depth = 0
    in_s = in_d = False
    i = open_idx
    while i < len(code):
        ch = code[i]
        if in_d and ch == "\\":
            i += 2
            continue
        if in_s and ch == "\\":
            i += 2
            continue
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif not in_s and not in_d:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return len(code) - 1


def _parse_action_arg(arg: str) -> str:
    pair = _CLASS_PAIR_RE.search(arg)
    if pair:
        cls = pair.group("cls").strip().strip("\\").replace("\\\\", "\\")
        if "\\" not in cls:
            cls = f"App\\Http\\Controllers\\{cls}"
        return f"{cls}@{pair.group('method')}"
    sa = _STRING_ACTION_RE.search(arg)
    if sa:
        return sa.group("a").replace("\\\\", "\\")
    if "function" in arg or "fn" in arg.split("=>")[0]:
        return "closure"
    if "::class" in arg:
        cls = arg.split("::class")[0].strip().strip("\\").replace("\\\\", "\\")
        if "\\" not in cls:
            cls = f"App\\Http\\Controllers\\{cls}"
        return cls  # resource target — expanded by caller
    return "closure"


def _collect_chains(code: str, end: int) -> dict[str, Any]:
    """Gather ``->name(...)`` / ``->middleware(...)`` chains right after end."""
    out: dict[str, Any] = {"name": None, "middleware": []}
    i = end + 1
    while i < len(code):
        while i < len(code) and code[i] in " \t\n":
            i += 1
        m = _CHAIN_RE.match(code, i)
        if not m:
            break
        open_p = code.find("(", i)
        close = _match_balanced(code, open_p)
        body = code[open_p + 1 : close]
        key = m.group("name")
        if key == "name":
            sm = _STRING_RE.match(body.strip())
            out["name"] = sm.group("val") if sm else None
        else:  # middleware
            arr = _METHODS_ARRAY_RE.match(body.strip())
            if arr:
                out["middleware"] = []
                for raw in arr.group("methods").split(","):
                    token = raw.strip()
                    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
                        token = token[1:-1]
                    token = token.strip("\\")
                    if token:
                        out["middleware"].append(token)
            else:
                sm = _STRING_RE.match(body.strip())
                if sm:
                    out["middleware"] = [sm.group("val")]
        i = close + 1
    return out


def _prefix_regions(code: str) -> list[tuple[int, int, str]]:
    """Return (open, close, prefix) for every ``Route::prefix('x')->group(``."""
    regions: list[tuple[int, int, str]] = []
    for m in _GROUP_PREFIX_RE.finditer(code):
        open_p = m.end() - 1  # points at '(' of group(
        close = _match_balanced(code, open_p)
        regions.append((m.start(), close, m.group("prefix")))
    regions.sort()
    return regions


def _prefixes_for(pos: int, regions: list[tuple[int, int, str]]) -> str:
    """Join the prefixes of every group region containing ``pos``."""
    parts = [r[2] for r in regions if r[0] < pos < r[1]]
    return "/".join(p.strip("/") for p in parts)


def parse_routes_from_source(code: str) -> list[dict[str, Any]]:
    """Parse one route file's source into route records (stable order)."""
    routes: list[dict[str, Any]] = []
    regions = _prefix_regions(code)
    seen: set[int] = set()

    for m in _ROUTE_CALL_RE.finditer(code):
        if m.start() in seen:
            continue
        verb = m.group("verb")
        open_p = code.find("(", m.start())
        call_close = _match_balanced(code, open_p)
        inner = code[open_p + 1 : call_close]
        seen.add(m.start())
        chains = _collect_chains(code, call_close)
        prefix = _prefixes_for(m.start(), regions)
        routes.extend(_parse_one(verb, inner, chains, prefix))
    return routes


def _parse_one(
    verb: str, chunk: str, chains: dict[str, Any], prefix: str
) -> list[dict[str, Any]]:
    args = _split_top_args(chunk)
    if not args:
        return []

    def _uri(u: str) -> str:
        sm = _STRING_RE.match(u.strip())
        return (sm.group("val") if sm else u.strip().strip("'\"")) or "/"

    def _record(method: str, uri: str, action: str) -> dict[str, Any]:
        full = f"/{prefix.strip('/')}/{uri.strip('/')}" if prefix else uri
        full = re.sub(r"/+", "/", full) or "/"
        if not full.startswith("/"):
            full = "/" + full
        return {
            "method": method,
            "uri": full,
            "action": action,
            "name": chains.get("name"),
            "middleware": list(chains.get("middleware") or []),
        }

    if verb == "view" or verb == "redirect":
        if len(args) >= 2:
            uri = _uri(args[0])
            action = f"{verb}:{_uri(args[1])}"
            return [_record("GET", uri, action)]
        return []

    if verb in ("resource", "apiResource"):
        if len(args) < 2:
            return []
        uri = _uri(args[0])
        target = _parse_action_arg(args[1])
        return [
            _record(method, f"{uri}{suffix}", f"{target}@{mname}")
            for method, suffix, mname in _RESOURCE_METHODS
        ]

    if verb == "match":
        if len(args) < 3:
            return []
        arr = _METHODS_ARRAY_RE.match(args[0].strip())
        methods = (
            [x.strip().strip("'\"").upper() for x in arr.group("methods").split(",") if x.strip()]
            if arr
            else []
        )
        uri = _uri(args[1])
        action = _parse_action_arg(args[2])
        return [_record(m, uri, action) for m in methods]

    # simple verb
    if len(args) < 2:
        return []
    uri = _uri(args[0])
    action = _parse_action_arg(args[1])
    return [_record(verb.upper(), uri, action)]


def parse_routes_directory(files: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Parse several (file_name, source) pairs into one route table."""
    routes: list[dict[str, Any]] = []
    for fname, source in files:
        file_routes = parse_routes_from_source(source)
        for r in file_routes:
            r["file"] = fname
            r["source"] = "static"
        routes.extend(file_routes)
    return routes
