"""Bounded PHP design signals for evidence-grounded Clean Code assessment.

These deterministic checks identify review candidates from indexed PHP AST
chunks. They do not claim to prove a SOLID violation or whole-program behavior.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import PurePosixPath
import re
import tempfile
from typing import Any

from telemetry.snapshot import Snapshot, validate_project_id, validate_revision_id

DESIGN_ANALYZER_VERSION = "php-solid-design-signals-1"
MAX_DESIGN_CHUNKS = 20_000
MAX_SIGNALS = 30
MAX_CONTEXT_CHARS = 1_400
MAX_AI_REVIEW_CONTEXTS = 8
LOCAL_PARSE_TIMEOUT_SECONDS = 90
_CLASS_TYPES = {"class", "interface", "trait", "enum"}

def _path(payload: dict[str, Any], allowed_files: set[str]) -> str | None:
    value = payload.get("file_path") or payload.get("file")
    if not isinstance(value, str) or not value or len(value) > 300:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        return None
    normalized = path.as_posix()
    return normalized if normalized in allowed_files else None


def _line_number(value: Any) -> int:
    return value if type(value) is int and value > 0 else 1


def build_php_design_signals(
    project_id: str,
    revision_id: str,
    snapshot: dict[str, Any],
    chunks: list[dict[str, Any]],
    business_files: list[str],
    *,
    truncated: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Produce stable, source-cited design review signals from one revision."""
    validate_project_id(project_id)
    validate_revision_id(revision_id)
    try:
        expected_collection = Snapshot(
            project_id, snapshot.get("commit_sha"), snapshot.get("source_archive_sha256")
        ).collection
    except (TypeError, ValueError):
        return _unavailable(revision_id, "The selected revision has invalid source identity metadata."), []
    if (snapshot.get("state") != "ready" or snapshot.get("project_id") != project_id
            or snapshot.get("revision_id") != revision_id
            or snapshot.get("collection_name") != expected_collection):
        return _unavailable(revision_id, "The selected revision does not have a ready matching index."), []
    if not isinstance(business_files, list) or not any(
        isinstance(path, str) and path.lower().endswith((".php", ".php3", ".php4", ".php5", ".phtml"))
        for path in business_files
    ):
        return _unavailable(revision_id, "No inventoried PHP business source files were available."), []

    from code_quality.design_metrics import build_units, load_rubric, select_candidates

    allowed_files = {path for path in business_files if isinstance(path, str)}
    from telemetry.snapshot import source_filter
    identity = {"project_id": project_id, "revision_id": revision_id,
                **source_filter(snapshot),
                "source_archive_sha256": snapshot.get("source_archive_sha256"),
                "dirty": False, "collection": expected_collection}
    matched = [row for row in chunks[:MAX_DESIGN_CHUNKS]
               if isinstance(row, dict) and all(row.get(k) == v for k, v in identity.items())
               and _path(row, allowed_files)]
    if not matched:
        return _unavailable(revision_id, "The selected revision index contains no matching PHP AST chunks."), []
    rubric, _ = load_rubric()
    units = build_units(matched, rubric)
    if not units:
        return _unavailable(revision_id, "The selected revision has no metric-bearing PHP units; re-index it."), []
    candidates, counts = select_candidates(project_id, revision_id, units, rubric)
    findings = []
    for candidate in candidates:
        card = candidate["card"]
        findings.append({
            "evidence_id": candidate["evidence_id"], "kind": "finding", "category": "clean_code",
            "rule": candidate["rule_id"], "file": candidate["file"], "line": candidate["line"],
            "message": f"{candidate['principle']} review candidate: {card['metric']} is {card['value']} (threshold {card['threshold']}).",
            "severity": "INFO", "tool": "php-solid-design-signals", "tool_version": DESIGN_ANALYZER_VERSION,
            "design_principle": candidate["principle"],
            "context_snippet": (card["body"] or card["declaration"])[:MAX_CONTEXT_CHARS],
        })
    partial = truncated or len(chunks) > MAX_DESIGN_CHUNKS
    return {
        "status": "partial" if partial else "complete", "revision_id": revision_id,
        "analyzer_version": DESIGN_ANALYZER_VERSION, "languages": ["php"],
        "principles_with_static_signals": sorted(counts),
        "principles_requiring_semantic_evidence": sorted(counts),
        "chunk_count": len(matched), "class_count": len(units),
        "method_count": sum(len(unit["methods"]) for unit in units),
        "signal_count": len(findings), "signals_truncated": any(c["unjudged"] for c in counts.values()),
        "limits": rubric["limits"], "reason": "Signals are review candidates, not proof of SOLID violations.",
    }, findings


