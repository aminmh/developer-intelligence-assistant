"""Static Laravel frontend extractor (blade views + components).

Answers "where is the login page?", "which views exist?":
  * views       — resources/views/**/*.blade.php as dotted names
                  (resources/views/dashboard/home.blade.php => dashboard.home)
  * components  — resources/views/components/** (classic blade) and
                  app/View/Components/*.php (anonymous + class components)
  * renderers   — which routes/controllers reference a view:
                  - Route::view('/uri', 'view.name')
                  - view('view.name') calls inside controllers
                  - controller actions whose name matches a view (showLoginForm
                    -> auth/login) when explicit view() calls are absent

Static cross-reference of explicit calls only — name-matching heuristics are
left out to stay honest.
"""

from __future__ import annotations

import re
from typing import Any

_VIEW_CALL_RE = re.compile(r"(?:view|->view)\s*\(\s*['\"]([^'\"]+)['\"]")
_ROUTE_VIEW_RE = re.compile(
    r"Route::view\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]"
)
_CLOSURE_ROUTE_RE = re.compile(
    r"Route::(get|post|put|patch|delete)\s*\(\s*['\"]([^'\"]+)['\"]"
)


def _brace_region(src: str, open_i: int) -> str:
    """Return text between the brace at ``open_i`` and its matching close."""
    depth = 0
    for i in range(open_i, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_i + 1 : i]
    return ""


def _closure_views(src: str) -> list[dict[str, Any]]:
    """Pair ``Route::VERB('uri', function (...) { ... view('x') ... })``."""
    out: list[dict[str, Any]] = []
    for cm in _CLOSURE_ROUTE_RE.finditer(src):
        # window: until the next Route:: statement so one route's closure does
        # not bleed into the following route's.
        nxt = src.find("Route::", cm.end())
        window = src[cm.end() : nxt if nxt != -1 else len(src)]
        fi = window.find("function")
        if fi == -1:
            continue
        open_i = window.find("{", fi)
        if open_i == -1:
            continue
        body = _brace_region(window, open_i)
        for vm in _VIEW_CALL_RE.finditer(body):
            out.append({"uri": "/" + cm.group(2).strip("/"), "view": vm.group(1)})
    return out


def _dotted(path: str) -> str:
    """resources/views/dashboard/home.blade.php -> dashboard.home"""
    return (
        path.replace(".blade.php", "")
        .replace("resources/views/", "")
        .replace("/", ".")
    )


def extract_frontend(
    files: list[tuple[str, str]],
    routes_files: list[tuple[str, str]] = (),
) -> dict[str, Any]:
    """Views + components inventory with explicit route/controller renderers.

    ``files`` = [(relative_path, source)] under the framework root;
    ``routes_files`` = [(name, source)] of routes/*.php.
    """
    views: list[dict[str, Any]] = []
    component_dirs = ("resources/views/components/", "app/View/Components/")
    for rel, _src in files:
        if not rel.endswith(".blade.php"):
            continue
        if rel.startswith(component_dirs[0]):
            continue  # anonymous components counted below
        views.append({"name": _dotted(rel), "file": rel})

    components: list[dict[str, Any]] = []
    for rel, src in files:
        if rel.startswith(component_dirs[1]) and rel.endswith(".php"):
            cm = re.search(r"class\s+(\w+)", src)
            components.append(
                {
                    "name": cm.group(1) if cm else rel.rsplit("/", 1)[-1].replace(".php", ""),
                    "file": rel,
                    "kind": "class",
                }
            )
        elif rel.startswith(component_dirs[0]):
            components.append(
                {"name": _dotted(rel).rsplit(".", 1)[-1], "file": rel, "kind": "blade"}
            )

    # renderers: route->view + controller view() calls
    route_views: list[dict[str, Any]] = []
    controller_views: list[dict[str, Any]] = []
    for name, src in routes_files:
        for rm in _ROUTE_VIEW_RE.finditer(src):
            route_views.append({"uri": rm.group(1), "view": rm.group(2), "file": name})
        for cv in _closure_views(src):
            cv["file"] = name
            route_views.append(cv)
    for rel, src in files:
        if not rel.startswith("app/Http/Controllers/"):
            continue
        calls = []
        for cm in _VIEW_CALL_RE.finditer(src):
            v = cm.group(1)
            if v not in {c for c in calls}:
                calls.append(v)
        if calls:
            controller_views.append({"controller": rel, "views": calls})

    return {
        "views": views,
        "components": components,
        "route_view_links": route_views,
        "controller_view_links": controller_views,
    }
