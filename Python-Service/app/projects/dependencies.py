"""Dependency-context scanner — version-aware package map from lockfiles.

Reads a project workspace's package-manager files and builds a normalized map
of installed dependencies (ecosystem, package_manager, packages). Lockfiles are
preferred over manifests because they carry *resolved* versions.

Supported sources:
  PHP        composer.json / composer.lock
  JS/TS      package.json, package-lock.json, pnpm-lock.yaml, yarn.lock,
             bun.lock, bun.lockb
  Python     pyproject.toml, uv.lock, poetry.lock, requirements*.txt,
             Pipfile(.lock)

Rules (from the doc-retrieval spec):
  * prefer lockfiles over manifests,
  * manifest-declared-but-not-locked packages are NOT treated as installed,
  * monorepo: dependency files of the package/service currently being scanned,
  * exact resolved version is what doc retrieval queries against.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# --- helpers -----------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _scan_dir(root: str, skip_names: tuple[str, ...]) -> list[Path]:
    """Walk ``root`` collecting candidate dependency files.

    Monorepo rule: scan shallowly first (project root), and prefer the
    dependency files nearest to the source tree being queried. We collect
    candidates ordered root-first so callers pick the top-level one unless a
    deeper ``subdir`` is explicitly provided.
    """
    root_path = Path(root)
    found: list[Path] = []
    if not root_path.is_dir():
        return found
    skip = set(skip_names)
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        depth = dirpath[len(str(root_path)) :].count(os.sep)
        if depth > 4:  # don't wander into vendored dependency trees too far
            dirnames[:] = []
            continue
        for name in filenames:
            if name in DEP_FILE_NAMES:
                found.append(Path(dirpath) / name)
    found.sort(key=lambda p: len(p.parts))  # root files first
    return found


DEP_FILE_NAMES = {
    # php
    "composer.json",
    "composer.lock",
    # js/ts
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    # python
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "requirements.txt",
    "Pipfile",
    "Pipfile.lock",
}

# Directories never scanned for dependency manifests.
_SKIP_DIRS = (
    "node_modules",
    "vendor",
    ".venv",
    "venv",
    "dist",
    "build",
    ".git",
    "__pycache__",
    "site-packages",
    ".tox",
)

# Well-known names that are tools/frameworks worth documenting, filter noise.
_COMMON_PHP = {
    "php", "ext-json", "ext-mbstring", "ext-pdo", "ext-tokenizer",
    "ext-dom", "ext-xml", "ext-ctype", "ext-filter", "ext-fileinfo",
}


def scan_dependencies(workspace: str) -> dict[str, Any]:
    """Scan ``workspace`` and return the normalized dependency map.

    Returns ``{"ecosystem": ..., "package_manager": ..., "packages": {...}}``
    with an empty packages dict when nothing resolvable is found.
    """
    files = _scan_dir(workspace, _SKIP_DIRS)
    if not files:
        return {"ecosystem": "unknown", "package_manager": None, "packages": {}}

    # composer.lock beats composer.json; package-lock beats package.json etc.
    for lock in (
        "composer.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
    ):
        match = next((f for f in files if f.name == lock), None)
        if match:
            parsed = _parse_lockfile(lock, match, files)
            if parsed["packages"]:
                return parsed

    # No lockfile → parse manifests (requested versions, installed=False unless
    # the ecosystem cannot resolve more precisely — manifests only).
    for manifest in ("composer.json", "package.json", "pyproject.toml", "Pipfile"):
        match = next((f for f in files if f.name == manifest), None)
        if match:
            parsed = _parse_manifest(manifest, match, files)
            if parsed["packages"]:
                return parsed

    return {"ecosystem": "unknown", "package_manager": None, "packages": {}}


# --- lockfile parsers ---------------------------------------------------------


def _parse_lockfile(lock_name: str, lock_path: Path, files: list[Path]) -> dict[str, Any]:
    if lock_name == "composer.lock":
        data = _read_json(lock_path)
        packages: dict[str, Any] = {}
        if data:
            locked = data.get("packages") or []
            for p in locked:
                name = str(p.get("name") or "")
                ver = str(p.get("version") or "")
                if name and name not in _COMMON_PHP:
                    packages[name] = {
                        "requested_version": None,
                        "resolved_version": ver or None,
                        "source": "composer.lock",
                        "installed": True,
                    }
            # annotate requested constraints from composer.json when present
            composer_json = next(
                (f for f in files if f.name == "composer.json"), None
            )
            if composer_json:
                cdata = _read_json(composer_json)
                if cdata:
                    req = cdata.get("require") or {}
                    for name, constraint in req.items():
                        if name in packages:
                            packages[name]["requested_version"] = str(constraint)
        return {"ecosystem": "php", "package_manager": "composer", "packages": packages}

    if lock_name == "package-lock.json":
        data = _read_json(lock_path)
        packages: dict[str, Any] = {}
        if data:
            root = data.get("packages") or {}
            # "packages" object in npm v7+: keys are paths; "" is the root.
            for dep_path, dep in root.items():
                if not dep_path or not isinstance(dep, dict):
                    continue
                name = str(dep.get("name") or dep_path.removeprefix("node_modules/").split("/node_modules/")[-1])
                ver = str(dep.get("version") or "")
                resolved = dep.get("resolved")
                if name and name not in ("", "."):
                    packages[name] = {
                        "requested_version": None,
                        "resolved_version": ver or None,
                        "source": "package-lock.json",
                        "installed": True,
                        "resolved_url": resolved if isinstance(resolved, str) else None,
                    }
            # top-level requested ranges from package.json dependencies
            pkg_json = next((f for f in files if f.name == "package.json"), None)
            if pkg_json:
                pdata = _read_json(pkg_json)
                if pdata:
                    for section in ("dependencies", "devDependencies"):
                        for name, constraint in (pdata.get(section) or {}).items():
                            if name in packages:
                                packages[name]["requested_version"] = str(constraint)
        return {"ecosystem": "js", "package_manager": "npm", "packages": packages}

    if lock_name in ("pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb"):
        # YAML/TOML lockfiles: parse via available stdlib? None — fall back to a
        # light line scan for `name@version` tuples (pnpm/yarn/bun list
        # resolved packages in their header/importers sections as specifiers).
        packages: dict[str, Any] = _scan_specifier_lockfile(lock_name, lock_path)
        ecosystem = "js"
        manager = {
            "pnpm-lock.yaml": "pnpm",
            "yarn.lock": "yarn",
            "bun.lock": "bun",
            "bun.lockb": "bun",
        }[lock_name]
        return {"ecosystem": ecosystem, "package_manager": manager, "packages": packages}

    if lock_name == "uv.lock":
        # uv.lock is TOML. Keep it simple: scan `[[package]]` name/version.
        packages: dict[str, Any] = {}
        try:
            text = lock_path.read_text(encoding="utf-8", errors="replace")
            cur_name: str | None = None
            cur_ver: str | None = None
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("name = "):
                    cur_name = s.split("=", 1)[1].strip().strip('"')
                elif s.startswith("version = ") and cur_name:
                    cur_ver = s.split("=", 1)[1].strip().strip('"')
                    if cur_name and cur_name not in _COMMON_PHP:
                        packages[cur_name] = {
                            "requested_version": None,
                            "resolved_version": cur_ver or None,
                            "source": "uv.lock",
                            "installed": True,
                        }
                    cur_name = cur_ver = None
        except Exception:  # noqa: BLE001, S110
            pass
        return {"ecosystem": "python", "package_manager": "uv", "packages": packages}

    if lock_name == "poetry.lock":
        packages: dict[str, Any] = {}
        try:
            text = lock_path.read_text(encoding="utf-8", errors="replace")
            cur_name: str | None = None
            cur_ver: str | None = None
            for line in text.splitlines():
                s = line.strip()
                if s.startswith('name = "'):
                    cur_name = s.split("=", 1)[1].strip().strip('"')
                elif s.startswith('version = "') and cur_name:
                    cur_ver = s.split("=", 1)[1].strip().strip('"')
                    if cur_name:
                        packages[cur_name] = {
                            "requested_version": None,
                            "resolved_version": cur_ver,
                            "source": "poetry.lock",
                            "installed": True,
                        }
                    cur_name = cur_ver = None
        except Exception:  # noqa: BLE001, S110
            pass
        return {"ecosystem": "python", "package_manager": "poetry", "packages": packages}

    if lock_name == "Pipfile.lock":
        data = _read_json(lock_path)
        packages: dict[str, Any] = {}
        if data:
            for section in ("default", "develop"):
                for name, meta in (data.get(section) or {}).items():
                    if not isinstance(meta, dict):
                        continue
                    packages[name] = {
                        "requested_version": None,
                        "resolved_version": str(meta.get("version") or "").lstrip("=") or None,
                        "source": "Pipfile.lock",
                        "installed": True,
                    }
        return {"ecosystem": "python", "package_manager": "pipenv", "packages": packages}

    return {"ecosystem": "unknown", "package_manager": None, "packages": {}}


def _scan_specifier_lockfile(lock_name: str, lock_path: Path) -> dict[str, Any]:
    """Light parser for yarn/pnpm/bun lockfiles.

    These are YAML/TOML without a stdlib parser in the container — but they
    consistently list resolved package specifiers like ``"name@version":`` or
    ``name@npm:name@version``. We extract ``name -> version`` pairs.
    """
    packages: dict[str, Any] = {}
    try:
        text = lock_path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return packages
    # pnpm: `  /@scope/pkg@1.2.3:`  | yarn: `"@scope/pkg@^1.2.3", "..."`-style keys
    # bun: `name@version` lines under [packages] or specifier keys.
    pattern = re.compile(r'["\s/]((?:@[^/@\s]+/)?[^/@\s:]+)@([^"@:\s,]+)')
    for line in text.splitlines():
        s = line.strip().rstrip(":")
        for m in pattern.finditer(s):
            name, version = m.group(1), m.group(2)
            if name in ("name", "version", "lockfile", "resolution") or name.startswith("."):
                continue
            version = version.rstrip(",")
            if version.startswith(("npm:", "http")) or "(" in version or version == "*":
                continue
            if name not in packages:
                packages[name] = {
                    "requested_version": None,
                    "resolved_version": version,
                    "source": lock_name,
                    "installed": True,
                }
    return packages


# --- manifest parsers ---------------------------------------------------------


def _parse_manifest(manifest_name: str, path: Path, files: list[Path]) -> dict[str, Any]:
    """Parse manifests without lockfiles. resolved_version stays None → the
    caller will query docs versionless (spec rule 9)."""
    if manifest_name == "composer.json":
        data = _read_json(path)
        packages: dict[str, Any] = {}
        if data:
            for name, constraint in (data.get("require") or {}).items():
                if name and name not in _COMMON_PHP:
                    packages[name] = {
                        "requested_version": str(constraint),
                        "resolved_version": None,
                        "source": "composer.json",
                        "installed": False,
                    }
        return {"ecosystem": "php", "package_manager": "composer", "packages": packages}

    if manifest_name == "package.json":
        data = _read_json(path)
        packages: dict[str, Any] = {}
        if data:
            for section in ("dependencies", "devDependencies"):
                for name, constraint in (data.get(section) or {}).items():
                    packages[name] = {
                        "requested_version": str(constraint),
                        "resolved_version": None,
                        "source": "package.json",
                        "installed": False,
                    }
        return {"ecosystem": "js", "package_manager": "npm", "packages": packages}

    if manifest_name == "pyproject.toml":
        packages: dict[str, Any] = {}
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            in_deps = False
            for line in text.splitlines():
                s = line.strip()
                if s.startswith(("[project.dependencies]", "[project.optional-dependencies]")):
                    in_deps = True
                    continue
                if s.startswith("[") and s != "[":
                    in_deps = False
                    continue
                if in_deps and s.startswith(('"', "'", "name =")):
                    m = re.match(r"^[\"']?([A-Za-z0-9_.\-]+)", s)
                    if m and not s.startswith("name"):
                        name = m.group(1)
                        packages[name] = {
                            "requested_version": None,
                            "resolved_version": None,
                            "source": "pyproject.toml",
                            "installed": False,
                        }
        except Exception:  # noqa: BLE001, S110
            pass
        return {"ecosystem": "python", "package_manager": "pip", "packages": packages}

    if manifest_name == "Pipfile":
        packages: dict[str, Any] = {}
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            cur_section = None
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("["):
                    cur_section = s.strip("[]").lower()
                    continue
                if cur_section in ("packages", "dev-packages") and s and not s.startswith("#"):
                    name = s.split("=", 1)[0].strip().strip('"')
                    if name:
                        packages[name] = {
                            "requested_version": None,
                            "resolved_version": None,
                            "source": "Pipfile",
                            "installed": False,
                        }
        except Exception:  # noqa: BLE001, S110
            pass
        return {"ecosystem": "python", "package_manager": "pipenv", "packages": packages}

    if manifest_name == "requirements.txt":
        packages: dict[str, Any] = {}
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                s = line.strip()
                if not s or s.startswith(("#", "-", ".")):
                    continue
                m = re.match(r"^([A-Za-z0-9_.\-]+)([<>=!~].*)?$", s)
                if m:
                    name = m.group(1)
                    packages[name] = {
                        "requested_version": m.group(2) or None,
                        "resolved_version": None,
                        "source": "requirements.txt",
                        "installed": False,
                    }
        except Exception:  # noqa: BLE001, S110
            pass
        return {"ecosystem": "python", "package_manager": "pip", "packages": packages}

    return {"ecosystem": "unknown", "package_manager": None, "packages": {}}
