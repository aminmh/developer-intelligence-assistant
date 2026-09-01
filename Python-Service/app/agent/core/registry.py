"""Plugin registry with entry-point discovery (spec §6).

The core never hard-codes ``FRAMEWORKS = {"laravel": ...}``. Plugins register
themselves here — either explicitly (builtins) or via Python package entry
points (``ai_agent.languages`` / ``ai_agent.frameworks`` groups).

TODO(packaging): add a pyproject.toml exposing the builtin plugins as entry
points so external plugin packages can be ``pip install``-ed without touching
core code.
"""

from __future__ import annotations

import importlib.metadata
import logging

from agent.core.profile import (
    FrameworkProfile,
    LanguageProfile,
    ProjectProfile,
    RuntimeProfile,
)
from agent.core.types import Project
from agent.interfaces.framework import FrameworkPlugin
from agent.interfaces.language import LanguagePlugin

logger = logging.getLogger(__name__)

LANGUAGE_ENTRY_POINT_GROUP = "ai_agent.languages"
FRAMEWORK_ENTRY_POINT_GROUP = "ai_agent.frameworks"


def _load_entry_point_classes(
    group: str,
) -> dict[str, type]:
    """Return {name: class} for every entry point in ``group``.

    Deliberately does not instantiate — registration is the caller's job so
    constructors can receive injected dependencies (spec §21).
    """
    discovered: dict[str, type] = {}
    try:
        eps = importlib.metadata.entry_points(group=group)
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
