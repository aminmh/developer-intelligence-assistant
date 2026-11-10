"""Read-only project source for navigation tools.

A chat reaches source in one of two ways, checked in this order:

1. An indexed project revision (``project_id`` with ``revision_id`` or
   ``commit_sha``): its immutable archive is extracted once into a local cache
   keyed by archive checksum, made read-only, and reused.
2. The newest retained workspace extracted for the chat's uploaded archive.

Every path a tool returns is relative to that root, and paths excluded by the
Code Quality rules (vendor, build, caches, project exclusion patterns) are
never searched or returned.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from agent_tools.types import ToolCallRejected, ToolContext

CACHE_ROOT = Path(os.environ.get("CODE_TOOLS_CACHE_DIR", "/tmp/devassistant-code-tools"))
MAX_CACHED_REVISIONS = 6
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


# Files that commonly hold credentials. Tool output goes to the remote model, so
# these are never searched, read, or listed, whatever the project's exclusions.
_SECRET_NAMES = {
    ".npmrc", ".pypirc", ".netrc", ".htpasswd", "auth.json", "credentials", "credentials.json",
    "secrets.json", "secrets.yml", "secrets.yaml", "service-account.json", "id_rsa", "id_dsa",
    "id_ecdsa", "id_ed25519", ".git-credentials", "wp-config.php",
}
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".crt", ".der")
_SECRET_MARKERS = ("service-account", "service_account", "serviceaccount", "credential", "secret", "private-key",
                   "private_key", "privatekey", "keyfile")
SECRET_GLOBS = [".env", ".env.*", "*.env", *(f"*{s}" for s in _SECRET_SUFFIXES), *sorted(_SECRET_NAMES),
                *(f"*{m}*.{ext}" for m in _SECRET_MARKERS for ext in ("json", "yml", "yaml", "xml", "ini", "toml"))]


def is_secret_path(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    if ".git" in parts or ".ssh" in parts or ".aws" in parts:
        return True
    name = parts[-1].lower() if parts else ""
    if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
        return name not in {".env.example", ".env.sample", ".env.dist", ".env.template"}
    if name in _SECRET_NAMES or name.endswith(_SECRET_SUFFIXES):
        return True
    # e.g. firebase-service-account.json, google-credentials.json, app-secrets.yaml
    stem, _, ext = name.rpartition(".")
    return ext in {"json", "yml", "yaml", "xml", "ini", "toml"} and any(
        marker in stem for marker in _SECRET_MARKERS)


@dataclass(frozen=True)
class SourceRoot:
    path: Path
    origin: str  # "revision" or "workspace"
    project_id: str | None = None
    revision_id: str | None = None
    exclusion_patterns: list[str] = field(default_factory=list)
    wrapper_prefix: str = ""  # archive folders skipped above the project root

    def is_excluded(self, relative: str) -> bool:
        from code_quality.exclusions import matches_exclusion
        from code_quality.service import _is_excluded_source_path

        if is_secret_path(relative) or _is_excluded_source_path(relative):
            return True
        if not self.exclusion_patterns:
            return False
        # Project exclusion patterns may be written against the archive layout.
        candidates = [relative] + ([f"{self.wrapper_prefix}/{relative}"] if self.wrapper_prefix else [])
        return any(matches_exclusion(path, self.exclusion_patterns) for path in candidates)

    def relative(self, path: str | Path) -> str | None:
        """Root-relative POSIX path, or None when outside the root."""
        try:
            return Path(path).resolve().relative_to(self.path.resolve()).as_posix()
        except (ValueError, OSError):
            return None

    def resolve_file(self, relative: str) -> Path:
        """Resolve a model-supplied relative path to a readable file inside the root."""
        text = str(relative or "").strip().replace("\\", "/")
        while text.startswith("./"):
            text = text[2:]
        if not text or text.startswith("/") or ".." in PurePosixPath(text).parts:
            raise ToolCallRejected("invalid_path", "Give a project-relative file path without '..'.")
        if self.wrapper_prefix and text.startswith(self.wrapper_prefix + "/"):
            # Older answers and the symbol graph may still cite the archive folder.
            text = text[len(self.wrapper_prefix) + 1:]
        candidate = (self.path / text).resolve()
        rel = self.relative(candidate)
        if rel is None:
            raise ToolCallRejected("invalid_path", "The path is outside the project.")
        if self.is_excluded(rel):
            raise ToolCallRejected("excluded_path", f"{rel} is excluded from code tools (dependency, build, or cache path).")
        if not candidate.is_file():
            raise ToolCallRejected("not_found", f"No file named {rel} in the project.")
        return candidate


def _make_read_only(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    for dirpath, dirnames, _ in os.walk(root, topdown=False):
        for name in dirnames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                os.chmod(path, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)


def _remove_tree(path: Path) -> None:
    def make_writable(func, target, _exc):
        os.chmod(target, stat.S_IRWXU)
        func(target)

    shutil.rmtree(path, onerror=make_writable)


def _prune(keep: Path) -> None:
    entries = [p for p in CACHE_ROOT.iterdir() if p.is_dir() and _SHA256.match(p.name) and p != keep]
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in entries[MAX_CACHED_REVISIONS - 1:]:
        _remove_tree(stale)


def _revision_root(context: ToolContext) -> SourceRoot:
    from code_quality.service import ReportStore
    from index.storage import storage
    from infra.php_tools import extract_zip_safely
    from telemetry.registry import default_registry
    from telemetry.snapshot import source_reference

    registry = default_registry()
    snapshot = (registry.get_by_revision_id(context.project_id, context.revision_id) if context.revision_id
                else registry.get(context.project_id, context.commit_sha))
    if not snapshot or snapshot.get("state") != "ready":
        raise ToolCallRejected("no_source", "The selected project revision is not indexed and ready.")
    checksum = str(snapshot.get("source_archive_sha256") or "")
    if not _SHA256.match(checksum):
        raise ToolCallRejected("no_source", "The selected revision has no verified source archive.")

    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    target = CACHE_ROOT / checksum
    with open(CACHE_ROOT / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not target.is_dir():
            key = registry.archive_key(context.project_id, source_reference({**snapshot, "project_id": context.project_id}))
            if not key:
                raise ToolCallRejected("no_source", "The source archive for this revision is no longer available.")
            data = storage.get_snapshot_archive(key)
            staging = Path(tempfile.mkdtemp(prefix="extract_", dir=CACHE_ROOT))
            try:
                extract_zip_safely(data, str(staging))
                _make_read_only(staging)
                os.rename(staging, target)
            except Exception:
                _remove_tree(staging)
                raise
        os.utime(target)
        _prune(target)
    return SourceRoot(
        path=target, origin="revision", project_id=context.project_id,
        revision_id=snapshot.get("revision_id"),
        exclusion_patterns=ReportStore().exclusions(context.project_id),
    )


_IGNORED_TOP_LEVEL = {"__MACOSX", ".DS_Store"}
# A lone folder with one of these names is part of the project, not a wrapper.
_PROJECT_FOLDERS = {
    "app", "src", "lib", "libs", "source", "routes", "config", "tests", "test", "spec", "modules", "packages",
    "public", "resources", "database", "cmd", "internal", "pkg", "api", "server", "client", "web", "frontend",
    "backend", "services", "components", "main", "scripts", "bin", "docs",
}


def project_root(path: Path, max_depth: int = 2) -> tuple[Path, str]:
    """Descend through archive wrapper folders to the project itself.

    Archives commonly hold one folder (``my-project/``) around the project, so
    every path would start with it and globs like ``routes/*.php`` would miss.
    Returns ``(root, wrapper_prefix)`` where the prefix is the skipped folders.
    A lone conventional source folder (``app/``, ``src/``) is never skipped.
    """
    root, skipped = path, []
    for _ in range(max_depth):
        try:
            entries = [e for e in root.iterdir() if e.name not in _IGNORED_TOP_LEVEL]
        except OSError:
            break
        if len(entries) != 1 or not entries[0].is_dir() or entries[0].is_symlink():
            break
        if entries[0].name.lower() in _PROJECT_FOLDERS:
            break
        skipped.append(entries[0].name)
        root = entries[0]
    return root, "/".join(skipped)


def resolve_source(context: ToolContext) -> SourceRoot:
    """Return the read-only source root for this chat or raise ``ToolCallRejected``."""
    if context.project_id and (context.revision_id or context.commit_sha):
        base = _revision_root(context)
    elif context.workspace_path and os.path.isdir(context.workspace_path):
        base = SourceRoot(path=Path(context.workspace_path), origin="workspace", project_id=context.project_id)
    else:
        raise ToolCallRejected(
            "no_source",
            "No project source is available for this chat. Select an indexed project revision or upload the project.",
        )
    root, prefix = project_root(base.path)
    if not prefix:
        return base
    return SourceRoot(path=root, origin=base.origin, project_id=base.project_id, revision_id=base.revision_id,
                      exclusion_patterns=base.exclusion_patterns, wrapper_prefix=prefix)
