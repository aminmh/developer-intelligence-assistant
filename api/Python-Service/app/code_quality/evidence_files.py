"""Non-source evidence files: CI, container, build, manifests, configuration, docs, translations, templates.

Plan task 13 (inventory) and task 14 (reader tools); contract: the evidence spec, clauses 1-2.
One classifier serves the assessment inventory (from the revision archive) and the read-only
tools (from the extracted revision), so both list the same files.

Rules shared with source reads apply: dependency, build and cache folders and the project's
exclusion patterns are left out, files over the source size limit are listed as unreadable,
and secret-bearing files (``agent_tools.source.is_secret_path``) are listed by name only,
never read. A file may be source and evidence at once (an HTML-in-PHP template, a translation
catalogue written as code); the source inventory is unchanged.
"""

from __future__ import annotations

import io
import os
import zipfile
from collections import Counter
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

CHECK_VERSION = "evidence-files-1"
KINDS = ("ci", "container", "build", "manifest", "config", "docs", "translations", "templates")
SECRET = "secret"
MAX_LISTED = 3000      # files listed per revision; the rest are counted
MAX_REPORTED = 500     # files kept in the stored report
# Left out of source scoring (sample code would skew it), but that is where documentation lives.
EVIDENCE_DIRS = frozenset({"docs"})

_CI_FILES = {".gitlab-ci.yml", ".travis.yml", "jenkinsfile", "azure-pipelines.yml", "bitbucket-pipelines.yml",
             ".drone.yml", ".woodpecker.yml", "cloudbuild.yaml", "cloudbuild.yml", "appveyor.yml", ".appveyor.yml"}
_CI_DIRS = (".github/workflows/", ".gitlab/", ".circleci/", ".buildkite/", ".woodpecker/", ".azure-pipelines/")
_CONTAINER_GLOBS = ("dockerfile", "dockerfile.*", "*.dockerfile", "containerfile", "containerfile.*",
                    "docker-compose*.yml", "docker-compose*.yaml", "compose.yml", "compose.yaml", "compose.*.yml",
                    "compose.*.yaml", ".dockerignore", "chart.yaml", "values*.yaml", "kustomization.yaml",
                    "skaffold.yaml", "devcontainer.json", ".devcontainer.json", "fly.toml", "app.yaml", "procfile")
_BUILD_FILES = {"makefile", "gnumakefile", "taskfile.yml", "taskfile.yaml", "justfile", "cmakelists.txt",
                "build.xml", "rakefile", "gruntfile.js", "gulpfile.js", "webpack.config.js", "vite.config.js",
                "vite.config.ts", "rollup.config.js", "turbo.json", "nx.json", "lerna.json", "box.json"}
_MANIFEST_FILES = {"composer.json", "composer.lock", "package.json", "package-lock.json", "npm-shrinkwrap.json",
                   "yarn.lock", "pnpm-lock.yaml", "pnpm-workspace.yaml", "bun.lockb", "requirements.txt",
                   "pyproject.toml", "pipfile", "pipfile.lock", "poetry.lock", "uv.lock", "setup.cfg",
                   "go.mod", "go.sum", "go.work", "cargo.toml", "cargo.lock", "gemfile", "gemfile.lock",
                   "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
                   "gradle.properties", "packages.config", "directory.packages.props", "global.json",
                   "mix.exs", "mix.lock", "pubspec.yaml", "pubspec.lock", "podfile", "podfile.lock",
                   ".nvmrc", ".node-version", ".python-version", ".ruby-version", ".php-version",
                   ".tool-versions", ".sdkmanrc", "runtime.txt", ".terraform-version"}
_MANIFEST_GLOBS = ("requirements*.txt", "*.csproj", "*.fsproj", "*.vbproj", "*.sln")
_CONFIG_FILES = {".editorconfig", ".gitattributes", ".gitignore", ".htaccess", "nginx.conf", "robots.txt",
                 ".browserslistrc", ".prettierrc", ".stylelintrc", ".eslintrc", ".babelrc", ".npmignore"}
_CONFIG_GLOBS = ("phpunit.xml*", "phpstan*.neon*", "psalm*.xml*", "phpcs.xml*", ".phpcs.xml*", "rector.php",
                 "infection.json*", "tsconfig*.json", "jsconfig*.json", ".eslintrc.*", "eslint.config.*",
                 ".prettierrc.*", "prettier.config.*", ".stylelintrc.*", "jest.config.*", "vitest.config.*",
                 "karma.conf.*", "cypress.config.*", "playwright.config.*", "codeception.yml*", "behat.yml*",
                 "pytest.ini", "tox.ini", "mypy.ini", ".flake8", ".pylintrc", ".rubocop.yml", ".golangci.y*ml",
                 "sonar-project.properties", ".pre-commit-config.yaml", "renovate.json*", ".env.example",
                 ".env.sample", ".env.dist", ".env.template", "*.env.example", "tailwind.config.*", "postcss.config.*", "babel.config.*",
                 "app.config.*", "appsettings*.json", "web.config", "*.ini", "*.neon", "*.properties", "*.conf",
                 "*.cfg", "*.toml")
