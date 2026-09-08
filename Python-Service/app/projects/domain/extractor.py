"""Domain extraction composition + merge.

The recognizer is two deterministic, composable passes:

  1. Language-level pass (``php_symbols`` today): typed symbols + verified
     static relations from the existing PHP symbol exporter.
  2. Optional framework-level pass: a matched framework plugin may implement
     ``extract_domain_concepts(workspace)`` to layer framework concepts
     (routes, tables, model/event/listener enrichments) on top.

Composition rule: the language pass always runs for ``php`` projects; the
framework pass runs afterwards ONLY when the plugin advertises the optional
method (duck-typed via ``hasattr``). ``agent/interfaces/framework.py`` is
deliberately untouched — adding a member to that protocol would break
``runtime_checkable`` registration of plugins that do not implement the
optional method, so the method is discovered by attribute presence instead.

Automatic framework discovery: the service paths (auto-index on save,
reconcile) call ``extract_for_workspace`` with no plugin instance. When the
language pass runs for ``php`` and ``framework_plugin`` was not supplied, the
workspace is probed through the SAME agent-registry detection that
``projects.structure_profile`` uses; a confidently-detected framework plugin
that advertises the optional ``extract_domain_concepts`` method (laravel
today) is merged in automatically. The registry's own markers (composer.json
requiring laravel/framework) gate the probe, so bare-php and non-php trees
degrade to the language pass alone.

Every public entry is total: failures degrade to an extraction carrying
``error`` and the ``degraded`` flag rather than raising.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from agent.core.workspace import Workspace
from projects.domain.php_symbols import extract_php_symbol_domain
from projects.domain.types import (
    DomainAlias,
    DomainConcept,
    DomainExtraction,
    DomainRelationship,
    DomainSource,
)
from projects.structure_profile import language_census


def _resolve_root(workspace_path: str | Path) -> Path | None:
    """Existing, expanded directory for ``workspace_path``, else None."""
    try:
        path = Path(workspace_path).expanduser()
        return path if path.is_dir() else None
    except (OSError, TypeError, ValueError):
        return None


def detect_php(root: str | Path) -> bool:
    """Cheap PHP detection: composer.json/artisan markers OR any .php file.

    The file walk reuses ``structure_profile.language_census`` so the same
    skip rules apply (vendor/node_modules/… never count). Never raises.
    """
    resolved = _resolve_root(root)
    if resolved is None:
        return False
    try:
        if (resolved / "composer.json").is_file():
            return True
        if (resolved / "artisan").is_file():
            return True
        census = language_census(resolved)
        for entry in census.get("languages") or []:
            if entry.get("language") == "php" and int(entry.get("files") or 0) > 0:
                return True
    except Exception:  # noqa: BLE001 — detection is best-effort
        return False
    return False


def _discover_framework_plugin(workspace_path: str | Path) -> Any | None:
    """Resolve the framework plugin owning ``workspace_path``, else None.

    Reuses ``structure_profile``'s agent-registry detection exactly: a
    ``Project`` built from pruned relevant files is handed to
    ``registry.detect(...)`` and the top framework is looked up by name. Only a
    plugin that advertises the optional ``extract_domain_concepts`` method is
    returned, so the caller can merge framework concepts with no plugin wired
    in. Never raises: an unreadable root, missing marker or a failed registry
    probe all degrade to None (the language pass alone is still a complete
    extraction).
    """
    root = _resolve_root(workspace_path)
    if root is None:
        return None
    try:
        from projects.structure_profile import _plugin_detect

        detection, registry, top = _plugin_detect(root)
        if detection is None or registry is None or top is None:
            return None
        plugin = registry.framework(top.name)
        if plugin is None or not hasattr(plugin, "extract_domain_concepts"):
            return None
        return plugin
    except Exception:  # noqa: BLE001 — plugin discovery is best-effort
        return None


def _merge_concepts(
    primary: DomainConcept, secondary: DomainConcept
) -> DomainConcept:
    """Union two concepts with the same identity key (deterministic)."""
    roles = list(primary.roles)
    for role in secondary.roles:
        if role not in roles:
            roles.append(role)

    alias_keys: set[str] = set()
    aliases: list[DomainAlias] = []
    for alias in [*primary.aliases, *secondary.aliases]:
        if alias.alias in alias_keys:
            continue
        alias_keys.add(alias.alias)
        aliases.append(alias)

    source_keys: set[tuple[str, Any, Any, str, float]] = set()
    sources: list[DomainSource] = []
    for source in [*primary.sources, *secondary.sources]:
        key = (
            source.file,
            source.start_line,
            source.end_line,
            source.method,
            source.confidence,
        )
        if key in source_keys:
            continue
        source_keys.add(key)
        sources.append(source)

    metadata = dict(primary.metadata or {})
    for key, value in (secondary.metadata or {}).items():
        metadata.setdefault(key, value)

    return DomainConcept(
        identity_key=primary.identity_key,
        canonical_name=primary.canonical_name,
        qualified_name=primary.qualified_name,
        concept_type=primary.concept_type,
        roles=roles,
        normalized_name=primary.normalized_name,
        description=primary.description or secondary.description,
        aliases=aliases,
        sources=sources,
        metadata=metadata,
    )


def merge_extractions(
    extractions: list[DomainExtraction],
) -> DomainExtraction:
    """Merge passes into one extraction; union semantics, deterministic.

    Concepts union by ``identity_key`` (roles/aliases/sources union, primary
    classification kept from the first occurrence, description first non-null);
    relationships union by ``(relation_type, source_identity,
    target_identity)`` keeping the first. Errors aggregate; the result is
    degraded when any input was.
    """
    passes = [ex for ex in extractions if ex is not None]
    if not passes:
        return DomainExtraction(
            project_id="",
            concepts=[],
            relationships=[],
            error="no extractions to merge",
            degraded=False,
        )

    merged: dict[str, DomainConcept] = {}
    for extraction in passes:
        for concept in extraction.concepts:
            if concept.identity_key not in merged:
                merged[concept.identity_key] = concept
                continue
            merged[concept.identity_key] = _merge_concepts(
                merged[concept.identity_key], concept
            )

    relationships: dict[tuple[str, str, str], DomainRelationship] = {}
    for extraction in passes:
        for rel in extraction.relationships:
            key = (rel.relation_type, rel.source_identity, rel.target_identity)
            if key not in relationships:
                relationships[key] = rel

    errors = [
        ex.error for ex in passes if isinstance(ex.error, str) and ex.error
    ]
    return DomainExtraction(
        project_id=passes[0].project_id,
        concepts=list(merged.values()),
        relationships=list(relationships.values()),
        error="; ".join(errors) if errors else None,
        degraded=any(ex.degraded for ex in passes),
    )


def _unsupported_extraction(project_id: str, language: str | None) -> DomainExtraction:
    label = language or "none detected"
    return DomainExtraction(
        project_id=project_id,
        concepts=[],
        relationships=[],
        error=(
            f"unsupported language '{label}': domain extraction supports "
            "'php' only"
        ),
        degraded=False,
    )


def extract_for_workspace(
    project_id: str,
    workspace_path: str | Path,
    *,
    framework_plugin: Any = None,
    language: str | None = None,
    exclude: tuple[str, ...] | None = None,
) -> DomainExtraction:
    """Run the composed extraction for one workspace. Never raises.

    ``language`` is a hint; when omitted the primary language of
    ``structure_profile.language_census`` decides (falling back to
    ``detect_php`` when the census has no primary). v1 supports ``php`` only.
    For php workspaces the language pass runs first, then the framework pass
    when ``framework_plugin`` implements the optional
    ``extract_domain_concepts(workspace)`` method (duck-typed). When
    ``framework_plugin`` is None (the default — the auto-index/reconcile
    service paths pass nothing) a framework plugin owning the workspace is
    auto-discovered via ``_discover_framework_plugin`` and merged the same way;
    a bare-php or non-laravel tree simply skips the framework pass.
    """
    root = _resolve_root(workspace_path)
    if root is None:
        return DomainExtraction(
            project_id=project_id,
            concepts=[],
            relationships=[],
            error=f"workspace path is not a directory: {workspace_path}",
            degraded=True,
        )

    detected = language
    if not detected:
        detected = language_census(root).get("primary")
    if not detected and detect_php(root):
        detected = "php"
    if detected != "php":
        return _unsupported_extraction(project_id, detected)

    extractions = [
        extract_php_symbol_domain(project_id, str(root), exclude=exclude)
    ]

    plugin = framework_plugin
    if plugin is None:
        plugin = _discover_framework_plugin(root)

    if plugin is not None and hasattr(plugin, "extract_domain_concepts"):
        try:
            plugin_extraction = plugin.extract_domain_concepts(
                Workspace(str(root))
            )
        except Exception as exc:  # noqa: BLE001 — plugin pass must not raise
            plugin_extraction = DomainExtraction(
                project_id=project_id,
                concepts=[],
                relationships=[],
                error=f"framework domain extraction failed: {exc}",
                degraded=True,
            )
        if plugin_extraction is not None and plugin_extraction.project_id != project_id:
            plugin_extraction = replace(plugin_extraction, project_id=project_id)
        extractions.append(plugin_extraction)

    return merge_extractions(extractions)
