"""Structure profile — deterministic, offline, multi-language project census.

A pure-stdlib sibling to ``project_profile.py`` that answers "what is this
workspace shaped like?" for ANY uploaded project — not just the ones a static
framework plugin knows about. It produces one canonical dict describing:

  * language census  (file counts per language via extension suffixes)
  * framework guess  (laravel via the agent plugin registry; python/js/java/…
    via a tiny, transparent manifest/import marker registry)
  * database hint    (laravel ``config/database.php`` default connection
    overridden by an active ``.env``/``.env.example`` assignment, or a shallow
    scan of ``.env*``/settings files for URL/DSN markers)
  * architecture     (top-level dirs, entrypoint files, manifest names, a
    dependency-map count when one can be derived)

Philosophy — three rules:

  1. DETERMINISTIC. Same path in, same dict out. Directory traversal is sorted,
     every regex is anchored and cheap, nothing depends on wall-clock time or
     hash order. This module is safe to call repeatedly and to cache by path.
  2. OFFLINE. No network, no docker, no subprocess. Filesystem reads are the
     only I/O and they are all bounded — text reads happen through a capped
     binary handle (never a whole-file slurp) with ``errors="replace"`` so one
     unreadable or oversized file never derails the census. Framework-plugin
     analysis is invoked with ``runtime_routes=False`` so even a plugin that
     could shell out to ``php artisan`` stays purely static here.
  3. HONEST ABSENCE. When nothing supports a claim the value is ``None`` /
     empty / ``"not determined"`` — never a fabricated framework, database or
     dependency. Commented-out ``.env`` lines never classify a database, and
     ``structure_source`` records whether the facts came from a real framework
     plugin (``"plugin"``) or from the generic marker path (``"generic"``).

Every public function is total: it never raises. Failures degrade to the
canonical empty shape (or, for a mid-build failure on a readable tree, to an
``analysis degraded: …`` summary with an ``error`` key) so the caller's
pipeline keeps running.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

# --- module constants ----------------------------------------------------------

# Directories never walked (vendored/build/test noise), matched
# case-insensitively. Aligned with projects/dependencies.py and extended with
# our own Laravel/dist/tooling entries. ``storage/framework`` is a nested
# Laravel path and is handled separately by the walk pruner.
_SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        "vendor",
        "node_modules",
        ".git",
        ".idea",
        ".vscode",
        "dist",
        "build",
        "coverage",
        ".venv",
        "venv",
        "__pycache__",
        "target",
        ".next",
        "site-packages",
        ".tox",
    }
)
_SKIP_DIR_NAMES_LOWER: frozenset[str] = frozenset(n.lower() for n in _SKIP_DIR_NAMES)
_LARAVEL_SKIP_PATH = "storage/framework"

# Language -> recognised file suffixes (lowercase, with leading dot).
LANG_EXTENSIONS: dict[str, frozenset[str]] = {
    "php": frozenset({".php", ".php3", ".php4", ".php5", ".phtml"}),
    "python": frozenset({".py", ".pyw"}),
    "javascript": frozenset({".js", ".mjs", ".cjs", ".jsx"}),
    "typescript": frozenset({".ts", ".tsx"}),
    "java": frozenset({".java"}),
    "go": frozenset({".go"}),
    "ruby": frozenset({".rb"}),
    "rust": frozenset({".rs"}),
    "csharp": frozenset({".cs"}),
    "cpp": frozenset({".cpp", ".cc", ".hpp"}),
    "c": frozenset({".c", ".h"}),
}

# Suffix -> language lookup built once (suffixes are lowercased by Path).
_EXT_TO_LANG: dict[str, str] = {
    ext: lang for lang, exts in LANG_EXTENSIONS.items() for ext in exts
}

# Well-known manifests collected during the census (bounded sample).
_MANIFEST_NAMES: frozenset[str] = frozenset(
    {
        "composer.json",
        "composer.lock",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "requirements.txt",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "Pipfile.lock",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "go.mod",
        "Gemfile",
        "Cargo.toml",
    }
)
_MAX_MANIFESTS = 12

# Extra non-extension files that framework/language detection keys on. Kept in
# the pruned plugin ``Project`` so ``has_any`` stays truthful (e.g. artisan
# raises Laravel confidence) without ever listing vendored trees.
_PLUGIN_FILE_NAMES: frozenset[str] = frozenset({"artisan"})

# Entrypoint-looking files probed shallowly (file presence only).
_ENTRYPOINT_NAMES: frozenset[str] = frozenset(
    {
        "artisan",
        "manage.py",
        "Manage.py",
        "main.py",
        "app.py",
        "server.py",
        "cli.py",
        "wsgi.py",
        "asgi.py",
        "main.go",
        "Application.java",
        "Program.cs",
        "index.js",
        "index.ts",
    }
)

# Files shallow-scanned for DB markers (never the whole tree).
_DB_CONFIG_FILES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.example",
        ".env.sample",
        "pyproject.toml",
        "settings.py",
        "application.properties",
        "application.yml",
        "application.yaml",
    }
)

_DB_DRIVER_NAMES = {
    "sqlite": "SQLite",
    "mysql": "MySQL",
    "pgsql": "PostgreSQL",
    "sqlsrv": "SQL Server",
    "mongodb": "MongoDB",
}
_DB_REDIS = {"redis"}
# substring -> human database name (checked in priority order, redis last).
_DB_MARKER_ORDER = (
    ("sqlserver", "SQL Server"),
    ("sqlsrv", "SQL Server"),
    ("jdbc:sqlserver", "SQL Server"),
    ("mongodb", "MongoDB"),
    ("mongo://", "MongoDB"),
    ("pgsql", "PostgreSQL"),
    ("postgres", "PostgreSQL"),
    ("mariadb", "MySQL"),
    ("mysql", "MySQL"),
    ("sqlite", "SQLite"),
    ("firebase", "Firebase"),
    ("redis", "redis"),
)

_LARAVEL_REQUIRE = "laravel/framework"
_PLUGIN_THRESHOLD = 0.6

# Canonical keys extracted from a framework plugin's AnalysisResult.data.
_CANONICAL_DATA_KEYS = (
    "routes", "models", "schema", "config", "controllers", "wiring",
    "behaviors", "frontend", "errors", "packages",
)

_VERSION_CONSTRAINT_RE = re.compile(
    r"(?:===|==|>=|<=|~=|~|\^)\s*v?\s*(\d+(?:\.\d+){0,3})"
)
_VERSION_BARE_RE = re.compile(r"(\d+\.\d+(?:\.\d+){0,3})")
_LARAVEL_DEFAULT_RE = re.compile(
    r"""['"]default['"]\s*=>\s*env\(\s*['"]DB_CONNECTION['"]\s*,\s*['"](?P<conn>\w+)['"]"""
)
_DRIVER_RE = re.compile(r"""['"]driver['"]\s*=>\s*['"](?P<driver>\w+)['"]""")
_POM_VERSION_RE = re.compile(r"<version>\s*(.*?)</version>")


class _FrameworkMarker:
    """One declarative framework hint for the marker registry.

    ``needles`` use word boundaries so ``react`` does not match ``react-dom``,
    ``express`` does not match ``express-handlebars``, and ``django`` does not
    match ``django-cors-headers``. A needle that itself contains ``-`` (e.g.
    ``spring-boot``) is allowed to be a prefix of a longer token so
    ``spring-boot-starter-web`` still counts.
    """

    __slots__ = (
        "confidence",
        "imports",
        "language",
        "manifests",
        "name",
        "needles",
        "version_across",
    )

    def __init__(
        self,
        name: str,
        language: str,
        manifests: tuple[str, ...] = (),
        needles: tuple[str, ...] = (),
        *,
        confidence: float = 0.9,
        version_across: bool = False,
        imports: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.language = language
        self.manifests = manifests
        self.needles = needles
        self.confidence = confidence
        self.version_across = version_across
        self.imports = imports


# Registration order IS priority order for equal-confidence hits: the most
# opinionated framework for an ecosystem comes first (Django before Flask,
# Next.js before React, Spring Boot before plain Spring).
_FRAMEWORK_MARKERS: tuple[_FrameworkMarker, ...] = (
    _FrameworkMarker(
        "Django", "python",
        manifests=("pyproject.toml", "requirements.txt", "setup.py", "Pipfile"),
        needles=("django",),
    ),
    _FrameworkMarker(
        "FastAPI", "python",
        manifests=("pyproject.toml", "requirements.txt", "setup.py", "Pipfile"),
        needles=("fastapi",),
    ),
    _FrameworkMarker(
        "Flask", "python",
        manifests=("pyproject.toml", "requirements.txt", "setup.py", "Pipfile"),
        needles=("flask",),
    ),
    _FrameworkMarker(
        "Next.js", "javascript",
        manifests=("package.json",),
        needles=("next",),
    ),
    _FrameworkMarker(
        "Nuxt", "javascript",
        manifests=("package.json",),
        needles=("nuxt",),
    ),
    _FrameworkMarker(
        "NestJS", "javascript",
        manifests=("package.json",),
        needles=("@nestjs/core",),
    ),
    _FrameworkMarker(
        "Express", "javascript",
        manifests=("package.json",),
        needles=("express",),
    ),
    _FrameworkMarker(
        "React", "javascript",
        manifests=("package.json",),
        needles=("react",),
    ),
    _FrameworkMarker(
        "Vue", "javascript",
        manifests=("package.json",),
        needles=("vue",),
    ),
    _FrameworkMarker(
        "Spring Boot", "java",
        manifests=("pom.xml", "build.gradle", "build.gradle.kts"),
        needles=("spring-boot", "org.springframework.boot"),
    ),
    _FrameworkMarker(
        "Spring", "java",
        manifests=("pom.xml", "build.gradle", "build.gradle.kts"),
        needles=("org.springframework",),
    ),
    _FrameworkMarker(
        "Go module", "go",
        manifests=("go.mod",),
        needles=("module",),
        version_across=True,
    ),
    _FrameworkMarker(
        "Rails", "ruby",
        manifests=("Gemfile",),
        needles=("rails",),
    ),
)

# (order, marker) pairs for the python import-scan fallback (confidence 0.7).
# Django is deliberately checked before FastAPI/Flask so an app that imports
# several web frameworks is labelled by the most opinionated one. Each entry is
# line-anchored: ``# import django`` comments never count (see
# ``_module_import_re``).
_PY_IMPORT_MARKERS: tuple[tuple[int, _FrameworkMarker], ...] = (
    (
        0,
        _FrameworkMarker(
            "Django", "python", imports=("django",), confidence=0.7
        ),
    ),
    (
        1,
        _FrameworkMarker(
            "FastAPI", "python", imports=("fastapi",), confidence=0.7
        ),
    ),
    (
        2,
        _FrameworkMarker(
            "Flask", "python", imports=("flask",), confidence=0.7
        ),
    ),
)

_DEFAULT_DETECTION = {
    "framework": None,
    "version": None,
    "confidence": 0.0,
    "source": None,
}


# --- small helpers --------------------------------------------------------------

def _resolve_root(workspace_path: str | Path | None) -> Path | None:
    """Resolve ``workspace_path`` to an existing directory, else None."""
    try:
        if workspace_path is None:
            return None
        path = Path(workspace_path).expanduser()
        return path if path.is_dir() else None
    except (OSError, TypeError, ValueError):
        return None


def _rel_posix(root: Path, dirpath: str) -> str:
    """Root-relative POSIX path of a walked dir ('' for the root itself)."""
    try:
        rel = os.path.relpath(str(dirpath), str(root))
    except ValueError:
        return ""
    if rel == ".":
        return ""
    return rel.replace(os.sep, "/")


def _skippable(name: str, parent_rel: str) -> bool:
    """True when a directory ``name`` under ``parent_rel`` should be pruned.

    Directory names are matched case-insensitively (``Vendor`` == ``vendor``);
    the nested Laravel path ``storage/framework`` is matched on the full
    relative path so a top-level ``storage/`` directory is never pruned.
    """
    if name.lower() in _SKIP_DIR_NAMES_LOWER:
        return True
    rel = f"{parent_rel}/{name}" if parent_rel else name
    rel_lower = rel.lower()
    return rel_lower == _LARAVEL_SKIP_PATH or rel_lower.endswith(
        "/" + _LARAVEL_SKIP_PATH
    )


def _prune(dirnames: list[str], parent_rel: str) -> None:
    """Filter ``dirnames`` in place, sorted, skip dirs removed."""
    dirnames[:] = sorted(d for d in dirnames if not _skippable(d, parent_rel))


def _read_text_bounded(path: Path, limit: int = 262_144) -> str:
    """Read ``path`` as utf-8 through a capped binary handle.

    Real I/O is bounded: at most ``limit + 1`` bytes leave the filesystem and
    the returned string never exceeds ``limit`` characters. Undecodable bytes
    are replaced rather than raising.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit + 1)
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")[:limit]


def _pruned_files(
    root: Path, max_depth: int | None = None
) -> list[tuple[str, Path]]:
    """Return ``(rel, path)`` for every non-skipped file under ``root``.

    Every directory level is sorted and skip directories are pruned. When
    ``max_depth`` is set, directories at that depth are still scanned for files
    but their children are not descended into. Never raises.
    """
    out: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        parent_rel = _rel_posix(root, dirpath)
        _prune(dirnames, parent_rel)
        depth = len(parent_rel.split("/")) if parent_rel else 0
        if max_depth is not None and depth >= max_depth:
            dirnames[:] = []
        for name in sorted(filenames):
            rel = f"{parent_rel}/{name}" if parent_rel else name
            out.append((rel, Path(dirpath) / name))
    return out


def _is_plugin_relevant_file(path: Path) -> bool:
    """True when a file matters to registry detection (extension/manifest)."""
    name = path.name
    return (
        path.suffix.lower() in _EXT_TO_LANG
        or name in _MANIFEST_NAMES
        or name in _PLUGIN_FILE_NAMES
    )


def _report(
    progress: Callable[[int, str], None] | None, pct: int, label: str
) -> None:
    if progress is None:
        return
    try:
        progress(pct, label)
    except Exception:  # noqa: BLE001 — progress reporting must never raise
        return


def _empty_profile() -> dict[str, Any]:
    """Canonical empty shape — every key build_structure_profile guarantees."""
    return {
        "framework": None,
        "version": None,
        "confidence": 0.0,
        "language": None,
        "database": None,
        "database_confidence": "low",
        "evidence": None,
        "languages": [],
        "total_files": 0,
        "top_level_dirs": [],
        "entrypoints": [],
        "manifests": [],
        "dependency_count": None,
        "structure_source": "generic",
        "error": None,
        "summary": "No readable workspace",
    }


def _degraded_profile(reason: object) -> dict[str, Any]:
    """Honest shape for a mid-build failure on a readable tree."""
    message = str(reason)[:160] or "unknown failure"
    profile = _empty_profile()
    profile["error"] = message
    profile["summary"] = f"analysis degraded: {message}"
    return profile


def _needle_patterns(needles: tuple[str, ...]) -> list[re.Pattern[str]]:
    """Compile word-boundary needle regexes (see ``_FrameworkMarker`` docs)."""
    patterns: list[re.Pattern[str]] = []
    for needle in needles:
        esc = re.escape(needle)
        if "-" in needle:
            # multi-token needle may prefix a longer token (spring-boot-starter)
            pattern = rf"(?<![A-Za-z0-9_@-]){esc}(?![A-Za-z0-9])"
        else:
            pattern = rf"(?<![A-Za-z0-9_@-]){esc}(?![A-Za-z0-9_-])"
        patterns.append(re.compile(pattern))
    return patterns


def _ver_from(
    text: str,
    regexes: tuple[re.Pattern[str], ...] | None = None,
) -> str | None:
    """Return the first semver-ish version group in ``text``, else None."""
    for regex in regexes or (_VERSION_CONSTRAINT_RE, _VERSION_BARE_RE):
        match = regex.search(text)
        if match:
            return match.group(1)
    return None


def _quoted_dependency_version(text: str, needle: str) -> str | None:
    """Version of a quoted ``"needle": "1.2.3"`` dependency key (package.json)."""
    match = re.search(r'"' + re.escape(needle) + r'"\s*:\s*"([^"]+)"', text)
    if match is None:
        return None
    return _ver_from(match.group(1))


def _pom_version_after(text: str, needle: str) -> str | None:
    """First ``<version>`` near a needle line (pom.xml starter artifacts)."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if needle not in line:
            continue
        for candidate in lines[index : index + 12]:
            match = _POM_VERSION_RE.search(candidate)
            if match:
                version = _ver_from(match.group(1).strip())
                if version:
                    return version
    return None


def _dependency_version(text: str, rel: str, needles: tuple[str, ...]) -> str | None:
    """Version association for a scaffold hit that had no version on its line.

    package.json scripts reference e.g. ``"next dev"`` while the real version
    lives under the ``"next"`` dependency key; pom.xml starter artifacts carry
    their ``<version>`` after the artifact element. Returns None when nothing
    supports the association (documented as an acceptable unknown).
    """
    rel_name = Path(rel).name
    if rel_name not in ("package.json", "pom.xml"):
        return None
    for needle in needles:
        if rel_name == "package.json":
            version = _quoted_dependency_version(text, needle)
        else:
            version = _pom_version_after(text, needle)
        if version:
            return version
    return None


def _marker_manifest_match(
    marker: _FrameworkMarker, root: Path, rel: str
) -> tuple[str, str | None] | None:
    """Return ``(needle_line, version)`` when manifest ``rel`` hits ``marker``."""
    text = _read_text_bounded(root / rel)
    if not text:
        return None
    patterns = _needle_patterns(marker.needles)
    if marker.version_across:
        # go.mod has no dependency line — presence + `go 1.2x` directive.
        if not any(p.search(text) for p in patterns):
            return None
        return "", _ver_from(text)
    for line in text.splitlines():
        for pattern in patterns:
            if pattern.search(line):
                version = _ver_from(line)
                if version is None:
                    version = _dependency_version(text, rel, marker.needles)
                return line, version
    return None


def _module_import_re(modules: tuple[str, ...]) -> re.Pattern[str]:
    """Line-anchored import regex: ``import x`` / ``from x`` at line start.

    ``# import django`` and ``import django_core`` must not match ``django``.
    """
    alternation = "|".join(re.escape(m) for m in modules)
    return re.compile(rf"^\s*(?:import|from)\s+(?:{alternation})\b", re.MULTILINE)


def _marker_import_match(marker: _FrameworkMarker, text: str) -> bool:
    return _module_import_re(marker.imports).search(text) is not None


def _candidate_best(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    candidates.sort(key=lambda c: (-c["confidence"], c["_order"]))
    best = candidates[0]
    return {
        "framework": best["framework"],
        "version": best["version"],
        "confidence": round(float(best["confidence"]), 3),
        "source": "markers",
    }


def _safe_manifest_rels(rels: list[str] | tuple[str, ...] | None) -> list[str]:
    """Sanitize caller-supplied manifest rels (reject absolute / ``..``)."""
    safe: list[str] = []
    for rel in rels or []:
        if not isinstance(rel, str):
            continue
        posix = rel.replace("\\", "/")
        if not posix or posix.startswith("/") or re.match(r"^[A-Za-z]:", posix):
            continue
        parts = [p for p in posix.split("/") if p not in ("", ".")]
        if not parts or any(p == ".." for p in parts):
            continue
        safe.append("/".join(parts))
    return safe


# --- census walk ----------------------------------------------------------------

def _collect(root: Path) -> dict[str, Any]:
    """One pruned census: languages, total file count, manifests, plugin files."""
    file_count = 0
    counts: dict[str, int] = {}
    manifest_hits: list[tuple[int, str]] = []
    plugin_paths: list[Path] = []
    for rel, full in _pruned_files(root):
        file_count += 1
        name = full.name
        lang = _EXT_TO_LANG.get(full.suffix.lower())
        if lang is not None:
            counts[lang] = counts.get(lang, 0) + 1
        if name in _MANIFEST_NAMES:
            manifest_hits.append((len(rel.split("/")), rel))
        if _is_plugin_relevant_file(full):
            plugin_paths.append(full)

    languages = [
        {"language": lang, "files": count}
        for lang, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    manifest_hits.sort(key=lambda item: (item[0], item[1]))
    return {
        "languages": languages,
        "total_files": file_count,
        "primary": languages[0]["language"] if languages else None,
        "manifests": [rel for _, rel in manifest_hits],
        "plugin_files": tuple(sorted(set(plugin_paths))),
    }


# --- public step functions ------------------------------------------------------

def language_census(workspace_path: str | Path) -> dict[str, Any]:
    """Walk the tree once and count languages + manifests.

    Skips ``_SKIP_DIR_NAMES`` (case-insensitively), sorts every level, and
    never follows the network. ``total_files`` counts EVERY non-skipped file
    (source, manifests, READMEs, assets — truthful), while ``languages`` keeps
    per-language counts separate. Returns
    ``{"languages": [...], "total_files", "primary", "manifests"}``.
    """
    root = _resolve_root(workspace_path)
    empty = {
        "languages": [],
        "total_files": 0,
        "primary": None,
        "manifests": [],
    }
    if root is None:
        return empty
    try:
        census = _collect(root)
        return {
            "languages": census["languages"],
            "total_files": census["total_files"],
            "primary": census["primary"],
            "manifests": census["manifests"][:_MAX_MANIFESTS],
        }
    except OSError:
        return empty


def detect_framework(
    workspace_path: str | Path,
    primary_language: str | None,
    manifests: list[str] | None,
) -> dict[str, Any]:
    """Detect the framework for ``workspace_path``.

    Laravel (composer.json requires laravel/framework, or an artisan file) is
    delegated to the agent plugin registry; everything else falls to the static
    ``_FRAMEWORK_MARKERS``. Caller-supplied manifest rels are sanitized so
    absolute paths or ``..`` escapes can never be joined under the root. Never
    raises — returns the canonical detection dict (framework/version/
    confidence/source) with ``None`` values when nothing is detected.
    """
    root = _resolve_root(workspace_path)
    try:
        if root is None:
            return dict(_DEFAULT_DETECTION)
        rels = _safe_manifest_rels(manifests)
        if _laravel_marked(root, rels):
            detection = _plugin_detect(root)[0]
            if detection is not None:
                return detection
        return _marker_detect(root, rels, primary_language)
    except Exception:  # noqa: BLE001 — detection is best-effort
        return dict(_DEFAULT_DETECTION)


def infer_database(
    workspace_path: str | Path,
    primary_language: str | None,
    framework: str | None,
) -> dict[str, Any]:
    """Infer the primary database from deterministic, read-only markers.

    Laravel apps are read through ``config/database.php``'s ``default``
    connection, with an active root ``.env``/``.env.example`` assignment taking
    precedence; other ecosystems are read from shallow ``.env*``/settings files.
    Only active (non-comment) assignment lines ever classify. Returns
    ``{"database", "confidence", "evidence"}`` — ``database`` is None (with
    ``confidence: "low"``) when nothing supports a claim.
    """
    root = _resolve_root(workspace_path)
    if root is None:
        return {"database": None, "confidence": "low", "evidence": None}
    try:
        if str(framework or "").lower() == "laravel":
            return _db_laravel(root)
        return _db_generic(root)
    except Exception:  # noqa: BLE001 — inference is best-effort
        return {"database": None, "confidence": "low", "evidence": None}


def architecture_summary(
    workspace_path: str | Path,
    census: dict[str, Any] | None,
    framework: str | None,
    primary_language: str | None,
    manifests: list[str] | None,
) -> dict[str, Any]:
    """Top-level layout truth: dirs, entrypoints, manifests, dependency count."""
    root = _resolve_root(workspace_path)
    empty = {
        "top_level_dirs": [],
        "entrypoints": [],
        "manifests": [],
        "dependency_count": None,
    }
    if root is None:
        return empty

    try:
        rels = sorted(
            dict.fromkeys(_safe_manifest_rels(census.get("manifests") if census else []))
        )
        if manifests:
            rels = sorted(
                dict.fromkeys(rels + _safe_manifest_rels(list(manifests)))
            )
        return {
            "top_level_dirs": _top_level_dirs(root),
            "entrypoints": _entrypoints(root),
            "manifests": rels[:_MAX_MANIFESTS],
            "dependency_count": _dependency_count(root),
        }
    except Exception:  # noqa: BLE001 — summary is best-effort
        return empty


def build_structure_profile(
    workspace_path: str | Path,
    progress: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    """Public entry — full canonical structure profile. Never raises.

    When a framework plugin confidently owns the workspace (laravel today) the
    plugin's canonical keys (routes/models/schema/…) stay authoritative and the
    generic facts (census/database/architecture) are layered on top. Otherwise a
    truthful generic profile is built from the marker path. Plugin analysis runs
    with ``runtime_routes=False`` so the offline contract is preserved.
    """
    _report(progress, 0, "start")
    root = _resolve_root(workspace_path)
    if root is None:
        _report(progress, 100, "done")
        return _empty_profile()

    try:
        census = _collect(root)
        _report(progress, 20, "language census")
        primary = census["primary"]
        rels = census["manifests"][:_MAX_MANIFESTS]

        plugin_profile: dict[str, Any] | None = None
        detection: dict[str, Any] | None = None
        if _laravel_marked(root, rels):
            det, registry, top = _plugin_detect(root, census["plugin_files"])
            if det is not None and top is not None and registry is not None:
                detection = det
                plugin_profile = _plugin_analyze(registry, top, root)

        if plugin_profile is not None:
            framework = str(detection.get("framework") or "laravel")
            version = detection.get("version")
            confidence = float(detection.get("confidence") or 0.0)
            structure_source = "plugin"
        else:
            detection = _marker_detect(root, rels, primary)
            framework = detection["framework"]
            version = detection["version"]
            confidence = float(detection["confidence"] or 0.0)
            structure_source = "generic"
        _report(progress, 35, "framework detection")

        db = infer_database(root, primary, framework)
        _report(progress, 55, "database inference")

        arch = architecture_summary(root, census, framework, primary, rels)
        _report(progress, 80, "architecture")

        if plugin_profile is not None:
            profile: dict[str, Any] = dict(plugin_profile)
            profile["language"] = primary or profile.get("language")
        else:
            profile = {
                "framework": framework,
                "version": version,
                "confidence": round(confidence, 3),
                "language": primary,
            }

        profile.update(
            {
                "database": db["database"],
                "database_confidence": db["confidence"],
                "evidence": db["evidence"],
                "languages": census["languages"],
                "total_files": census["total_files"],
                "top_level_dirs": arch["top_level_dirs"],
                "entrypoints": arch["entrypoints"],
                "manifests": arch["manifests"],
                "dependency_count": arch["dependency_count"],
                "structure_source": structure_source,
                "error": None,
            }
        )

        if plugin_profile is not None:
            base = profile.get("summary") or f"{framework} project"
            profile["summary"] = f"{base}; DB: {db['database'] or 'not determined'}"
        else:
            n = profile["total_files"]
            label = framework or "Unclassified"
            profile["summary"] = (
                f"{label} project; {primary or '?'}; {n} file(s); "
                f"DB {db['database'] or 'not determined'}"
            )
        _report(progress, 100, "done")
        return profile
    except Exception as exc:  # noqa: BLE001 — profile is best-effort
        _report(progress, 100, "done")
        return _degraded_profile(exc)


# --- plugin path (agent registry) ------------------------------------------------

def _laravel_marked(root: Path, manifests: list[str]) -> bool:
    """Cheap pre-check: composer.json w/ laravel/framework or artisan present."""
    try:
        for rel in _safe_manifest_rels(manifests):
            if Path(rel).name != "composer.json":
                continue
            text = _read_text_bounded(root / rel, limit=131_072)
            if _LARAVEL_REQUIRE in text:
                return True
        for candidate in (root, *sorted(root.iterdir())):
            if not candidate.is_dir():
                continue
            if (candidate / "artisan").is_file():
                return True
    except OSError:
        return False
    return False


def _plugin_detect(
    root: Path,
    plugin_files: tuple[Path, ...] | list[Path] | None = None,
) -> tuple[dict[str, Any] | None, Any | None, Any | None]:
    """Run the agent plugin registry's detection on ``root``.

    ``Project`` is built from the pruned census's relevant files (never
    ``Project.from_path``), so vendored ``vendor/``/``node_modules/`` trees are
    not enumerated. Returns ``(detection_dict, registry, top_framework)`` — all
    None when no framework clears the confidence threshold. Never raises.
    """
    try:
        from agent.core.registry import PluginRegistry
        from agent.core.types import Project
        from agent.plugins import register_builtins

        if plugin_files is None:
            files = tuple(
                full
                for _, full in _pruned_files(root)
                if _is_plugin_relevant_file(full)
            )
        else:
            files = tuple(plugin_files)
        project = Project(root=root, files=tuple(sorted(set(files))))

        registry = PluginRegistry()
        register_builtins(registry)
        detected = registry.detect(project)
        frameworks = sorted(
            detected.frameworks, key=lambda f: f.confidence, reverse=True
        )
        if not frameworks or frameworks[0].confidence < _PLUGIN_THRESHOLD:
            return None, None, None
        top = frameworks[0]
        detection = {
            "framework": top.name,
            "version": top.version,
            "confidence": round(float(top.confidence), 3),
            "source": "plugin",
        }
        return detection, registry, top
    except Exception:  # noqa: BLE001 — plugin path is best-effort
        return None, None, None


def _plugin_analyze(
    registry: Any, top: Any, root: Path
) -> dict[str, Any] | None:
    """Run the detected plugin's analyze() and flatten to canonical shape."""
    try:
        import inspect

        from agent.core.workspace import Workspace

        plugin = registry.framework(top.name)
        if plugin is None or not hasattr(plugin, "analyze"):
            return None
        # Offline contract: a structure census must never shell out to a
        # runtime (``php artisan route:list``). Laravel's analyze() accepts
        # runtime_routes=False; plugins that cannot honour the offline kwarg
        # are skipped rather than invoked in a possibly-runtime mode.
        accepts = False
        try:
            accepts = "runtime_routes" in inspect.signature(plugin.analyze).parameters
        except (TypeError, ValueError):  # signature is best-effort
            accepts = False
        if not accepts:
            return None
        analysis = plugin.analyze(Workspace(root), runtime_routes=False)
        data = analysis.data or {}
        return {
            "framework": top.name,
            "version": top.version,
            "confidence": round(float(top.confidence), 3),
            "language": getattr(plugin, "language", None),
            "summary": analysis.summary,
            **{
                key: data.get(key, [] if key != "config" else {})
                for key in _CANONICAL_DATA_KEYS
            },
        }
    except Exception:  # noqa: BLE001 — analysis is best-effort
        return None


# --- marker path ------------------------------------------------------------------

def _marker_detect(
    root: Path, manifests: list[str], primary_language: str | None
) -> dict[str, Any]:
    """Static manifest/import marker detection. Never raises."""
    candidates: list[dict[str, Any]] = []
    try:
        for order, marker in enumerate(_FRAMEWORK_MARKERS):
            if not marker.manifests:
                continue
            for rel in _safe_manifest_rels(manifests):
                if Path(rel).name not in marker.manifests:
                    continue
                hit = _marker_manifest_match(marker, root, rel)
                if hit is None:
                    continue
                version = hit[1]
                candidates.append(
                    {
                        "framework": marker.name,
                        "version": version,
                        "confidence": marker.confidence,
                        "_order": order,
                    }
                )
                break  # one manifest per marker is enough

        # Import scan only when no manifest marker fired (cheaper + noisier).
        if not candidates and primary_language == "python":
            import_hit = _python_import_scan(root)
            if import_hit is not None:
                order, marker, version = import_hit
                candidates.append(
                    {
                        "framework": marker.name,
                        "version": version,
                        "confidence": marker.confidence,
                        "_order": order,
                    }
                )
    except Exception:  # noqa: BLE001 — detection is best-effort
        return dict(_DEFAULT_DETECTION)

    best = _candidate_best(candidates)
    return best if best is not None else dict(_DEFAULT_DETECTION)


def _python_import_scan(
    root: Path,
) -> tuple[int, _FrameworkMarker, str | None] | None:
    """Scan a bounded set of shallow python files for line-anchored imports."""
    try:
        count = 0
        for _, full in _pruned_files(root, max_depth=2):
            name = full.name
            if not name.endswith((".py", ".pyw")):
                continue
            text = _read_text_bounded(full, limit=65_536)
            for order, marker in _PY_IMPORT_MARKERS:
                if _marker_import_match(marker, text):
                    return order, marker, None
            count += 1
            if count >= 40:
                return None
    except OSError:
        return None
    return None


# --- database inference ------------------------------------------------------------

def _active_assignment(line: str) -> tuple[str, str] | None:
    """Return ``(UPPER_KEY, value)`` for an active assignment line.

    Full-comment lines (first non-whitespace char ``#`` or ``;``) and lines
    without an ``=``/``:`` separator return None, so commented ``.env`` samples
    such as ``# DB_CONNECTION=postgres`` never classify.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("//") or stripped[0] in "#;":
        return None
    eq = stripped.find("=")
    colon = stripped.find(":")
    if eq < 0 and colon < 0:
        return None
    if colon < 0:
        separator = eq
    elif eq < 0:
        separator = colon
    else:
        separator = min(eq, colon)
    key = stripped[:separator].strip().strip("'\"")
    value = stripped[separator + 1 :].strip().strip("'\"")
    if not key or not value:
        return None
    return key.upper(), value


def _classify_value(value: str) -> str | None:
    """Human database name from a value string (None when nothing matches)."""
    lowered = value.lower()
    for needle, human in _DB_MARKER_ORDER:
        if needle in lowered:
            return human
    return None


def _db_laravel(root: Path) -> dict[str, Any]:
    """Laravel: parse config/database.php default connection + .env override.

    A root-level ``.env`` (or, when absent, ``.env.example``) carrying an
    active ``DB_CONNECTION=`` / ``DATABASE_URL=`` assignment wins over the
    config fallback; otherwise the config default is reported with evidence
    marked ``(default)``. Commented .env lines are never trusted.
    """
    config_path = _find_config_database(root)
    app_root = root
    if config_path is not None:
        candidate = config_path.parent.parent
        if candidate.is_dir():
            app_root = candidate

    override = _laravel_env_override(app_root, root)
    if override is not None:
        return override

    if config_path is None:
        return {"database": None, "confidence": "low", "evidence": None}
    rel = _rel_posix(root, str(config_path.parent))
    rel_path = f"{rel}/{config_path.name}" if rel else config_path.name
    text = _read_text_bounded(config_path, limit=196_608)

    match = None
    for line in text.splitlines():
        if line.lstrip().startswith(("//", "#", "*")):
            continue
        match = _LARAVEL_DEFAULT_RE.search(line)
        if match is not None:
            break
    if match is None:
        return {"database": None, "confidence": "low", "evidence": rel_path}
    conn = match.group("conn")

    conn_re = re.compile(
        rf"['\"]{re.escape(conn)}['\"]\s*=>\s*\[(?P<body>[^\]]{{0,800}})"
    )
    driver: str | None = None
    block = conn_re.search(text)
    if block:
        d_match = _DRIVER_RE.search(block.group("body"))
        if d_match:
            driver = d_match.group("driver")
    driver = driver or conn

    if driver in _DB_REDIS:
        return {
            "database": None,
            "confidence": "low",
            "evidence": f"{rel_path} (default connection redis — cache, not primary DB)",
        }
    human = _DB_DRIVER_NAMES.get(driver)
    if human is None:
        return {"database": None, "confidence": "low", "evidence": rel_path}
    return {
        "database": human,
        "confidence": "high",
        "evidence": f"{rel_path} (default)",
    }


def _laravel_env_override(app_root: Path, workspace_root: Path) -> dict[str, Any] | None:
    """Active ``.env``/``.env.example`` DB assignment for a Laravel app.

    A real ``.env`` is authoritative; ``.env.example`` is only consulted when
    no ``.env`` ships (it is a template, not runtime state).
    """
    env_path = app_root / ".env"
    example_path = app_root / ".env.example"
    candidates = []
    if env_path.is_file():
        candidates = [env_path]
    elif example_path.is_file():
        candidates = [example_path]
    for path in candidates:
        text = _read_text_bounded(path, limit=131_072)
        assignment = _active_db_assignment(text)
        if assignment is None:
            continue
        kind, value = assignment
        rel = _rel_posix(workspace_root, str(path.parent))
        rel_path = f"{rel}/{path.name}" if rel else path.name
        if kind == "connection":
            if value in _DB_REDIS:
                return {
                    "database": None,
                    "confidence": "low",
                    "evidence": f"{rel_path} (DB_CONNECTION redis — cache, not primary DB)",
                }
            human = _DB_DRIVER_NAMES.get(value)
            if human is None:
                continue
            return {"database": human, "confidence": "high", "evidence": rel_path}
        human = _classify_value(value)
        if human == "redis":
            return {
                "database": None,
                "confidence": "low",
                "evidence": f"{rel_path} (redis URL present — cache, not primary DB)",
            }
        if human is not None:
            return {"database": human, "confidence": "high", "evidence": rel_path}
    return None


def _active_db_assignment(text: str) -> tuple[str, str] | None:
    """``("connection"|"url", value)`` for an active Laravel env DB line."""
    connection: str | None = None
    url: str | None = None
    for line in text.splitlines():
        pair = _active_assignment(line)
        if pair is None:
            continue
        key, value = pair
        if key == "DB_CONNECTION" and connection is None:
            connection = value.lower()
        elif key == "DATABASE_URL" and url is None:
            url = value
    if connection:
        return "connection", connection
    if url:
        return "url", url
    return None


def _db_generic(root: Path) -> dict[str, Any]:
    """Python/js/java: shallow scan of env/settings files for DB markers."""
    matched_files: list[tuple[str, str]] = []  # (rel_path, snippet)
    try:
        for rel, full in _pruned_files(root, max_depth=3):
            name = full.name
            parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
            if not _is_db_candidate(name, parent_rel):
                continue
            snippets = _db_snippets(_read_text_bounded(full, limit=131_072))
            if not snippets:
                continue
            matched_files.append((rel, snippets[0]))
    except OSError:
        pass

    if not matched_files:
        return {"database": None, "confidence": "low", "evidence": None}

    redis_only: str | None = None
    for rel, snippet in sorted(matched_files):
        human = _classify_value(snippet)
        if human is None:
            continue
        if human == "redis":
            redis_only = rel
            continue
        return {"database": human, "confidence": "high", "evidence": rel}
    if redis_only is not None:
        return {
            "database": None,
            "confidence": "low",
            "evidence": f"{redis_only} (redis URL present — cache, not primary DB)",
        }
    return {"database": None, "confidence": "low", "evidence": None}


def _is_db_candidate(name: str, parent_rel: str) -> bool:
    in_settings_pkg = (
        parent_rel.split("/")[-1:] == ["settings"] and name.endswith(".py")
    )
    # settings/ package files: settings/*.py (basename need not be settings.py)
    return name in _DB_CONFIG_FILES or name.endswith(".env.example") or in_settings_pkg


def _db_snippets(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        pair = _active_assignment(line)
        if pair is None:
            continue
        out.append(pair[1][:120])
        if len(out) >= 5:
            break
    return out


# --- architecture helpers ----------------------------------------------------------

def _top_level_dirs(root: Path) -> list[str]:
    try:
        dirs: list[str] = []
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if _skippable(entry.name, ""):
                continue
            dirs.append(entry.name)
        return dirs[:12]
    except OSError:
        return []


def _entrypoints(root: Path) -> list[str]:
    """Entrypoint-like file names up to depth 2 (skip dirs respected).

    Conventional names are probed anywhere in that window; ``*.go`` files under
    a ``cmd/`` path are also recognised (Go's convention).
    """
    found: list[str] = []
    seen: set[str] = set()
    try:
        for rel, full in _pruned_files(root, max_depth=2):
            name = full.name
            if name in _ENTRYPOINT_NAMES and name not in seen:
                seen.add(name)
                found.append(name)
            elif name.endswith(".go") and name not in seen:
                parents = rel.split("/")[:-1]
                if "cmd" in parents:
                    seen.add(name)
                    found.append(name)
    except OSError:
        return []
    return sorted(found)


def _dependency_count(root: Path) -> int | None:
    """Best-effort dependency-map count via projects.dependencies (pure stdlib)."""
    try:
        from projects.dependencies import scan_dependencies

        result = scan_dependencies(str(root))
        packages = result.get("packages") if isinstance(result, dict) else {}
        return len(packages) if isinstance(packages, dict) else 0
    except Exception:  # noqa: BLE001 — dependency count is best-effort
        return None


def _find_config_database(root: Path) -> Path | None:
    """Locate ``config/database.php`` at the root or one level down."""
    try:
        for base in (root, *sorted(root.iterdir())):
            if not base.is_dir() or base.name.startswith((".", "__MACOSX")):
                continue
            if _skippable(base.name, ""):
                continue
            candidate = base / "config" / "database.php"
            if candidate.is_file():
                return candidate
    except OSError:
        return None
    return None
