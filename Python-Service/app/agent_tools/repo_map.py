"""Ranked repository map: which files and symbols the rest of the project calls most.

Built from the indexer's chunks (any producer), using the same canonical symbol
matching as strict graph ingest, so a call counts only when its target is a
definition in this project. A symbol ranks by its callers in other files, then by
all callers (so a class calling its own helpers does not outrank code the rest of
the project depends on); a file ranks by the sum over its symbols. Stored per revision, keyed by the
source archive checksum, under ``REPO_MAP_DIR`` (default ``/app/data/repo_maps``).
Revisions indexed before maps existed get one built from source on first use.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from telemetry.symbols import from_chunk, normalize

MAP_VERSION = 2
_PSEUDO_OWNERS = ("__unknown", "__function", "__global__")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_SYMBOLS_PER_FILE = 8
_MAX_FILES = 400


def map_dir() -> Path:
    return Path(os.environ.get("REPO_MAP_DIR", "/app/data/repo_maps"))


def _target_id(target: Any) -> str | None:
    if not isinstance(target, str) or not target.strip():
        return None
    try:
        if "::" in target:
            owner, method = target.split("::", 1)
            if owner in _PSEUDO_OWNERS:
                return normalize({"kind": "function", "name": method})
            return normalize({"kind": "method", "class": owner, "method": method})
        return normalize({"kind": "function", "name": target})
    except ValueError:
        return None


def _call_values(calls: Any) -> list[Any]:
    if isinstance(calls, dict):
        return [v for values in calls.values() for v in (values if isinstance(values, list) else [values])]
    return list(calls) if isinstance(calls, (list, tuple)) else []


def build_repo_map(chunks: list[dict[str, Any]], *, producer: str | None = None) -> dict[str, Any]:
    """Rank files and symbols by how many distinct definitions call them."""
    ids: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        try:
            symbol = from_chunk(chunk)
        except ValueError:
            symbol = None
        if symbol and symbol not in ids:
            ids[symbol] = chunk
    callers: dict[str, set[str]] = defaultdict(set)
    external: dict[str, set[str]] = defaultdict(set)
    calls_out: dict[str, int] = defaultdict(int)
    path_of = lambda c: str(c.get("file_path") or c.get("file") or "")  # noqa: E731
    for symbol, chunk in ids.items():
        for target in {t for t in map(_target_id, _call_values(chunk.get("calls"))) if t}:
            if target in ids and target != symbol:
                callers[target].add(symbol)
                if path_of(ids[target]) != path_of(chunk):
                    external[target].add(symbol)
                calls_out[symbol] += 1

    files: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        path = str(chunk.get("file_path") or chunk.get("file") or "")
        if not path:
            continue
        entry = files.setdefault(path, {"path": path, "score": 0, "callers": 0, "symbols": []})
        try:
            symbol = from_chunk(chunk)
        except ValueError:
            symbol = None
        incoming = len(callers.get(symbol, ())) if symbol else 0
        outside = len(external.get(symbol, ())) if symbol else 0
        entry["score"] += outside
        entry["callers"] += incoming
        entry["symbols"].append({
            "name": chunk.get("name"),
            "kind": chunk.get("definition_kind") or chunk.get("node_type") or chunk.get("type"),
            "owner": chunk.get("class") if (chunk.get("node_type") or chunk.get("type")) == "method" else None,
            "line": chunk.get("start_line"),
            "callers": incoming,
            "callers_elsewhere": outside,
            "calls": calls_out.get(symbol, 0) if symbol else 0,
        })
    ranked = sorted(files.values(),
                    key=lambda f: (-f["score"], -f["callers"], -len(f["symbols"]), f["path"]))[:_MAX_FILES]
    for entry in ranked:
        entry["symbol_count"] = len(entry["symbols"])
        entry["symbols"] = sorted(entry["symbols"],
                                  key=lambda s: (-s["callers_elsewhere"], -s["callers"], -s["calls"], s["line"] or 0))[
            :_MAX_SYMBOLS_PER_FILE]
    return {"version": MAP_VERSION, "producer": producer, "symbols": len(ids),
            "edges": sum(len(v) for v in callers.values()), "files": ranked}


def store_repo_map(archive_sha256: str | None, repo_map: dict[str, Any]) -> None:
    """Persist a map for one revision; silently skips revisions without a verified archive."""
    if not archive_sha256 or not _SHA256.match(archive_sha256):
        return
    directory = map_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{archive_sha256}.json"
    staging = target.with_suffix(".tmp")
    staging.write_text(json.dumps(repo_map), encoding="utf-8")
    os.replace(staging, target)


def load_repo_map(archive_sha256: str | None) -> dict[str, Any] | None:
    if not archive_sha256 or not _SHA256.match(archive_sha256):
        return None
    try:
        data = json.loads((map_dir() / f"{archive_sha256}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("version") == MAP_VERSION else None


def map_for_source(path: Path, archive_sha256: str | None) -> dict[str, Any]:
    """The stored map for a revision, building and storing it from source when missing."""
    cached = load_repo_map(archive_sha256)
    if cached is not None:
        return {**cached, "built": "stored"}
    from projects.lang_index import produce_chunks

    result = produce_chunks(str(path))
    if result.get("error") and not result.get("chunks"):
        return {"version": MAP_VERSION, "files": [], "symbols": 0, "edges": 0, "error": result["error"]}
    repo_map = build_repo_map(result.get("chunks") or [], producer=result.get("producer"))
    try:
        store_repo_map(archive_sha256, repo_map)
    except OSError:
        pass  # an unwritable store only costs a rebuild next time
    return {**repo_map, "built": "from_source"}
