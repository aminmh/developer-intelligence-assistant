"""Project-sense profile — detect the uploaded project's framework and extract
the structured facts (routes, schema, models, controllers, wiring, behaviors,
frontend, errors, packages) that make answers aware of the actual project.

Pipeline: static registry detection (language + framework plugins) →
framework plugin ``analyze()`` on the extracted workspace → compact
serializable profile (canonical keys only — see project_knowledge.py contract).

When NO static plugin detects the project confidently, an optional one-shot
LLM probe (``llm_detect_framework``) classifies it. The verdict may name a
registered framework — that plugin's ``analyze()`` then runs as usual — or an
unknown one, in which case a thin identity profile is returned (honest
label, no fabricated structure). Nothing Laravel-specific is hard-coded here.
"""

from __future__ import annotations

from typing import Any

from agent.core.registry import PluginRegistry
from agent.core.types import Project
from agent.core.workspace import Workspace
from agent.plugins import register_builtins

_DETECTION_THRESHOLD = 0.6

# Canonical profile keys filled from a plugin's AnalysisResult.data.
_CANONICAL_DATA_KEYS = (
    "routes", "models", "schema", "config", "controllers", "wiring",
    "behaviors", "frontend", "errors", "packages",
)


def _analyze_profile(
    registry: PluginRegistry,
    workspace_path: str,
    framework_name: str,
    framework_version: str | None,
    confidence: float,
) -> dict[str, Any] | None:
    """Run the framework plugin's analyze() and flatten to the canonical shape."""
    plugin = registry.framework(framework_name)
    if plugin is None or not hasattr(plugin, "analyze"):
        return None
    analysis = plugin.analyze(Workspace(workspace_path))
    data = analysis.data or {}
    profile: dict[str, Any] = {
        "framework": framework_name,
        "version": framework_version,
        "confidence": round(confidence, 3),
        "language": getattr(plugin, "language", None),
        "summary": analysis.summary,
        **{key: data.get(key, [] if key != "config" else {}) for key in _CANONICAL_DATA_KEYS},
    }
    return profile


def build_project_profile(workspace_path: str) -> dict[str, Any] | None:
    """Detect + analyze the project at ``workspace_path``.

    Returns a profile dict, or None when no framework is detected confidently
    (static or LLM). Never raises: failures degrade to None so the pipeline
    continues.
    """
    import os

    if not workspace_path or not os.path.isdir(workspace_path):
        return None
    try:
        registry = PluginRegistry()
        register_builtins(registry)
        project = Project.from_path(workspace_path)
        detected = registry.detect(project)

        # highest-confidence framework first
        frameworks = sorted(detected.frameworks, key=lambda f: f.confidence, reverse=True)
        if frameworks and frameworks[0].confidence >= _DETECTION_THRESHOLD:
            top = frameworks[0]
            profile = _analyze_profile(
                registry, workspace_path, top.name, top.version, top.confidence
            )
            return profile

        # Static detection missed — one-shot LLM fallback probe.
        try:
            from projects.llm_detect import llm_detect_framework

            verdict = llm_detect_framework(workspace_path)
        except Exception:  # noqa: BLE001 — fallback is best-effort
            verdict = None
        if not verdict:
            return None

        name = str(verdict["framework"]).strip().lower()
        # Known framework → full plugin analysis. Unknown → thin identity only
        # (no structure to extract honestly without a plugin).
        if registry.framework(name) is not None:
            profile = _analyze_profile(
                registry, workspace_path, name, verdict.get("version"),
                float(verdict.get("confidence", 0.0)),
            )
            if profile is not None:
                profile["summary"] = (
                    f"{profile.get('summary') or ''} (LLM-assisted detection: "
                    f"{verdict.get('reason') or ''})".strip()
                )
                return profile

        return {
            "framework": name,
            "version": verdict.get("version"),
            "confidence": float(verdict.get("confidence", 0.0)),
            "language": None,
            "summary": (
                f"Detected by LLM probe: {verdict.get('reason') or 'no detail'} "
                f"(no framework plugin registered for {name!r} — structure "
                "analysis unavailable)"
            ),
            **{key: [] if key != "config" else {} for key in _CANONICAL_DATA_KEYS},
        }
    except Exception:  # noqa: BLE001 — profile is best-effort
        return None
