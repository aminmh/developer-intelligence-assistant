"""LLM-assisted framework detection — one-shot fallback probe.

Runs only when no static framework plugin detects the project confidently
(see project_profile.build_project_profile). It is a single stateless chat
completion: the model returns a JSON verdict about the uploaded project's
framework/version; nothing is retained afterwards (no session, no context) —
"memory is freed immediately" by construction.

Static plugin detection stays the source of truth. This probe exists for
projects the deterministic registry cannot classify (new/unknown markers). The
verdict may still name a REGISTERED framework (e.g. when manifests are weak);
build_project_profile then runs that plugin's analyze() as usual.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Well-known stack markers probed shallowly (depth <= 2, mirrors the static
# detect walk which tolerates uploads wrapped in a top-level folder).
_MANIFESTS = (
    "composer.json", "package.json", "pyproject.toml", "setup.py",
    "requirements.txt", "Pipfile", "Gemfile", "go.mod", "Cargo.toml",
    "pom.xml", "build.gradle", "project.json", "artisan", "manage.py",
    "wsgi.py", "asgi.py", "app.config.js", "angular.json", "vue.config.js",
    "next.config.js", "nuxt.config.js", "Dockerfile", "docker-compose.yml",
    "docker-compose.yaml", "Rakefile", "mix.exs", "pom.xml",
)

_SKIP_DIRS = {"vendor", "node_modules", ".git", "__MACOSX", "dist", "build",
              ".venv", "venv", "storage", "bootstrap/cache"}

_MAX_EXCERPT = 2400
_MAX_FILES = 12

_PROBE_SYSTEM = (
    "You are a project stack detector. Given a code-project excerpt (top-level "
    "file/dir listing plus manifest/entrypoint snippets), decide the primary "
    "application framework. Answer STRICTLY as a single JSON object — no prose, "
    "no markdown fences: {\"framework\": string|null, \"version\": string|null, "
    "\"confidence\": number 0..1, \"reason\": short string}. Return framework=null "
    "and low confidence when the evidence is ambiguous or absent. Never guess a "
    "framework from a generic dependency; base it on concrete markers in the excerpt."
)


def _collect_excerpt(workspace_path: str) -> str:
    """Build a compact evidence excerpt: listing + manifest/entry snippets."""
    root = Path(workspace_path)
    listing: list[str] = []
    manifests: list[str] = []
    try:
        for child in sorted(root.iterdir()):
            if child.is_dir():
                if child.name not in _SKIP_DIRS and not child.name.startswith("."):
                    listing.append(f"{child.name}/")
            else:
                listing.append(child.name)
        for base in [root, *sorted(p for p in root.iterdir() if p.is_dir())]:
            if not base.is_dir() or base.name in _SKIP_DIRS or base.name.startswith("."):
                continue
            for name in _MANIFESTS:
                f = base / name
                if f.is_file() and len(manifests) < _MAX_FILES:
                    try:
                        text = f.read_text(encoding="utf-8", errors="replace")[:600]
                        rel = str(f.relative_to(root))
                        manifests.append(f"--- {rel} ---\n{text}")
                    except OSError:
                        continue
    except OSError:
        return ""
    head = "Top-level entries: " + ", ".join(listing[:60])
    body = "\n\n".join(manifests)
    excerpt = f"{head}\n\n{body}"
    return excerpt[:_MAX_EXCERPT]


def _extract_json(content: str) -> dict | None:
    """Tolerantly parse the verdict — strip fences, take last {...} object."""
    cleaned = re.sub(r"```(?:json)?", "", content or "").strip()
    matches = list(re.finditer(r"\{[\s\S]*\}", cleaned))
    if not matches:
        return None
    for m in reversed(matches[-3:]):
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def llm_detect_framework(workspace_path: str) -> dict | None:
    """One-shot LLM probe for the project's framework.

    Returns {framework, version, confidence, reason, provider} when the model
    gives a usable, confident verdict; None otherwise. Never raises — a probe
    failure simply means "stay undetected" (the caller falls back to None).
    """
    try:
        from runtime.llm import complete_with_model
        from infra.settings import settings

        if not settings.llm_detect_enabled:
            return None
        excerpt = _collect_excerpt(workspace_path)
        if not excerpt.strip():
            return None
        prompt = (
            "Classify the framework of this uploaded project (excerpt below). "
            "Evidence first, verdict JSON last.\n\n" + excerpt
        )
        content, _model, provider = complete_with_model(
            prompt,
            system=_PROBE_SYSTEM,
            temperature=0.0,
            max_tokens=400,
            # remote preferred; complete_with_model silently falls back to the
            # local chat model when the remote is unconfigured/failing.
            provider="remote",
        )
        verdict = _extract_json(content)
        if not verdict:
            logger.info("LLM detect: unparseable verdict")
            return None
        framework = str(verdict.get("framework") or "").strip().lower()
        unknown = framework in ("", "null", "none", "unknown", "n/a")
        try:
            confidence = float(verdict.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if unknown or confidence < 0.5:
            logger.info("LLM detect: no confident verdict (%r, %.2f)", framework, confidence)
            return None
        version = verdict.get("version")
        return {
            "framework": framework,
            "version": str(version).strip() if version else None,
            "confidence": round(min(max(confidence, 0.0), 1.0), 3),
            "reason": str(verdict.get("reason") or "")[:200],
            "provider": provider,
        }
    except Exception:
        logger.exception("LLM detect probe failed")
        return None
