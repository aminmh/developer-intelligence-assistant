"""Bounded source windows from immutable Code Quality revision archives."""
from __future__ import annotations

import io
from pathlib import PurePosixPath
import zipfile

from code_quality.service import (
    LANGUAGE_BY_SUFFIX,
    MAX_ARCHIVE_ENTRIES,
    MAX_FILE_BYTES,
    _is_excluded_source_path,
    _safe_archive_path,
)
from telemetry.snapshot import Snapshot, source_filter

SOURCE_WINDOW_LINES = 100
SOURCE_WINDOW_CONTEXT = 35
MAX_SOURCE_DIRECTORIES = 20_000
MAX_SOURCE_DIRECTORY_BYTES = 2_000_000
MAX_INDEXED_DIRECTORY_CHUNKS = 50_000
# Directory browsing reads ZIP metadata only; it does not extract source files.
# Keep this limit separate from the smaller assessment/model input limit.
MAX_DIRECTORY_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_SOURCE_PREVIEW_ARCHIVE_BYTES = 256 * 1024 * 1024


def _is_archive_metadata_path(path: str) -> bool:
    return path == "__MACOSX" or path.startswith("__MACOSX/")


def list_source_directories(archive_data: bytes) -> list[str]:
    """Return bounded, safe directory paths only from one immutable source archive."""
    if not archive_data or len(archive_data) > MAX_DIRECTORY_ARCHIVE_BYTES:
        raise SourceViewError("The selected revision source archive is empty or exceeds the directory preview limit.")
    directories: set[str] = set()
    directory_bytes = 0

    def add_directory(path: str) -> None:
        nonlocal directory_bytes
        if path in directories:
            return
        directory_bytes += len(path.encode("utf-8")) + 1
        if len(directories) >= MAX_SOURCE_DIRECTORIES or directory_bytes > MAX_SOURCE_DIRECTORY_BYTES:
            raise SourceViewError("The source directory list exceeds the display limit.")
        directories.add(path)

    try:
        with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise SourceViewError("The source archive exceeds the file limit.")
            for entry in entries:
                relative = _safe_archive_path(entry.filename.rstrip("/"))
                if not relative or _is_archive_metadata_path(relative) or len(relative) > 1024:
                    continue
                if not entry.is_dir():
                    mode = (entry.external_attr >> 16) & 0o170000
                    if mode == 0o120000:
                        continue
                path = PurePosixPath(relative)
                parents = path.parents if entry.is_dir() else path.parents
                for parent in parents:
                    value = parent.as_posix()
                    if value != ".":
                        add_directory(value)
                if entry.is_dir():
                    add_directory(relative)
    except SourceViewError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise SourceViewError("The selected revision source archive is invalid.") from exc
    return sorted(directories, key=lambda value: (value.count("/"), value.casefold(), value))