_CONFIG_DIRS = {"config", "configs", "conf", "configuration", "settings", "etc", "deploy", "deployment", "infra",
                "k8s", "kubernetes", "helm", "terraform", "ansible", "docker", ".docker", "devops", "ops"}
_CONFIG_SUFFIXES = (".yaml", ".yml", ".json", ".json5", ".xml", ".ini", ".toml", ".neon", ".properties",
                    ".conf", ".cfg", ".tf", ".tfvars", ".hcl", ".dist")
_DOC_NAMES = ("readme", "changelog", "changes", "contributing", "license", "licence", "copying", "notice",
              "security", "code_of_conduct", "upgrade", "upgrading", "install", "authors", "claude", "agents",
              "gemini", "copilot-instructions", "architecture", "adr")
_DOC_SUFFIXES = (".md", ".mdx", ".markdown", ".rst", ".adoc", ".asciidoc", ".txt", ".org")
_DOC_DIRS = {"docs", "doc", "documentation", "adr", "adrs", "decisions", "wiki", "guides", ".archcore"}
_AGENT_FILES = {".cursorrules", ".windsurfrules", ".clinerules"}
_TRANSLATION_DIRS = {"translations", "translation", "lang", "langs", "language", "languages", "locale", "locales",
                     "i18n", "l10n", "messages"}
_TRANSLATION_SUFFIXES = (".po", ".pot", ".mo", ".xlf", ".xliff", ".arb", ".resx", ".strings", ".stringsdict")
_CATALOGUE_SUFFIXES = (".yaml", ".yml", ".json", ".php", ".properties", ".ini", ".toml", ".js", ".ts")
_TEMPLATE_SUFFIXES = (".twig", ".blade.php", ".html", ".htm", ".xhtml", ".hbs", ".handlebars", ".mustache",
                      ".ejs", ".erb", ".haml", ".slim", ".jinja", ".jinja2", ".j2", ".njk", ".liquid", ".tpl",
                      ".latte", ".phtml", ".vue", ".cshtml", ".razor", ".jsp", ".gohtml", ".tmpl", ".pug", ".jade")


def _any(name: str, globs: Iterable[str]) -> bool:
    return any(fnmatchcase(name, pattern) for pattern in globs)


def classify(relative: str) -> str | None:
    """The evidence kind of a project-relative path, ``"secret"`` for a secret-bearing file, else None."""
    from agent_tools.source import is_secret_path

    path = PurePosixPath(relative)
    if not path.parts:
        return None
    if is_secret_path(relative):
        return SECRET
    name = path.name.lower()
    folders = [part.lower() for part in path.parts[:-1]]
    lowered = "/".join([*folders, name])
    if name in _CI_FILES or any(lowered.startswith(prefix) or f"/{prefix}" in lowered for prefix in _CI_DIRS):
        if name.endswith((".yml", ".yaml", ".json", ".groovy", ".sh")) or "." not in name or name in _CI_FILES:
            return "ci"
    if _any(name, _CONTAINER_GLOBS) or (folders and folders[-1] in {"templates"} and "helm" in folders):
        return "container"
    if name in _MANIFEST_FILES or _any(name, _MANIFEST_GLOBS):
        return "manifest"
    if name in _BUILD_FILES or name.endswith((".sh", ".bash")):  # build and analysis scripts
        return "build"
    if name.endswith(_TRANSLATION_SUFFIXES):
        return "translations"
    if any(part in _TRANSLATION_DIRS for part in folders) and name.endswith(_CATALOGUE_SUFFIXES):
        return "translations"
    if name.endswith(_TEMPLATE_SUFFIXES):
        return "templates"
    if name in _AGENT_FILES or name.split(".", 1)[0] in _DOC_NAMES and (name.endswith(_DOC_SUFFIXES) or "." not in name):
        return "docs"
    if name.endswith(_DOC_SUFFIXES) and (not folders or any(part in _DOC_DIRS for part in folders)):
        return "docs"
    if name in _CONFIG_FILES or _any(name, _CONFIG_GLOBS):
        return "config"
    if any(part in _CONFIG_DIRS for part in folders) and name.endswith(_CONFIG_SUFFIXES):
        return "config"
    if not folders and name.endswith((".yaml", ".yml", ".xml", ".json")) and not name.endswith(".min.json"):
        return "config"
    return None


def is_excluded_path(relative: str) -> bool:
    """The source exclusions (dependencies, build output, caches, generated files), except EVIDENCE_DIRS."""
    from code_quality.service import _is_excluded_source_path

    path = PurePosixPath(relative)
    kept = [part for part in path.parts[:-1] if part.lower() not in EVIDENCE_DIRS]
    return _is_excluded_source_path("/".join([*kept, path.name]))


def _excluded(relative: str, patterns: list[str], pattern_prefix: str = "") -> bool:
    from code_quality.exclusions import matches_exclusion

    if is_excluded_path(relative):
        return True
    # Project patterns may be written against the archive layout (as SourceRoot.is_excluded allows).
    candidates = [relative] + ([f"{pattern_prefix}/{relative}"] if pattern_prefix else [])
    return bool(patterns) and any(matches_exclusion(path, patterns) for path in candidates)