def build_php_design_signals_from_archive(
    project_id: str,
    revision_id: str,
    snapshot: dict[str, Any],
    archive_data: bytes,
    business_files: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    summary, findings, _ = build_php_assessment_evidence_from_archive(
        project_id, revision_id, snapshot, archive_data, business_files,
    )
    return summary, findings


def build_php_assessment_evidence_from_archive(
    project_id: str,
    revision_id: str,
    snapshot: dict[str, Any],
    archive_data: bytes,
    business_files: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build PHP design signals and bounded code excerpts from one archive parse."""
    """Parse the selected archive when its ready index contains no PHP chunks.

    The fallback uses the application-owned read-only parser and does not
    re-embed source or run project-defined commands.
    """
    validate_project_id(project_id)
    validate_revision_id(revision_id)
    try:
        from infra.php_cli import PhpCli, _decode_json_array
        from infra.php_tools import extract_zip_safely
        from index.llamaindex_layer import _payload
        from telemetry.snapshot import prepare_chunks

        commit_sha = snapshot.get("commit_sha")
        archive_hash = snapshot.get("source_archive_sha256")
        if (commit_sha is not None and not isinstance(commit_sha, str)) or not isinstance(archive_hash, str):
            return _unavailable(
                revision_id, "The selected revision has invalid source identity metadata."
            ), [], []

        with tempfile.TemporaryDirectory(prefix="code_quality_php_") as workspace:
            extract_zip_safely(archive_data, workspace)
            result = PhpCli().run(
                "code-parse", ["--path", workspace, "--output", "json"],
                timeout=LOCAL_PARSE_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                return _unavailable(
                    revision_id,
                    "The selected revision's bounded PHP parser could not complete.",
                ), [], []
            parsed_chunks = _decode_json_array(result.stdout)
            if not parsed_chunks:
                return _unavailable(
                    revision_id,
                    "The selected revision's PHP parser returned no AST chunks.",
                ), [], []

            source_snapshot = Snapshot(
                project_id, commit_sha, archive_hash, revision_id=revision_id,
            )
            prepared_chunks = prepare_chunks(parsed_chunks, source_snapshot)
            payloads = [_payload(chunk, source_snapshot.collection) for chunk in prepared_chunks]
            summary, findings = build_php_design_signals(
                project_id, revision_id, snapshot, payloads, business_files,
                truncated=len(payloads) > MAX_DESIGN_CHUNKS,
            )
            review_context = _build_ai_review_context(
                project_id, revision_id, payloads, business_files,
            )
            summary["source"] = "local-php-ast-fallback"
            summary["reason"] = (
                "The revision's indexed collection had no usable PHP AST chunks; "
                "the bounded local parser supplied this revision's source evidence. "
                "Signals remain review candidates, not proof of SOLID violations."
            )
            return summary, findings, review_context
    except Exception:
        return _unavailable(
            revision_id,
            "The selected revision's bounded PHP source parser could not be used.",
        ), [], []


def _build_ai_review_context(
    project_id: str,
    revision_id: str,
    payloads: list[dict[str, Any]],
    business_files: list[str],
) -> list[dict[str, Any]]:
    """Select deterministic, revision-scoped PHP excerpts for semantic review."""
    allowed_files = {path for path in business_files if isinstance(path, str)}
    candidates: list[tuple[int, str, int, str]] = []
    for payload in payloads[:MAX_DESIGN_CHUNKS]:
        file = _path(payload, allowed_files)
        node_type = str(payload.get("node_type") or payload.get("type") or "").casefold()
        code = payload.get("code")
        line = _line_number(payload.get("start_line") or payload.get("line"))
        if file is None or node_type not in _CLASS_TYPES | {"method", "class_method", "classmethod"}:
            continue
        if not isinstance(code, str) or len(code.strip()) < 80:
            continue
        snippet = code.strip()[:MAX_CONTEXT_CHARS]
        # Prefer excerpts with enough implementation detail for review while
        # keeping ordering stable and independent of parser output order.
        complexity = sum(len(re.findall(pattern, snippet, re.IGNORECASE)) for pattern in (
            r"\bif\b", r"\belseif\b", r"\bswitch\b", r"\bforeach\b", r"\bwhile\b",
            r"\bcatch\b", r"\breturn\b", r"\bnew\b",
        ))
        candidates.append((complexity, file, line, snippet))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
    selected: list[tuple[int, str, int, str]] = []
    selected_files: set[str] = set()
    for candidate in candidates:
        if candidate[1] not in selected_files:
            selected.append(candidate)
            selected_files.add(candidate[1])
            if len(selected) == MAX_AI_REVIEW_CONTEXTS:
                break
    if len(selected) < MAX_AI_REVIEW_CONTEXTS:
        selected_ids = {(row[1], row[2]) for row in selected}
        for candidate in candidates:
            if (candidate[1], candidate[2]) in selected_ids:
                continue
            selected.append(candidate)
            selected_ids.add((candidate[1], candidate[2]))
            if len(selected) == MAX_AI_REVIEW_CONTEXTS:
                break
    evidence = []
    for _, file, line, snippet in selected:
        digest = sha256(f"{file}:{line}:{snippet}".encode("utf-8")).hexdigest()
        evidence_id = "ev_" + sha256(
            f"{project_id}:{revision_id}:php-clean-code-context-1:{digest}".encode("utf-8")
        ).hexdigest()[:16]
        evidence.append({
            "evidence_id": evidence_id,
            "kind": "semantic_context",
            "category": "clean_code",
            "rule": "php-clean-code-context-1",
            "file": file,
            "line": line,
            "message": "Revision-matched PHP excerpt supplied as AI review context; this excerpt alone is not a finding.",
            "context_snippet": snippet,
        })
    return evidence


def _unavailable(revision_id: str, reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable", "revision_id": revision_id,
        "analyzer_version": DESIGN_ANALYZER_VERSION, "languages": ["php"],
        "principles_with_static_signals": [], "principles_requiring_semantic_evidence": [],
        "chunk_count": 0, "class_count": 0, "method_count": 0, "signal_count": 0,
        "signals_truncated": False,
        "limits": {"chunks": MAX_DESIGN_CHUNKS, "signals": MAX_SIGNALS,
                   "context_chars": MAX_CONTEXT_CHARS},
        "reason": reason,
    }