def list_indexed_source_directories(
    project_id: str,
    revision_id: str,
    snapshot: dict,
) -> list[str]:
    """Return directories from the selected revision's indexed source paths.

    This fallback is used when a valid archive is larger than the bounded preview
    limit. It never searches another revision or a retained embedding generation.
    """
    expected_collection = Snapshot(
        project_id,
        snapshot.get("commit_sha"),
        snapshot.get("source_archive_sha256"),
    ).collection
    from index.embedding_rebuild import active_vector_collection
    vector_collection = active_vector_collection(expected_collection)
    if vector_collection is None:
        raise SourceViewError("The selected revision's active vector index is unavailable. Re-index it before browsing folders.")

    from index.llamaindex_layer import CodePropertyGraph
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    identity = {
        "project_id": project_id,
        "revision_id": revision_id,
        **source_filter(snapshot),
        "source_archive_sha256": snapshot.get("source_archive_sha256"),
        "dirty": False,
        "collection": vector_collection,
    }
    try:
        qdrant = CodePropertyGraph()._get_qdrant()
        if not qdrant.collection_exists(vector_collection):
            raise SourceViewError("The selected revision's active vector index is unavailable. Re-index it before browsing folders.")
        conditions = Filter(must=[
            FieldCondition(key=key, match=MatchValue(value=value))
            for key, value in identity.items()
        ])
        paths: set[str] = set()
        offset = None
        while len(paths) < MAX_INDEXED_DIRECTORY_CHUNKS:
            records, offset = qdrant.scroll(
                collection_name=vector_collection,
                scroll_filter=conditions,
                limit=min(256, MAX_INDEXED_DIRECTORY_CHUNKS - len(paths)),
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for record in records:
                payload = record.payload or {}
                path = payload.get("file_path") or payload.get("file")
                if isinstance(path, str):
                    normalized = _safe_archive_path(path)
                    if normalized and not _is_archive_metadata_path(normalized):
                        paths.add(normalized)
            if offset is None:
                break
        if not paths:
            raise SourceViewError("No indexed source paths are available for this revision.")
    except SourceViewError:
        raise
    except Exception as exc:
        raise SourceViewError("The indexed source paths could not be read for this revision.") from exc

    directories: set[str] = set()
    directory_bytes = 0
    for path_name in paths:
        path = PurePosixPath(path_name)
        for parent in path.parents:
            value = parent.as_posix()
            if value == "." or value in directories:
                continue
            directory_bytes += len(value.encode("utf-8")) + 1
            if len(directories) >= MAX_SOURCE_DIRECTORIES or directory_bytes > MAX_SOURCE_DIRECTORY_BYTES:
                raise SourceViewError("The source directory list exceeds the display limit.")
            directories.add(value)
    return sorted(directories, key=lambda value: (value.count("/"), value.casefold(), value))


class SourceViewError(ValueError):
    """A source file cannot safely be shown from the selected revision."""


def read_source_window(
    archive_data: bytes,
    file_path: str,
    line: int,
    start_line: int | None = None,
) -> dict:
    """Return at most 100 line-numbered source lines around the cited line."""
    if not isinstance(file_path, str) or not file_path or len(file_path) > 1024 or any(ord(char) < 32 for char in file_path):
        raise SourceViewError("Select a valid source file.")
    normalized = _safe_archive_path(file_path)
    if normalized != file_path:
        raise SourceViewError("The requested source path is invalid.")
    path = PurePosixPath(normalized)
    if (not path.suffix or path.suffix.lower() not in LANGUAGE_BY_SUFFIX
            or _is_excluded_source_path(normalized)):
        raise SourceViewError("This file is not an assessable business source file.")
    if type(line) is not int or line < 1:
        raise SourceViewError("Select a valid source line.")
    if start_line is not None and (type(start_line) is not int or start_line < 1):
        raise SourceViewError("Select a valid source window.")
    if not archive_data or len(archive_data) > MAX_SOURCE_PREVIEW_ARCHIVE_BYTES:
        raise SourceViewError("The selected revision source archive is empty or exceeds the preview limit.")

    try:
        with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise SourceViewError("The source archive exceeds the file limit.")
            matches = [entry for entry in entries if entry.filename == normalized and not entry.is_dir()]
            if len(matches) != 1:
                raise SourceViewError("The cited source file is unavailable in this revision.")
            entry = matches[0]
            if entry.file_size > MAX_FILE_BYTES:
                raise SourceViewError("The cited source file exceeds the preview size limit.")
            source = archive.read(entry).decode("utf-8", errors="replace")
    except SourceViewError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise SourceViewError("The selected revision source archive is invalid.") from exc

    source_lines = source.splitlines()
    total_lines = len(source_lines)
    if start_line is None:
        first_line = max(1, line - SOURCE_WINDOW_CONTEXT)
    else:
        first_line = min(start_line, max(1, total_lines))
    last_line = min(total_lines, first_line + SOURCE_WINDOW_LINES - 1)
    selected = source_lines[first_line - 1:last_line]
    return {
        "file": normalized,
        "requested_line": line,
        "total_lines": total_lines,
        "start_line": first_line,
        "end_line": last_line,
        "has_previous": first_line > 1,
        "has_next": last_line < total_lines,
        "lines": [
            {"number": first_line + offset, "text": text}
            for offset, text in enumerate(selected)
        ],
    }
