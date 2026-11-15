"""HTTP routes a back-end project declares, read statically from a revision's source.

Plan: @.archcore/frontend-assist/frontend-developer-assistance.plan.md (Phase 4, task 21).

Used to compare a provider project with its API contract. Two frameworks are
covered: Laravel (``routes/*.php``, through the existing static parser) and
Symfony (``#[Route]`` attributes, ``@Route`` annotations, and YAML route files).
Nothing is executed, so routes registered at run time (packages, loops over
variables) are not seen; the result says which framework was read.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

MAX_ROUTES = 5000
MAX_PHP_FILES = 4000
_MAX_FILE_BYTES = 1_000_000
_SKIP_DIRS = {"vendor", "node_modules", "var", "storage", "public", "tests", "test", ".git", "migrations"}
ANY_METHOD = "ANY"


def _composer(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / "composer.json").read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    return {**(data.get("require") or {}), **(data.get("require-dev") or {})} if isinstance(data, dict) else {}


def find_backend(root: Path) -> tuple[Path, str] | None:
    """The folder that holds a Laravel or Symfony application, and which one it is."""
    candidates = [root, *sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "__")))]
    for base in candidates:
        packages = _composer(base)
        if "laravel/framework" in packages:
            return base, "laravel"
        if any(name.startswith("symfony/") for name in packages):
            return base, "symfony"
    return None


def _join(*parts: str) -> str:
    return "/" + "/".join(part.strip("/") for part in parts if part and part.strip("/"))


# ── Laravel ──────────────────────────────────────────────────────────────────

_API_PREFIX = re.compile(r"apiPrefix\s*:\s*['\"]([^'\"]*)['\"]")


def _laravel_routes(root: Path) -> list[dict[str, Any]]:
    from agent.plugins.frameworks.laravel.routes import parse_routes_from_source

    api_prefix = "api"  # Laravel's default for routes/api.php
    try:
        configured = _API_PREFIX.search((root / "bootstrap" / "app.php").read_text(encoding="utf-8", errors="replace"))
        if configured:
            api_prefix = configured.group(1)
    except OSError:
        pass
    routes = []
    directory = root / "routes"
    for path in sorted(directory.glob("*.php")) if directory.is_dir() else []:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        prefix = api_prefix if path.name == "api.php" else ""
        for route in parse_routes_from_source(source):
            if route["action"].startswith(("view:", "redirect:")) or route["method"] in ("OPTIONS", "HEAD"):
                continue
            routes.append({"method": ANY_METHOD if route["method"] == "ANY" else route["method"],
                           "path": _join(prefix, route["uri"]), "action": route["action"],
                           "file": f"routes/{path.name}", "line": None, "api": path.name == "api.php"})
    return routes


# ── Symfony ──────────────────────────────────────────────────────────────────

_ROUTE_ATTRIBUTE = re.compile(r"#\[\s*(?:[A-Za-z_\\]+\\)?Route\s*\((?P<args>.*?)\)\s*(?:,[^\]]*)?\]", re.DOTALL)
_ROUTE_ANNOTATION = re.compile(r"@(?:[A-Za-z_\\]+\\)?Route\s*\((?P<args>[^)]*)\)")
_FIRST_STRING = re.compile(r"""^\s*(?:path\s*[:=]\s*)?(['"])(?P<path>.*?)\1""", re.DOTALL)
_NAMED_PATH = re.compile(r"""\bpath\s*[:=]\s*(['"])(?P<path>.*?)\1""", re.DOTALL)
_METHODS = re.compile(r"""\bmethods\s*[:=]\s*(?:[\[{](?P<many>[^\]}]*)[\]}]|(['"])(?P<one>[A-Za-z]+)\2)""")
_CLASS = re.compile(r"^\s*(?:final\s+|abstract\s+|readonly\s+)*class\s+(\w+)", re.MULTILINE)
_FUNCTION = re.compile(r"function\s+(\w+)\s*\(")


def _route_arguments(arguments: str) -> tuple[str, list[str]]:
    found = _NAMED_PATH.search(arguments) or _FIRST_STRING.match(arguments)
    path = found.group("path") if found else ""
    methods_match = _METHODS.search(arguments)
    methods: list[str] = []
    if methods_match:
        raw = methods_match.group("many") if methods_match.group("many") is not None else methods_match.group("one")
        methods = [name.upper() for name in re.findall(r"[A-Za-z]+", raw or "")]
    return path, methods or [ANY_METHOD]


def _symfony_controller_routes(source: str, relative: str) -> list[dict[str, Any]]:
    declared = _CLASS.search(source)
    if not declared:
        return []
    class_name, class_at = declared.group(1), declared.start()
    prefix, class_methods = "", None
    routes = []
    for match in sorted([*_ROUTE_ATTRIBUTE.finditer(source), *_ROUTE_ANNOTATION.finditer(source)], key=lambda m: m.start()):
        path, methods = _route_arguments(match.group("args"))
        if match.start() < class_at:                       # a route on the class is a prefix for its methods
            prefix, class_methods = path, None if methods == [ANY_METHOD] else methods
            continue
        function = _FUNCTION.search(source, match.end())
        action = f"{class_name}::{function.group(1)}" if function else class_name
        if methods == [ANY_METHOD] and class_methods:
            methods = class_methods
        line = source.count("\n", 0, match.start()) + 1
        routes.extend({"method": method, "path": _join(prefix, path), "action": action, "file": relative,
                       "line": line, "api": False} for method in methods)
    return routes


def _symfony_yaml_routes(root: Path) -> list[dict[str, Any]]:
    routes = []
    config = root / "config"
    files = [config / "routes.yaml", config / "routes.yml",
             *sorted((config / "routes").glob("*.y*ml") if (config / "routes").is_dir() else [])]
    for path in files:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, yaml.YAMLError):
            continue
        for name, entry in (data or {}).items() if isinstance(data, dict) else []:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or "resource" in entry:
                continue
            methods = entry.get("methods") or [ANY_METHOD]
            methods = [methods] if isinstance(methods, str) else methods
            routes.extend({"method": str(method).upper(), "path": _join(entry["path"]),
                           "action": str(entry.get("controller") or name), "file": path.relative_to(root).as_posix(),
                           "line": None, "api": False} for method in methods)
    return routes


def _symfony_routes(root: Path) -> tuple[list[dict[str, Any]], bool]:
    routes = _symfony_yaml_routes(root)
    files_read, truncated = 0, False
    source_root = root / "src" if (root / "src").is_dir() else root
    for directory, dirs, names in os.walk(source_root):
        dirs[:] = sorted(name for name in dirs if name.lower() not in _SKIP_DIRS)
        for name in sorted(names):
            if not name.endswith(".php"):
                continue
            if files_read >= MAX_PHP_FILES:
                truncated = True
                break
            files_read += 1
            path = Path(directory) / name
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "Route" in source:
                routes.extend(_symfony_controller_routes(source, path.relative_to(root).as_posix()))
    return routes, truncated


# ── entry points ─────────────────────────────────────────────────────────────

def backend_routes(root: str | Path) -> dict[str, Any]:
    """``{"framework", "routes", "notes"}`` for the back-end application under ``root``."""
    found = find_backend(Path(root))
    if found is None:
        return {"framework": None, "routes": [],
                "notes": ["No Laravel or Symfony application was found, so the project's routes could not be read."]}
    base, framework = found
    notes: list[str] = []
    if framework == "laravel":
        routes = _laravel_routes(base)
    else:
        routes, truncated = _symfony_routes(base)
        if truncated:
            notes.append(f"Only the first {MAX_PHP_FILES} PHP files were read for routes.")
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for route in routes:
        unique.setdefault((route["method"], route["path"]), route)
    routes = sorted(unique.values(), key=lambda route: (route["path"], route["method"]))[:MAX_ROUTES]
    if not routes:
        notes.append("No routes were found in the project's route files.")
    return {"framework": framework, "routes": routes, "notes": notes}


class RoutesUnavailable(LookupError):
    """There is no revision source to read routes from; the message is safe to show."""


def routes_for_revision(project_id: str, revision_id: str | None = None, *, registry: Any = None,
                        source_resolver: Any = None) -> dict[str, Any]:
    """Routes of ``revision_id`` (default: the project's newest ready revision)."""
    from agent_tools.types import ToolCallRejected, ToolContext

    if registry is None:
        from telemetry.registry import default_registry
        registry = default_registry()
    if source_resolver is None:
        from agent_tools.source import resolve_source as source_resolver
    revision = (registry.get_by_revision_id(project_id, revision_id) if revision_id
                else registry.latest_ready(project_id))
    if not revision or revision.get("state") != "ready":
        raise RoutesUnavailable("The project has no indexed revision yet. Index the project first.")
    try:
        source = source_resolver(ToolContext(workflow="chat", project_id=project_id, revision_id=revision["revision_id"]))
    except ToolCallRejected as exc:
        raise RoutesUnavailable(exc.message) from exc
    return {"project_id": project_id, "revision_id": revision["revision_id"], **backend_routes(source.path)}
