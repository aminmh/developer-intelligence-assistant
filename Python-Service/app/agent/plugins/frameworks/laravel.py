"""Laravel framework plugin — the first implementation of the generic
framework adapter (spec §1, §5, §25).

Contains Laravel-specific knowledge ONLY: marker detection, version parsing,
route/module structure analysis, artisan-aware validators. The core agent
never references Laravel directly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from agent.core.capability import Capability
from agent.core.types import (
    AnalysisFinding,
    AnalysisResult,
    AnalysisScope,
    DetectionResult,
    Project,
    RuntimeInspection,
    Tool,
    Workspace,
)
from agent.interfaces.framework import FrameworkPlugin
from agent.interfaces.validator import Validator
from agent.plugins.validators import ArtisanValidator, PhpUnitValidator

_LARAVEL_REQUIRE = "laravel/framework"
_VERSION_RE = re.compile(r"\^?v?(\d+)")


def _composer_path(project: Project) -> Path | None:
    """Locate composer.json declaring laravel/framework, up to 2 dirs deep.

    Uploaded zips often wrap the app in a top-level folder (Archive/…), so the
    manifest is not always at the workspace root.
    """
    for rel in ("composer.json",):
        candidate = project.root / rel
        if candidate.is_file():
            return candidate
    for child in sorted(project.root.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "__MACOSX")):
            continue
        candidate = child / "composer.json"
        if candidate.is_file():
            return candidate
    return None


def _laravel_root(workspace: Workspace) -> Path | None:
    """Return the directory holding composer.json w/ laravel/framework."""
    root = workspace.root
    candidates = [root, *sorted(p for p in root.iterdir() if p.is_dir())]
    for base in candidates:
        if base.name.startswith((".", "__MACOSX")):
            continue
        manifest = base / "composer.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        if _LARAVEL_REQUIRE in (data.get("require") or {}):
            return base
    return None


class LaravelFrameworkPlugin(FrameworkPlugin):
    name = "laravel"
    language = "php"

    capabilities = (
        Capability.AST_ANALYSIS,
        Capability.DEPENDENCY_ANALYSIS,
        Capability.ROUTE_ANALYSIS,
        Capability.RUNTIME_INSPECTION,
        Capability.TEST_EXECUTION,
    )

    def detect(self, project: Project) -> DetectionResult:
        manifest = _composer_path(project)
        if manifest is None:
            return DetectionResult(name="laravel", confidence=0.0)
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return DetectionResult(name="laravel", confidence=0.0)

        requires = data.get("require", {})
        if _LARAVEL_REQUIRE not in requires:
            return DetectionResult(name="laravel", confidence=0.0)

        version = None
        match = _VERSION_RE.search(str(requires[_LARAVEL_REQUIRE]))
        if match:
            version = match.group(1)

        confidence = 0.95
        if project.has_any(("artisan",)):
            confidence = 0.99
        return DetectionResult(name="laravel", confidence=confidence, version=version)

    def analyze(self, workspace: Workspace) -> AnalysisResult:
        """Structural Laravel analysis: routes (static DSL parse), models.

        Runtime ``php artisan route:list`` (= Router::getRoutes()) is attempted
        only when vendor/ is installed — uploaded zips usually ship without it,
        so the static parser is the primary source here.
        """
        scope = AnalysisScope(kind="routes", detail="Laravel route/module structure")
        findings: list[AnalysisFinding] = []
        data: dict = {}

        root = _laravel_root(workspace) or workspace.root
        routes_dir = root / "routes"
        route_files = sorted(routes_dir.glob("*.php")) if routes_dir.is_dir() else []

        route_table: list[dict] = []
        if route_files:
            from agent.plugins.frameworks.laravel_routes import (
                parse_routes_directory,
            )

            route_table = parse_routes_directory(
                [(p.name, p.read_text(encoding="utf-8", errors="replace")) for p in route_files]
            )
            findings.append(
                AnalysisFinding(
                    message=f"{len(route_files)} route file(s): "
                    + ", ".join(p.name for p in route_files),
                    kind="info",
                )
            )
            verbs = {}
            for r in route_table:
                verbs[r["method"]] = verbs.get(r["method"], 0) + 1
            verb_summary = ", ".join(f"{k} {v}" for k, v in sorted(verbs.items()))
            findings.append(
                AnalysisFinding(
                    message=f"{len(route_table)} route(s): {verb_summary}",
                    kind="info",
                )
            )

            # Runtime truth when the project is runnable (vendor + artisan + php):
            # ``php artisan route:list`` == Router::getRoutes(). Static table is
            # kept as fallback when execution is unavailable.
            runtime_routes = _runtime_routes(root)
            if runtime_routes is not None:
                route_table = runtime_routes
                findings.append(
                    AnalysisFinding(
                        message=f"{len(route_table)} route(s) from artisan route:list (runtime)",
                        kind="info",
                    )
                )
        else:
            findings.append(
                AnalysisFinding(message="No routes/ directory found", kind="warning")
            )

        data["routes"] = route_table

        models: list[dict] = []
        models_dir = root / "app" / "Models"
        if models_dir.is_dir():
            from agent.plugins.frameworks.laravel_models import (
                parse_models_directory,
            )

            models = parse_models_directory(
                [
                    (p.name, p.read_text(encoding="utf-8", errors="replace"))
                    for p in sorted(models_dir.rglob("*.php"))
                ]
            )
            findings.append(
                AnalysisFinding(message=f"{len(models)} Eloquent model(s)", kind="info")
            )
        data["models"] = models

        # Canonical schema slice (flat merged table list) is what profile
        # consumers render — raw migration records never leave the plugin.
        schema: list[dict] = []
        migrations: list[dict] = []
        mig_dir = root / "database" / "migrations"
        if mig_dir.is_dir():
            from agent.plugins.frameworks.laravel_schema import (
                collapse_migrations,
                parse_migrations_directory,
            )

            migrations = parse_migrations_directory(
                [
                    (p.name, p.read_text(encoding="utf-8", errors="replace"))
                    for p in sorted(mig_dir.glob("*.php"))
                ]
            )
            schema = collapse_migrations(migrations)
            findings.append(
                AnalysisFinding(
                    message=(
                        f"{len(migrations)} migration(s) → {len(schema)} table(s)"
                    ),
                    kind="info",
                )
            )
        data["schema"] = schema

        controllers: list[dict] = []
        ctrl_dir = root / "app" / "Http" / "Controllers"
        if ctrl_dir.is_dir():
            for c in sorted(ctrl_dir.rglob("*.php")):
                src = c.read_text(encoding="utf-8", errors="replace")
                cls = re.search(r"class\s+(\w+)", src)
                methods = re.findall(
                    r"public\s+function\s+(\w+)\s*\(", src
                )
                cls_name = cls.group(1) if cls else c.stem
                # Laravel's abstract base Controller carries no routes; drop
                # the boilerplate so inventories list real endpoints only.
                if cls_name == "Controller" and not methods:
                    continue
                controllers.append(
                    {
                        "name": cls_name,
                        "file": str(c.relative_to(root)),
                        "methods": methods,
                    }
                )
        data["controllers"] = controllers

        config: dict = {}
        config_dir = root / "config"
        if config_dir.is_dir():
            from agent.plugins.frameworks.laravel_config import (
                parse_config_directory,
            )

            config = parse_config_directory(
                [
                    (p.name, p.read_text(encoding="utf-8", errors="replace"))
                    for p in sorted(config_dir.glob("*.php"))
                ]
            )
        data["config"] = config

        # Deeper knowledge layers (wiring/behaviors/frontend/errors/packages)
        # scan the whole app/ tree once; each extractor owns its slice.
        code_files: list[tuple[str, str]] = []
        app_root = root / "app"
        if app_root.is_dir():
            for p in sorted(app_root.rglob("*.php")):
                code_files.append(
                    (str(p.relative_to(root)), p.read_text(encoding="utf-8", errors="replace"))
                )
        # frontend inventory needs the blade tree (app/ has no views)
        scan_files = list(code_files)
        for sub in ("resources", "bootstrap"):
            base = root / sub
            if base.is_dir():
                for p in sorted(base.rglob("*")):
                    if p.is_file() and p.suffix in {".php", ".blade.php"}:
                        scan_files.append(
                            (str(p.relative_to(root)), p.read_text(encoding="utf-8", errors="replace"))
                        )
        bootstrap_app_src = ""
        bootstrap_providers_src = ""
        if (root / "bootstrap" / "app.php").is_file():
            bootstrap_app_src = (root / "bootstrap" / "app.php").read_text(
                encoding="utf-8", errors="replace"
            )
        if (root / "bootstrap" / "providers.php").is_file():
            bootstrap_providers_src = (root / "bootstrap" / "providers.php").read_text(
                encoding="utf-8", errors="replace"
            )

        from agent.plugins.frameworks.laravel_wiring import extract_wiring

        # Legacy (pre-L11) apps register providers in config/app.php — read the
        # raw source only when bootstrap/providers.php does not exist.
        legacy_config_src = ""
        if not bootstrap_providers_src and (root / "config" / "app.php").is_file():
            legacy_config_src = (root / "config" / "app.php").read_text(
                encoding="utf-8", errors="replace"
            )
        wiring = extract_wiring(
            code_files,
            bootstrap_providers_src=bootstrap_providers_src,
            bootstrap_app_src=bootstrap_app_src,
            app_config_src=legacy_config_src,
        )
        data["wiring"] = wiring
        findings.append(
            AnalysisFinding(
                message=(
                    f"{wiring['provider_count']} provider(s) via "
                    f"{wiring['registration_site'] or 'n/a'}; "
                    f"{len(wiring['bindings'])} service binding(s); "
                    f"middleware: {len(wiring['middleware'].get('aliases', {}))} alias(es)"
                ),
                kind="info",
            )
        )

        console_src = ""
        console_file = root / "routes" / "console.php"
        if console_file.is_file():
            console_src = console_file.read_text(encoding="utf-8", errors="replace")

        from agent.plugins.frameworks.laravel_behaviors import extract_behaviors

        behaviors = extract_behaviors(code_files, console_routes_src=console_src)
        data["behaviors"] = behaviors
        findings.append(
            AnalysisFinding(
                message=(
                    f"{len(behaviors['jobs'])} job(s), {len(behaviors['events'])} event(s), "
                    f"{len(behaviors['listeners'])} listener(s), "
                    f"{len(behaviors['notifications'])} notification(s), "
                    f"{len(behaviors['schedule'])} scheduled task(s)"
                ),
                kind="info",
            )
        )

        from agent.plugins.frameworks.laravel_frontend import extract_frontend

        route_file_tuples = [
            (p.name, p.read_text(encoding="utf-8", errors="replace"))
            for p in route_files
        ]
        frontend = extract_frontend(scan_files, routes_files=route_file_tuples)
        data["frontend"] = frontend
        if frontend["views"]:
            findings.append(
                AnalysisFinding(
                    message=(
                        f"{len(frontend['views'])} blade view(s), "
                        f"{len(frontend['components'])} component(s), "
                        f"{len(frontend['route_view_links'])} route→view link(s)"
                    ),
                    kind="info",
                )
            )

        from agent.plugins.frameworks.laravel_errors import extract_errors

        errors = extract_errors(code_files, bootstrap_app_src=bootstrap_app_src)
        data["errors"] = errors
        if errors["custom_exceptions"] or errors["throw_hotspots"]:
            findings.append(
                AnalysisFinding(
                    message=(
                        f"{len(errors['custom_exceptions'])} custom exception(s); "
                        f"top throw file: "
                        f"{(errors['throw_hotspots'][0]['file'] if errors['throw_hotspots'] else 'n/a')}"
                    ),
                    kind="info",
                )
            )

        composer_json_src = ""
        composer_lock_src = ""
        composer_file = root / "composer.json"
        lock_file = root / "composer.lock"
        if composer_file.is_file():
            composer_json_src = composer_file.read_text(
                encoding="utf-8", errors="replace"
            )
        if lock_file.is_file():
            composer_lock_src = lock_file.read_text(encoding="utf-8", errors="replace")

        from agent.plugins.frameworks.laravel_packages import extract_packages

        packages = extract_packages(composer_json_src, composer_lock_src)
        data["packages"] = packages
        if packages["payment_gateways"]:
            findings.append(
                AnalysisFinding(
                    message=(
                        "Payment SDK(s): "
                        + ", ".join(
                            f"{p['gateway']} {p['version'] or p['required']}"
                            for p in packages["payment_gateways"]
                        )
                    ),
                    kind="info",
                )
            )
        elif packages["payment_absent"]:
            findings.append(
                AnalysisFinding(
                    message=(
                        "No payment SDK installed (searched: "
                        + ", ".join(packages["payment_absent"])
                        + ")"
                    ),
                    kind="info",
                )
            )

        summary = "; ".join(f.message for f in findings)
        return AnalysisResult(scope=scope, findings=findings, summary=summary, data=data)

    def inspect_runtime(self, runtime: object) -> RuntimeInspection:
        # TODO(scope-B): run `php artisan --version` against the injected
        # Runtime (never direct subprocess/docker here).
        return RuntimeInspection(
            name="artisan",
            version=None,
            capabilities=self.capabilities,
            detail="artisan CLI runtime (version probe deferred to scope-B)",
        )

    def get_tools(self) -> list[Tool]:
        return []

    def get_validators(self) -> list[Validator]:
        return [ArtisanValidator(), PhpUnitValidator()]


def _runtime_routes(laravel_root: Path) -> list[dict] | None:
    """Best-effort ``php artisan route:list --json``.

    Returns the live route table, or None when the project is not runnable
    (missing vendor / php binary) or the command fails. Never raises.
    """
    import shutil
    import subprocess

    try:
        if not (laravel_root / "vendor" / "autoload.php").is_file():
            return None
        if not (laravel_root / "artisan").is_file():
            return None
        php = shutil.which("php")
        if not php:
            return None
        proc = subprocess.run(
            [php, "artisan", "route:list", "--json"],
            cwd=str(laravel_root),
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
            env={
                **__import__("os").environ,
                "APP_ENV": "testing",
                "APP_KEY": "base64:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "APP_DEBUG": "false",
                "DB_CONNECTION": "sqlite",
                "DB_DATABASE": ":memory:",
                "CACHE_DRIVER": "array",
                "SESSION_DRIVER": "array",
                "QUEUE_CONNECTION": "sync",
                "MAIL_MAILER": "array",
            },
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout or "[]")
        out = []
        for r in data if isinstance(data, list) else []:
            out.append(
                {
                    "method": str(r.get("method") or "GET"),
                    "uri": str(r.get("uri") or "/"),
                    "action": str(r.get("action") or r.get("controller") or "?"),
                    "name": r.get("name") or None,
                    "middleware": r.get("middleware") or [],
                    "source": "runtime",
                }
            )
        return out or None
    except Exception:  # noqa: BLE001 — runtime path is best-effort
        return None

