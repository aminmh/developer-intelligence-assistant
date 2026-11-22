"""Plugin registry with entry-point discovery (spec §6).

The core never hard-codes ``FRAMEWORKS = {"laravel": ...}``. Plugins register
themselves here — either explicitly (builtins) or via Python package entry
points (``ai_agent.languages`` / ``ai_agent.frameworks`` groups).

TODO(packaging): add a pyproject.toml exposing the builtin plugins as entry
points so external plugin packages can be ``pip install``-ed without touching
core code.
"""

from __future__ import annotations

import importlib.metadata as metadata
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent.core.profile import (
    FrameworkProfile,
    LanguageProfile,
    ProjectProfile,
    RuntimeProfile,
)
from agent.core.types import Project
from agent.interfaces.framework import FrameworkPlugin
from agent.interfaces.language import LanguagePlugin

if TYPE_CHECKING:
    from agent.core.capability import Capability
    from agent.core.revision import RevisionContext

logger = logging.getLogger(__name__)

LANGUAGE_ENTRY_POINT_GROUP = "ai_agent.languages"
FRAMEWORK_ENTRY_POINT_GROUP = "ai_agent.frameworks"
MIGRATION_ENTRY_POINT_GROUP = "ai_agent.migration_adapters"


def _load_entry_point_classes(
    group: str,
) -> dict[str, type]:
    """Return {name: class} for every entry point in ``group``.

    Deliberately does not instantiate — registration is the caller's job so
    constructors can receive injected dependencies (spec §21).
    """
    discovered: dict[str, type] = {}
    try:
        eps = metadata.entry_points(group=group)
    except Exception:  # noqa: BLE001 - metadata backend can fail in many ways
        return discovered
    for ep in eps:
        try:
            discovered[ep.name] = ep.load()
        except Exception:  # noqa: BLE001 - a broken plugin must not kill discovery
            logger.warning("skipping unloadable plugin entry point %r", ep.name)
            continue
    return discovered


