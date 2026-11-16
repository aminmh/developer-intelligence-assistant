"""Laravel framework plugin — the first implementation of the generic
framework adapter (spec §1, §5, §25).

Contains Laravel-specific knowledge ONLY: marker detection, version parsing,
route/module structure analysis, artisan-aware validators. The core agent
never references Laravel directly.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from agent.core.revision import RevisionContext
    from projects.domain.types import DomainExtraction

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


def _php_fqn_from_rel(rel: str) -> str | None:
    """PSR-4 style ``App\\`` guess from an ``app/.../File.php`` path.

    Laravel's default autoload maps ``App\\`` to ``app/``, so the file's
    relative path already encodes the class FQN: ``app/Models/Payment.php``
    -> ``App\\Models\\Payment``. This is a documented heuristic (confidence is
    set by the caller), not a read of the file's own ``namespace`` statement.
    Returns None when no FQN can be derived.
    """
    if not isinstance(rel, str) or not rel.endswith(".php"):
        return None
    raw = rel[:-4].replace("\\", "/").split("/")
    segments: list[str] = []
    for part in raw:
        segment = part.strip()
        if not segment:
            return None
        segments.append(segment[:1].upper() + segment[1:])
    if not segments:
        return None
    return "\\".join(segments)


def _evidence_hash(method: str, file: str | None, label: str) -> str:
    """Stable location hash for one evidence/source record."""
    raw = "|".join(str(part) for part in (method, file, label))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class LaravelFrameworkPlugin(FrameworkPlugin):
    name = "laravel"
    language = "php"

    capabilities = (
        Capability.AST_ANALYSIS,
        Capability.DEPENDENCY_ANALYSIS,
        Capability.ROUTE_ANALYSIS,
        Capability.ROUTE_AUTHORIZATION,
        Capability.TAINT_MODEL,
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

    def analyze(
        self, workspace: Workspace, *, runtime_routes: bool = True
    ) -> AnalysisResult:
        """Structural Laravel analysis: routes (static DSL parse), models.

        Runtime ``php artisan route:list`` (= Router::getRoutes()) is attempted
        only when vendor/ is installed AND ``runtime_routes`` is True — uploaded
        zips usually ship without it, so the static parser is the primary source
        here, and offline callers (structure census) pass ``runtime_routes=False``
        to keep the analysis subprocess-free.
        """
        scope = AnalysisScope(kind="routes", detail="Laravel route/module structure")
        findings: list[AnalysisFinding] = []
        data: dict = {}

        root = _laravel_root(workspace) or workspace.root
        routes_dir = root / "routes"
        route_files = sorted(routes_dir.glob("*.php")) if routes_dir.is_dir() else []

        route_table: list[dict] = []
        if route_files:
            # PHP facts (real parser, group middleware and route-file loading) when a
            # subprocess is allowed; the regex DSL parser stays the offline fallback.
            route_table = (_facts_route_table(root) if runtime_routes else None) or []
            if not route_table:
                from agent.plugins.frameworks.laravel.routes import (
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

            # Runtime truth when the project is runnable AND the caller wants
            # it (vendor + artisan + php): ``php artisan route:list`` ==
            # Router::getRoutes(). Static table is kept as fallback when
            # execution is unavailable or disabled (runtime_routes=False).
            live_routes = _runtime_routes(root) if runtime_routes else None
            if live_routes is not None:
                route_table = live_routes
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
            from agent.plugins.frameworks.laravel.models import (
                parse_models_directory,
            )

            models = parse_models_directory(
                [
                    (
                        str(p.relative_to(root)),
                        p.read_text(encoding="utf-8", errors="replace"),
                    )
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
            from agent.plugins.frameworks.laravel.schema import (
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
            from agent.plugins.frameworks.laravel.config import (
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

        from agent.plugins.frameworks.laravel.wiring import extract_wiring

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

        from agent.plugins.frameworks.laravel.behaviors import extract_behaviors

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

        from agent.plugins.frameworks.laravel.frontend import extract_frontend

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

        from agent.plugins.frameworks.laravel.errors import extract_errors

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

        from agent.plugins.frameworks.laravel.packages import extract_packages

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

    def extract_domain_concepts(self, workspace: Workspace) -> DomainExtraction:
        """Framework-level domain concepts for a Laravel workspace.

        Optional member of the framework-plugin shape: the core recognizer
        discovers it by attribute presence (``hasattr``), so it is NOT part of
        ``agent.interfaces.framework.FrameworkPlugin`` — that protocol is left
        untouched and stays ``runtime_checkable`` for plugins without it.

        Emits ONLY framework knowledge (route + table concepts and
        model/event/listener/job/notification enrichments keyed by the PSR-4
        ``App\\`` path convention). The PHP symbol pass is the core's job, so
        class/method concepts are never duplicated here — enrichments carry the
        same identity keys the language pass produces (``App\\Models\\X`` etc.)
        and the core's merge attaches roles/aliases to the real class concepts.
        Enrichment via path convention is a heuristic and therefore carries
        confidence 0.6 + a ``*-path-convention`` provenance; relations are only
        emitted when BOTH endpoints are uniquely resolvable (never on a leaf
        basename collision). Never raises.

        v1 limitations (documented, deliberate):
          * Controllers get no separate concept — routes carry the
            ``Controller@method`` action metadata and the php pass already
            indexes the controller class itself.
          * Config-derived concepts (``config/*.php``) are not emitted.
        """
        from projects.domain.normalizer import ConceptNormalizer
        from projects.domain.types import (
            DomainAlias,
            DomainConcept,
            DomainExtraction,
            DomainRelationship,
            DomainSource,
        )

        project_id = workspace.root.name or ""
        try:
            analysis = self.analyze(workspace, runtime_routes=False)
        except Exception as exc:  # noqa: BLE001 — plugin pass must not raise
            return DomainExtraction(
                project_id=project_id,
                concepts=[],
                relationships=[],
                error=f"laravel domain extraction failed: {exc}",
                degraded=True,
            )
        data = analysis.data if isinstance(getattr(analysis, "data", None), dict) else {}

        normalize = ConceptNormalizer().normalize
        concepts_by_identity: dict[str, DomainConcept] = {}
        relationship_keys: set[tuple[str, str, str]] = set()
        relationships: list[DomainRelationship] = []

        def space_alias(text: str) -> DomainAlias:
            tokens = [
                t
                for t in normalize(text).split()
                if t
            ]
            return DomainAlias(
                alias=" ".join(tokens) or text.strip(),
                alias_type="generated",
                confidence=0.5,
                provenance="normalizer",
            )

        def emit(
            identity: str,
            canonical: str,
            concept_type: str,
            roles: list[str],
            aliases: list[DomainAlias],
            metadata: dict,
            file: str,
            method: str,
            confidence: float,
            source_metadata: dict | None = None,
        ) -> None:
            if identity in concepts_by_identity:
                return
            source = DomainSource(
                concept_identity=identity,
                file=file or "",
                source_hash=_evidence_hash(method, file, canonical),
                start_line=None,
                end_line=None,
                method=method,
                confidence=confidence,
                metadata=source_metadata,
            )
            concepts_by_identity[identity] = DomainConcept(
                identity_key=identity,
                canonical_name=canonical,
                qualified_name=identity,
                concept_type=concept_type,
                roles=list(roles),
                normalized_name=normalize(canonical),
                description=None,
                aliases=list(aliases),
                sources=[source],
                metadata=metadata,
            )

        def add_relationship(
            relation_type: str,
            source_identity: str,
            target_identity: str,
            source_file: str,
            metadata: dict,
        ) -> None:
            key = (relation_type, source_identity, target_identity)
            if key in relationship_keys:
                return
            relationship_keys.add(key)
            relationships.append(
                DomainRelationship(
                    relation_type=relation_type,
                    source_identity=source_identity,
                    target_identity=target_identity,
                    confidence=1.0,
                    sources=[
                        DomainSource(
                            concept_identity=source_identity,
                            file=source_file or "",
                            source_hash=_evidence_hash(
                                "laravel-plugin", source_file, target_identity
                            ),
                            start_line=None,
                            end_line=None,
                            method="laravel-plugin",
                            confidence=1.0,
                            metadata=None,
                        )
                    ],
                    metadata=metadata,
                )
            )

        # --- 1. route concepts ---------------------------------------------
        for route in data.get("routes") or []:
            if not isinstance(route, dict):
                continue
            raw_method = str(route.get("method") or "GET").strip().upper() or "GET"
            method_lower = raw_method.lower()
            uri = str(route.get("uri") or "/")
            canonical = f"{raw_method} {uri}"
            identity = f"route:{method_lower}:{uri}"
            aliases = [space_alias(canonical)]
            route_name = route.get("name")
            if isinstance(route_name, str) and route_name.strip():
                aliases.append(
                    DomainAlias(
                        alias=route_name.strip(),
                        alias_type="route",
                        confidence=1.0,
                        provenance="laravel-plugin",
                    )
                )
            evidence_file = None
            source = str(route.get("source") or "static")
            route_file = route.get("file")
            if source == "static" and isinstance(route_file, str) and route_file:
                evidence_file = (
                    route_file if "/" in route_file else f"routes/{route_file}"
                )
            emit(
                identity=identity,
                canonical=canonical,
                concept_type="route",
                roles=["route"],
                aliases=aliases,
                metadata={
                    "method": method_lower,
                    "uri": uri,
                    "name": route_name,
                    "middleware": list(route.get("middleware") or []),
                    "action": route.get("action"),
                    "file": evidence_file,
                    "source": source,
                },
                file=evidence_file,
                method="laravel-routes",
                confidence=1.0,
            )

        # --- 2. schema table concepts --------------------------------------
        for table in data.get("schema") or []:
            if not isinstance(table, dict):
                continue
            table_name = str(table.get("table") or "").strip()
            if not table_name:
                continue
            columns = table.get("columns") or []
            identity = f"table:{table_name}"
            emit(
                identity=identity,
                canonical=table_name,
                concept_type="table",
                roles=["table"],
                aliases=[space_alias(table_name)],
                metadata={"table": table_name, "column_count": len(columns)},
                file="",
                method="laravel-plugin",
                confidence=1.0,
            )

        # --- 3. app/ enrichments via the PSR-4 path convention -------------
        # Each emits a concept keyed by its conventional FQN so the core merge
        # attaches the role/alias to the real class concept when the php pass
        # produced one; standalone otherwise (flagged by the path method).
        path_methods = {
            "model": "laravel-model-path-convention",
            "event": "laravel-path-convention",
            "listener": "laravel-path-convention",
            "job": "laravel-path-convention",
            "notification": "laravel-path-convention",
        }
        for model in data.get("models") or []:
            if not isinstance(model, dict):
                continue
            name = str(model.get("name") or "").strip()
            file = str(model.get("file") or "").strip()
            identity = _php_fqn_from_rel(file) if file else None
            if not name or identity is None:
                continue
            table = str(model.get("table") or "").strip() or None
            aliases = [space_alias(name)]
            if table:
                aliases.append(
                    DomainAlias(
                        alias=table,
                        alias_type="table",
                        confidence=0.9,
                        provenance="laravel-plugin",
                    )
                )
            emit(
                identity=identity,
                canonical=name,
                concept_type="model",
                roles=["model"],
                aliases=aliases,
                metadata={"table": table, "file": file},
                file=file,
                method=path_methods["model"],
                confidence=0.6,
                source_metadata={"kind": "model"},
            )

        behaviors = data.get("behaviors")
        if not isinstance(behaviors, dict):
            behaviors = {}

        event_leaf_map: dict[str, list[str]] = {}
        for event in behaviors.get("events") or []:
            if not isinstance(event, dict):
                continue
            name = str(event.get("name") or "").strip()
            file = str(event.get("file") or "").strip()
            identity = _php_fqn_from_rel(file) if file else None
            if not name or identity is None:
                continue
            emit(
                identity=identity,
                canonical=name,
                concept_type="event",
                roles=["event"],
                aliases=[space_alias(name)],
                metadata={"file": file},
                file=file,
                method=path_methods["event"],
                confidence=0.6,
                source_metadata={"kind": "event"},
            )
            event_leaf_map.setdefault(name, []).append(identity)

        for job in behaviors.get("jobs") or []:
            if not isinstance(job, dict):
                continue
            name = str(job.get("name") or "").strip()
            file = str(job.get("file") or "").strip()
            identity = _php_fqn_from_rel(file) if file else None
            if not name or identity is None:
                continue
            emit(
                identity=identity,
                canonical=name,
                concept_type="job",
                roles=["job"],
                aliases=[space_alias(name)],
                metadata={"file": file, "queueable": bool(job.get("queueable"))},
                file=file,
                method=path_methods["job"],
                confidence=0.6,
                source_metadata={"kind": "job"},
            )

        for listener in behaviors.get("listeners") or []:
            if not isinstance(listener, dict):
                continue
            name = str(listener.get("name") or "").strip()
            file = str(listener.get("file") or "").strip()
            identity = _php_fqn_from_rel(file) if file else None
            if not name or identity is None:
                continue
            emit(
                identity=identity,
                canonical=name,
                concept_type="listener",
                roles=["listener"],
                aliases=[space_alias(name)],
                metadata={"file": file, "handles": listener.get("handles")},
                file=file,
                method=path_methods["listener"],
                confidence=0.6,
                source_metadata={"kind": "listener"},
            )
            handled = str(listener.get("handles") or "").strip()
            if not handled:
                continue
            matches = event_leaf_map.get(handled) or []
            if len(matches) == 1:
                add_relationship(
                    "HANDLED_BY",
                    matches[0],
                    identity,
                    file,
                    {"event": handled, "listener": name},
                )

        for notification in behaviors.get("notifications") or []:
            if not isinstance(notification, dict):
                continue
            name = str(notification.get("name") or "").strip()
            file = str(notification.get("file") or "").strip()
            identity = _php_fqn_from_rel(file) if file else None
            if not name or identity is None:
                continue
            emit(
                identity=identity,
                canonical=name,
                concept_type="notification",
                roles=["notification"],
                aliases=[space_alias(name)],
                metadata={"file": file},
                file=file,
                method=path_methods["notification"],
                confidence=0.6,
                source_metadata={"kind": "notification"},
            )

        return DomainExtraction(
            project_id=project_id,
            concepts=list(concepts_by_identity.values()),
            relationships=relationships,
            error=None,
            degraded=False,
        )

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

    def provide(self, capability: Capability, context: "RevisionContext") -> list:
        """Routes and authorization guards from PHP facts; the Psalm taint model.

        Decision: framework-knowledge-lives-in-plugins-over-language-facts. No PHP is parsed here.
        """
        from agent.interfaces.capabilities import CapabilityUnavailable
        from agent.plugins.frameworks.laravel.authorization import provide_guards
        from agent.plugins.frameworks.laravel.routes_from_facts import provide_routes

        if capability == Capability.TAINT_MODEL:
            from pathlib import Path as _Path

            from agent.core.records import TaintStubRecord

            stub = _Path(__file__).with_name("psalm") / "taint.phpstub"
            return [TaintStubRecord(framework="laravel", analyzer="psalm", name="laravel.phpstub",
                                    content=stub.read_text(encoding="utf-8"))]
        facts = context.facts("php")
        if facts is None:
            raise CapabilityUnavailable("PHP facts are unavailable for this revision, so Laravel routes cannot be read.")
        if capability == Capability.ROUTE_ANALYSIS:
            return provide_routes(context, facts)
        if capability == Capability.ROUTE_AUTHORIZATION:
            return provide_guards(facts, provide_routes(context, facts))
        return []


def _facts_route_table(laravel_root: Path) -> list[dict] | None:
    """``route:list`` shaped rows from PHP facts; None when facts cannot be exported."""
    from agent.core.facts import facts_for_workspace
    from agent.core.revision import RevisionContext
    from agent.plugins.frameworks.laravel.routes_from_facts import provide_routes, route_table

    facts = facts_for_workspace(laravel_root)
    if facts is None:
        return None
    return route_table(provide_routes(RevisionContext.from_directory(laravel_root), facts)) or None


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

