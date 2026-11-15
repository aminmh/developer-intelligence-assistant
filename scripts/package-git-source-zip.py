#!/usr/bin/env python3
"""Convert a ``git archive --format=tar`` stream into an API-safe ZIP.

Git archives can contain symlinks. The source ingestion API intentionally
rejects ZIP symlink entries, so safe links to regular files are materialized as
ordinary files. Directory links and unsafe or unresolved links are omitted.
"""

from __future__ import annotations

import posixpath
import shutil
import sys
import tarfile
import tempfile
import zipfile
from collections import deque
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20_000
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class TarEntry:
    kind: str
    mode: int
    data: bytes | None = None
    linkname: str | None = None


def safe_archive_path(name: str) -> str:
    """Validate a tar path without allowing normalization to hide traversal."""
    if not name or name.startswith("/") or "\\" in name:
        raise ValueError("Git archive contains an unsafe path")
    trimmed = name[:-1] if name.endswith("/") else name
    parts = trimmed.split("/")
    if not trimmed or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Git archive contains an unsafe path")
    return "/".join(parts)


def normalized_target(parent: str, target: str) -> str:
    if not target or target.startswith("/") or "\\" in target:
        raise ValueError("symlink target is absolute or empty")
    resolved = posixpath.normpath(posixpath.join(parent, target))
    if resolved in {"", ".", ".."} or resolved.startswith("../") or resolved.startswith("/"):
        raise ValueError("symlink target escapes the source root")
    return resolved


def read_tar() -> tuple[dict[str, TarEntry], set[str], int]:
    entries: dict[str, TarEntry] = {}
    directories: set[str] = set()
    expanded_size = 0

    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
        for member in archive:
            if len(entries) + len(directories) >= MAX_ARCHIVE_ENTRIES:
                raise ValueError("Git archive exceeds the source entry limit")
            path = safe_archive_path(member.name)
            if path in entries or path in directories:
                raise ValueError("Git archive contains duplicate paths")

            if member.isdir():
                directories.add(path)
                continue
            if member.isfile():
                expanded_size += member.size
                if member.size < 0 or expanded_size > MAX_EXPANDED_BYTES:
                    raise ValueError("Git archive exceeds the expanded source size limit")
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("Git archive contains an unreadable file")
                data = source.read(member.size + 1)
                if len(data) != member.size:
                    raise ValueError("Git archive contains a truncated file")
                entries[path] = TarEntry("file", member.mode, data=data)
                continue
            if member.issym():
                entries[path] = TarEntry("symlink", member.mode, linkname=member.linkname)
                continue
            raise ValueError("Git archive contains an unsupported file type")

    return entries, directories, expanded_size


def resolve_path(path: str, entries: dict[str, TarEntry], directories: set[str]) -> tuple[str, str]:
    """Resolve symlink components inside the archive; return (kind, path)."""
    pending = deque(path.split("/"))
    resolved: list[str] = []
    links_seen: set[str] = set()

    while pending:
        component = pending.popleft()
        if component in {"", "."}:
            continue
        if component == "..":
            if not resolved:
                raise ValueError("symlink target escapes the source root")
            resolved.pop()
            continue

        candidate = "/".join((*resolved, component))
        entry = entries.get(candidate)
        if entry and entry.kind == "symlink":
            if candidate in links_seen:
                raise ValueError("symlink cycle")
            links_seen.add(candidate)
            target = entry.linkname or ""
            if target.startswith("/") or "\\" in target:
                raise ValueError("symlink target is absolute")
            pending = deque((*resolved, *target.split("/"), *pending))
            resolved = []
            continue

        if entry and entry.kind == "file" and pending:
            raise ValueError("file path is used as a directory")
        resolved.append(component)

    final_path = "/".join(resolved)
    if final_path in entries and entries[final_path].kind == "file":
        return "file", final_path
    if final_path in directories or any(key.startswith(final_path + "/") for key in (*entries, *directories)):
        return "directory", final_path
    raise ValueError("symlink target does not exist in the archive")


def build_zip(entries: dict[str, TarEntry], directories: set[str], output) -> int:
    files = {path: entry for path, entry in entries.items() if entry.kind == "file"}
    symlinks = {path: entry for path, entry in entries.items() if entry.kind == "symlink"}
    output_files: dict[str, tuple[bytes, int]] = {
        path: (entry.data or b"", entry.mode) for path, entry in files.items()
    }
    omitted = 0

    for path, entry in symlinks.items():
        try:
            target = normalized_target(posixpath.dirname(path), entry.linkname or "")
            kind, resolved = resolve_path(target, entries, directories)
            if kind != "file":
                omitted += 1
                continue
            if path in output_files:
                raise ValueError("symlink path conflicts with a regular file")
            target_entry = files[resolved]
            output_files[path] = (target_entry.data or b"", target_entry.mode)
        except ValueError:
            omitted += 1

    if len(output_files) + len(directories) > MAX_ARCHIVE_ENTRIES:
        raise ValueError("Materialized source archive exceeds the entry limit")

    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for directory in sorted(directories):
            info = zipfile.ZipInfo(directory + "/", date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = ((0o040000 | 0o755) << 16) | 0x10
            archive.writestr(info, b"")
        for path in sorted(output_files):
            data, mode = output_files[path]
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = ((0o100000 | (mode & 0o777)) << 16)
            archive.writestr(info, data)

    if omitted:
        print(
            "Warning: omitted {} unsafe, unresolved, or directory symlink(s) from the source snapshot.".format(omitted),
            file=sys.stderr,
        )
    return omitted


def main() -> int:
    try:
        entries, directories, _ = read_tar()
        if not entries:
            raise ValueError("Git archive contains no regular source files")
        with tempfile.TemporaryFile() as output:
            build_zip(entries, directories, output)
            archive_size = output.tell()
            if archive_size > MAX_ARCHIVE_BYTES:
                raise ValueError("Compressed source ZIP exceeds the API upload limit")
            output.seek(0)
            shutil.copyfileobj(output, sys.stdout.buffer, length=CHUNK_SIZE)
        return 0
    except (OSError, tarfile.TarError, ValueError, zipfile.BadZipFile) as error:
        print("Could not package the Git source snapshot: {}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