class PluginRegistry:
    def __init__(self) -> None:
        self._languages: dict[str, LanguagePlugin] = {}
        self._frameworks: dict[str, FrameworkPlugin] = {}
        self._migration_adapters: dict[str, object] = {}

    # -- registration ------------------------------------------------------

    def register_language(self, plugin: LanguagePlugin) -> None:
        if not isinstance(plugin, LanguagePlugin):
            raise TypeError(
                f"{plugin!r} does not satisfy the LanguagePlugin protocol"
            )
        self._languages[plugin.name] = plugin

    def register_framework(self, plugin: FrameworkPlugin) -> None:
        if not isinstance(plugin, FrameworkPlugin):
            raise TypeError(
                f"{plugin!r} does not satisfy the FrameworkPlugin protocol"
            )
        self._frameworks[plugin.name] = plugin

    def discover_entry_points(self) -> None:
        """Instantiate + register everything advertised via entry points."""
        for name, cls in _load_entry_point_classes(LANGUAGE_ENTRY_POINT_GROUP).items():
            if name not in self._languages:
                self.register_language(cls())
        for name, cls in _load_entry_point_classes(FRAMEWORK_ENTRY_POINT_GROUP).items():
            if name not in self._frameworks:
                self.register_framework(cls())
        for name, cls in _load_entry_point_classes(MIGRATION_ENTRY_POINT_GROUP).items():
            if name not in self._migration_adapters:
                self.register_migration_adapter(name, cls())

    def register_migration_adapter(self, name: str, adapter: object) -> None:
        from agent.migration.contract import MigrationAdapter
        if not isinstance(adapter, MigrationAdapter):
            raise TypeError("Adapter must implement the migration adapter contract")
        self._migration_adapters[name] = adapter

    def migration_adapter(self, name: str):
        return self._migration_adapters.get(name)

    # -- access ------------------------------------------------------------

    @property
    def languages(self) -> dict[str, LanguagePlugin]:
        return dict(self._languages)

    @property
    def frameworks(self) -> dict[str, FrameworkPlugin]:
        return dict(self._frameworks)

    def language(self, name: str) -> LanguagePlugin | None:
        return self._languages.get(name)

    def framework(self, name: str) -> FrameworkPlugin | None:
        return self._frameworks.get(name)

    # -- detection (spec §7) -------------------------------------------------

    def detect(self, project: Project) -> ProjectProfile:
        profile = ProjectProfile(path=project.root)

        for lang in self._languages.values():
            result = lang.detect(project)
            if result.confidence > 0:
                profile.languages.append(
                    LanguageProfile(name=result.name, confidence=result.confidence)
                )

        for plugin in self._frameworks.values():
            result = plugin.detect(project)
            if result.confidence > 0:
                profile.frameworks.append(
                    FrameworkProfile(
                        name=result.name,
                        version=result.version,
                        confidence=result.confidence,
                        capabilities=tuple(getattr(plugin, "capabilities", ())),
                    )
                )

        profile.languages.sort(key=lambda l: l.confidence, reverse=True)
        profile.frameworks.sort(key=lambda f: f.confidence, reverse=True)

        # Runtime: the language the highest-confidence framework needs, else
        # the strongest detected language.
        if profile.frameworks:
            top_fw = profile.frameworks[0]
            fw_plugin = self._frameworks.get(top_fw.name)
            if fw_plugin is not None:
                lang_plugin = self._languages.get(fw_plugin.language)
                version = None
                if lang_plugin is not None:
                    det = lang_plugin.detect(project)
                    version = det.version
                profile.runtime = RuntimeProfile(name=fw_plugin.language, version=version)
        elif profile.languages:
            profile.runtime = RuntimeProfile(
                name=profile.languages[0].name, version=None
            )

        return profile

    # -- capability providers ----------------------------------------------------

    def collect(self, capability: "Capability", context: "RevisionContext",
                min_confidence: float = 0.5) -> "CapabilityCollection":
        """Records of one capability from every framework plugin detected in the revision.

        Checks call this instead of naming frameworks (decision:
        framework-knowledge-lives-in-plugins-over-language-facts). A detected framework
        that does not provide the capability, or fails to, is reported as a gap.
        """
        from agent.interfaces.capabilities import RECORD_TYPES, CapabilityProvider, CapabilityUnavailable

        project = context.project
        result = CapabilityCollection(capability=capability)
        allowed = RECORD_TYPES.get(capability, ())
        # Language plugins join only when they advertise the capability (a language's own tooling,
        # such as its test runner's configuration); one that does not is not a gap.
        languages = [plugin for plugin in self._languages.values()
                     if capability in tuple(getattr(plugin, "capabilities", ()) or ())]
        for plugin in [*self._frameworks.values(), *languages]:
            try:
                detection = plugin.detect(project)
            except Exception:  # noqa: BLE001 — a broken detector must not stop collection
                continue
            if detection.confidence < min_confidence:
                continue
            result.detected.append(plugin.name)
            advertised = capability in tuple(getattr(plugin, "capabilities", ()))
            if not advertised or not isinstance(plugin, CapabilityProvider):
                result.gaps.append({"framework": plugin.name, "capability": capability.value,
                                    "reason": f"The {plugin.name} plugin does not provide {capability.value}."})
                continue
            try:
                records = list(plugin.provide(capability, context) or [])
            except CapabilityUnavailable as exc:
                result.gaps.append({"framework": plugin.name, "capability": capability.value,
                                    "reason": str(exc)[:300] or f"{plugin.name} could not provide {capability.value}."})
                continue
            except Exception as exc:  # noqa: BLE001 — a failing provider is a gap, never fatal
                logger.warning("capability provider %s failed for %s: %s", plugin.name, capability.value, exc)
                result.gaps.append({"framework": plugin.name, "capability": capability.value,
                                    "reason": f"The {plugin.name} plugin failed while providing {capability.value}."})
                continue
            bad = [r for r in records if allowed and not isinstance(r, allowed)]
            if bad:
                result.gaps.append({"framework": plugin.name, "capability": capability.value,
                                    "reason": f"The {plugin.name} plugin returned records of the wrong type."})
                continue
            result.providers.append(plugin.name)
            result.records.extend(records)
        # A framework that does not provide a capability its language plugin provided is not a gap.
        covered = {plugin.name for plugin in languages if plugin.name in result.providers}
        result.gaps = [gap for gap in result.gaps
                       if not (getattr(self._frameworks.get(gap["framework"]), "language", None) in covered
                               and gap["reason"].endswith(f"does not provide {capability.value}."))]
        return result


@dataclass
class CapabilityCollection:
    """What ``PluginRegistry.collect`` found for one capability."""

    capability: "Capability"
    records: list = field(default_factory=list)
    providers: list[str] = field(default_factory=list)   # detected plugins that provided records
    detected: list[str] = field(default_factory=list)    # all detected framework plugins
    gaps: list[dict] = field(default_factory=list)       # detected plugins that could not provide

    @property
    def status(self) -> str:
        """complete when every detected framework provided; partial with gaps; unavailable when none did."""
        if not self.detected or not self.providers:
            return "unavailable"
        return "partial" if self.gaps else "complete"

    def coverage(self) -> dict:
        return {"capability": self.capability.value, "status": self.status, "providers": self.providers,
                "detected_frameworks": self.detected, "gaps": self.gaps, "records": len(self.records)}
