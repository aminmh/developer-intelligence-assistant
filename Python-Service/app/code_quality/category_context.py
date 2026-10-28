"""Select bounded, revision-pinned source excerpts for every quality category."""
from __future__ import annotations

from hashlib import sha256
from pathlib import PurePosixPath
import re
from typing import Any, Iterable
import zipfile
from io import BytesIO

from telemetry.snapshot import validate_project_id, validate_revision_id

MAX_EXCERPT_CHARS = 1_200
WINDOW_LINES = 28

_CATEGORY_HINTS = {
    "security": re.compile(
        r"\b(auth|authoriz|permission|policy|csrf|middleware|request|input|query|sql|exec|password|token|upload|validate|saniti[sz]e|escape|jwt)\w*\b",
        re.IGNORECASE,
    ),
    "clean_code": re.compile(
        r"\b(class|interface|trait|function|def|public|private|protected|new|instanceof|implements|extends)\b",
        re.IGNORECASE,
    ),
    "maintainability": re.compile(
        r"\b(class|interface|function|def|import|use|extends|implements|switch|match|TODO|FIXME)\b",
        re.IGNORECASE,
    ),
    "reliability": re.compile(
        r"\b(try|catch|except|throw|raise|retry|transaction|rollback|finally|assert|optional|timeout)\b|\b(null|None|nil)\b",
        re.IGNORECASE,
    ),
    "performance": re.compile(
        r"\b(for|foreach|while|select|query|fetch|paginate|collection|cache|batch|limit|cursor)\b|\.(map|filter|reduce)\s*\(",
        re.IGNORECASE,
    ),
}


def _safe_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 300 or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix()


def _excerpt(lines: list[str], anchor: int) -> tuple[int, str]:
    start = max(0, anchor - WINDOW_LINES // 4)
    end = min(len(lines), start + WINDOW_LINES)
    start = max(0, end - WINDOW_LINES)
    while start < end and not lines[start].strip():
        start += 1
    text = "\n".join(lines[start:end]).rstrip()
    while len(text) > MAX_EXCERPT_CHARS and end - start > 1:
        if end - anchor > anchor - start and end > anchor + 1:
            end -= 1
        else:
            start += 1
        text = "\n".join(lines[start:end]).rstrip()
    text = text[:MAX_EXCERPT_CHARS]
    return start + 1, text


def build_category_context(
    project_id: str,
    revision_id: str,
    archive_data: bytes,
    business_files: list[str],
    categories: Iterable[str],
    *,
    max_file_bytes: int,
    max_scan_bytes: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Choose one useful excerpt per requested category without treating it as a finding."""
    validate_project_id(project_id)
    validate_revision_id(revision_id)
    requested = list(dict.fromkeys(category for category in categories if category in _CATEGORY_HINTS))
    best: dict[str, tuple[int, str, int, str]] = {}
    allowed = {path for item in business_files if (path := _safe_path(item)) is not None}
    scanned_files = 0
    scanned_bytes = 0

    if archive_data and allowed:
        try:
            with zipfile.ZipFile(BytesIO(archive_data)) as archive:
                entries = {info.filename: info for info in archive.infolist() if not info.is_dir()}
                for file in sorted(allowed):
                    info = entries.get(file)
                    if (info is None or info.file_size > max_file_bytes
                            or scanned_bytes + info.file_size > max_scan_bytes):
                        continue
                    source = archive.read(info).decode("utf-8", errors="replace")
                    scanned_files += 1
                    scanned_bytes += info.file_size
                    lines = source.splitlines()
                    if not lines:
                        continue
                    for category in requested:
                        hint = _CATEGORY_HINTS[category]
                        anchors = [index for index, line in enumerate(lines) if hint.search(line)]
                        if not anchors:
                            anchors = [next((index for index, line in enumerate(lines) if line.strip()), 0)]
                        local_best = None
                        for anchor in anchors:
                            start = max(0, anchor - WINDOW_LINES // 4)
                            end = min(len(lines), start + WINDOW_LINES)
                            window = "\n".join(lines[start:end])
                            relevance = len(hint.findall(window))
                            structure = len(re.findall(r"\b(if|else|for|foreach|while|try|catch|return)\b", window, re.I))
                            score = relevance * 10 + min(structure, 12)
                            excerpt_line, snippet = _excerpt(lines, anchor)
                            candidate = (score, file, excerpt_line, snippet)
                            if local_best is None or (-score, file, excerpt_line) < (-local_best[0], local_best[1], local_best[2]):
                                local_best = candidate
                        if local_best and (category not in best or
                                           (-local_best[0], local_best[1], local_best[2]) <
                                           (-best[category][0], best[category][1], best[category][2])):
                            best[category] = local_best
        except (OSError, ValueError, zipfile.BadZipFile):
            best.clear()

    evidence = []
    for category in requested:
        candidate = best.get(category)
        if not candidate:
            continue
        _, file, line, snippet = candidate
        digest = sha256(f"{category}:{file}:{line}:{snippet}".encode("utf-8")).hexdigest()
        evidence_id = "ev_" + sha256(
            f"{project_id}:{revision_id}:local-category-context-1:{digest}".encode("utf-8")
        ).hexdigest()[:16]
        evidence.append({
            "evidence_id": evidence_id,
            "kind": "semantic_context",
            "category": category,
            "rule": "local-category-context-1",
            "file": file,
            "line": line,
            "message": f"Revision-pinned {category.replace('_', ' ')} source sample; context for scoring, not a finding.",
            "context_snippet": snippet,
        })

    counts = {category: sum(1 for row in evidence if row["category"] == category)
              for category in _CATEGORY_HINTS}
    return ({
        "status": "available" if len(evidence) == len(requested) else "partial" if evidence else "unavailable",
        "source": "revision-source-archive",
        "excerpt_count": len(evidence),
        "categories": counts,
        "scanned_files": scanned_files,
        "scanned_bytes": scanned_bytes,
        "reason": "Source excerpts are representative review context, not findings; static samples do not establish runtime behavior.",
    }, evidence)