def _entry(relative: str, kind: str, size: int) -> dict[str, Any]:
    from code_quality.service import MAX_FILE_BYTES

    readable = kind != SECRET and size <= MAX_FILE_BYTES
    reason = None if readable else ("secret-bearing file; listed by name only" if kind == SECRET
                                    else "larger than the source read limit")
    return {"path": relative, "kind": kind, "bytes": size, "readable": readable, "reason": reason}


def collect(entries: Iterable[tuple[str, int]], exclusion_patterns: list[str] | None = None,
            wrapper_prefix: str = "", pattern_prefix: str = "") -> dict[str, Any]:
    """Classify ``(relative path, size)`` pairs into the evidence inventory.

    With a ``wrapper_prefix`` (the folder an archive wraps the project in), paths are classified
    relative to the project but reported as given, matching the source inventory."""
    from code_quality.exclusions import normalize_exclusion_patterns

    patterns = normalize_exclusion_patterns(exclusion_patterns or [])
    files: list[dict[str, Any]] = []
    by_kind: Counter[str] = Counter()
    excluded = unlisted = 0
    strip = wrapper_prefix + "/" if wrapper_prefix else ""
    for relative, size in entries:
        kind = classify(relative[len(strip):] if strip and relative.startswith(strip) else relative)
        if kind is None:
            continue
        if _excluded(relative, patterns, pattern_prefix):
            excluded += 1
            continue
        by_kind[kind] += 1
        if len(files) >= MAX_LISTED:
            unlisted += 1
            continue
        files.append(_entry(relative, kind, size))
    files.sort(key=lambda row: (KINDS.index(row["kind"]) if row["kind"] in KINDS else len(KINDS), row["path"]))
    return {"check_version": CHECK_VERSION, "files": files, "by_kind": {k: by_kind[k] for k in (*KINDS, SECRET) if by_kind[k]},
            "excluded_files": excluded, "unlisted_files": unlisted,
            "unreadable_files": sum(1 for row in files if not row["readable"])}


def wrapper_of(paths: list[str]) -> str:
    """The folders an archive wraps the project in (as ``agent_tools.source.project_root`` skips them)."""
    from agent_tools.source import _IGNORED_TOP_LEVEL, _PROJECT_FOLDERS

    skipped: list[str] = []
    rows = [PurePosixPath(p).parts for p in paths]
    for depth in range(2):
        tops = {parts[depth] for parts in rows if len(parts) > depth and parts[depth] not in _IGNORED_TOP_LEVEL}
        nested = all(len(parts) > depth + 1 for parts in rows if len(parts) > depth and parts[depth] not in _IGNORED_TOP_LEVEL)
        if len(tops) != 1 or not nested or next(iter(tops)).lower() in _PROJECT_FOLDERS:
            break
        skipped.append(next(iter(tops)))
    return "/".join(skipped)


def inventory_archive(archive_data: bytes, exclusion_patterns: list[str] | None = None) -> dict[str, Any]:
    """The evidence inventory of a revision archive (names and sizes only; nothing is read)."""
    from code_quality.service import MAX_ARCHIVE_ENTRIES, _safe_archive_path

    with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise ValueError("The archive exceeds assessment limits.")
        entries = [(relative, info.file_size) for info in infos if not info.is_dir()
                   for relative in [_safe_archive_path(info.filename.rstrip("/"))] if relative]
    return collect(entries, exclusion_patterns, wrapper_of([path for path, _ in entries]))


def inventory_directory(root: Path, exclusion_patterns: list[str] | None = None, pattern_prefix: str = "") -> dict[str, Any]:
    """The evidence inventory of an extracted project root; excluded folders are not walked.

    ``pattern_prefix`` is the wrapper folder above ``root`` that exclusion patterns may name."""
    from code_quality.service import EXCLUDED_SOURCE_DIRS

    def entries() -> Iterable[tuple[str, int]]:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d.lower() not in EXCLUDED_SOURCE_DIRS - EVIDENCE_DIRS and not os.path.islink(os.path.join(dirpath, d)))
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    continue
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                yield Path(full).relative_to(root).as_posix(), size

    return collect(entries(), exclusion_patterns, pattern_prefix=pattern_prefix)


def report_summary(inventory: dict[str, Any]) -> dict[str, Any]:
    """What the stored report keeps: counts plus the first files of each kind (bounded)."""
    files = inventory["files"]
    kept: list[dict[str, Any]] = []
    per_kind = max(20, MAX_REPORTED // max(1, len(inventory["by_kind"])))
    seen: Counter[str] = Counter()
    for row in files:
        if seen[row["kind"]] < per_kind and len(kept) < MAX_REPORTED:
            seen[row["kind"]] += 1
            kept.append({key: row[key] for key in ("path", "kind", "bytes", "readable")})
    return {"check_version": inventory["check_version"], "by_kind": inventory["by_kind"],
            "listed_files": len(files), "reported_files": len(kept), "files": kept,
            "excluded_files": inventory["excluded_files"], "unlisted_files": inventory["unlisted_files"],
            "unreadable_files": inventory["unreadable_files"]}
