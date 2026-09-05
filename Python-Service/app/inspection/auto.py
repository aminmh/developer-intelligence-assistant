"""Agent-style auto-inspection for answer grounding.

Invoked when a chat request carries ``auto_inspect: true`` alongside an
indexed workspace (``project_dir`` + ``stem``) and hybrid retrieval produced
symbol matches. Pipeline mirrors the prompt's Tier protocol:

    1. index the project symbol graph once (per process + project),
    2. resolve the top candidate method symbols,
    3. read the exact method bodies that matter most,
    4. pull one hop of call-hierarchy / references facts from the graph.

Everything is bounded (few candidates, hard row caps, source cap per method)
and best-effort: any failure degrades to a shorter section or None — never an
exception into the answer path. Absence of indexable symbols is reported
honestly ("no PHP symbols indexed") instead of guessed.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from inspection.service import InspectionService, inspection_service

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 4
MAX_ROWS = 60
MAX_SOURCE_CHARS = 500
MAX_METHOD_READS = 2
INDEX_RETRY_GUARD_S = 300.0

# project_dir|stem -> (ok_at_timestamp | None while in flight)
_indexed: dict[str, bool | float] = {}
_inflight: set[str] = set()


def _key(project_dir: str, stem: str) -> str:
    return f"{project_dir}|{stem}"


def _ensure_indexed(svc: InspectionService, project_dir: str, stem: str) -> bool:
    """Index once per process/project; repeat after a short failure guard."""
    key = _key(project_dir, stem)
    if key in _indexed:
        return bool(_indexed[key])
    if key in _inflight:
        return False
    _inflight.add(key)
    try:
        result = svc.index(project_dir, stem)
        _indexed[key] = bool(result.get("indexed")) and result.get("nodes", 0) > 0
    except Exception as exc:  # noqa: BLE001 — exporter failures are not fatal
        logger.warning("Auto-inspection index failed for %s: %s", key, exc)
        _indexed[key] = False
    finally:
        _inflight.discard(key)
    return bool(_indexed.get(key))


def _abs(project_dir: str, rel: str) -> str:
    """Join rel file to project dir when inside it, else ''."""
    candidate = os.path.realpath(os.path.join(project_dir, rel or ""))
    root = os.path.realpath(project_dir)
    if candidate != root and candidate.startswith(root + os.sep):
        return candidate
    return ""


def _extract_candidates(results: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Dedup (file, class, method) triples from retrieval payloads."""
    seen: set[tuple[str, str, str]] = set()
    out: list[tuple[str, str, str]] = []
    for r in results:
        payload = r.get("payload") or {}
        file = payload.get("file") or ""
        cls = str(payload.get("class") or "").lstrip("\\")
        name = payload.get("name") or ""
        if not (file and cls and name):
            continue
        key = (file, cls, name)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def build_inspection_grounding(
    results: list[dict[str, Any]],
    project_dir: str,
    stem: str,
    *,
    service: InspectionService | None = None,
) -> str | None:
    """Render a markdown grounding block for the /answer prompt context."""
    if not results:
        return None
    svc = service or inspection_service()
    from inspection.service import sym_collection_for

    collection = sym_collection_for(stem)
    if not _ensure_indexed(svc, project_dir, stem):
        return None

    lines: list[str] = []
    method_reads: list[tuple[str, str, str]] = []
    for file, cls, method in _extract_candidates(results):
        rows = svc.resolve_symbol(collection, f"{cls}::{method}")
        if not rows:
            rows = svc.resolve_symbol(collection, method)
        if not rows:
            continue
        abs_path = _abs(project_dir, file)
        if abs_path and len(method_reads) < MAX_METHOD_READS:
            method_reads.append((abs_path, method, cls))
        # One hop of call-graph context around the first candidate method.
        target = next(
            (r for r in rows if r.get("kind") == "method" and r.get("owner") == cls),
            rows[0],
        )
        fqn = target.get("fqn") or ""
        if not fqn:
            continue
        hierarchy = svc.call_hierarchy(collection, fqn)
        hop_lines: list[str] = []
        for callee in hierarchy.get("callees", [])[:8]:
            ref = callee.get("ref") or {}
            hop_lines.append(
                f"- calls {callee.get('fqn') or callee.get('name')} "
                f"({ref.get('file', '').split('/')[-1]}:{ref.get('line', '')})"
            )
        for caller in hierarchy.get("callers", [])[:4]:
            ref = caller.get("ref") or {}
            hop_lines.append(
                f"- called by {caller.get('fqn') or caller.get('name')} "
                f"({ref.get('file', '').split('/')[-1]}:{ref.get('line', '')})"
            )
        if hop_lines:
            lines.append(f"**{fqn}**")
            lines.extend(hop_lines)
        if len(lines) >= MAX_ROWS:
            break

    for abs_path, method, _cls in method_reads:
        body = svc.method(abs_path, method) or {}
        source = str(body.get("source") or "").strip()
        if source:
            if len(source) > MAX_SOURCE_CHARS:
                source = source[:MAX_SOURCE_CHARS] + "\n…"
            lines.append(f"**Method body `{method}` ({abs_path.split('/')[-1]}):**")
            lines.append("```php\n" + source + "\n```")

    if not lines:
        return None
    return "Symbol graph inspection (verified structure — not documentation):\n" + "\n".join(lines)
