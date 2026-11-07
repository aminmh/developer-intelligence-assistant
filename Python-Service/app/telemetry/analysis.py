"""Stateless, revision-scoped Runtime request analysis and diff previews."""
from __future__ import annotations

import difflib
import json
import logging
import re
import zipfile
from io import BytesIO
from typing import Any, Callable

from telemetry.symbols import relative_path

MAX_SOURCE_FILES = 8
MAX_SOURCE_FILE_BYTES = 64 * 1024
MAX_SOURCE_CONTEXT_CHARS = 24_000
MAX_MODEL_RESPONSE_CHARS = 80_000
MAX_DIFF_CHARS = 40_000
MAX_DATABASE_GROUPS = 15
FINDING_CATEGORIES = {"database", "methods", "profile", "general"}
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:password|passwd|secret|token|api[_-]?key|client[_-]?secret|private[_-]?key)\b\s*[:=]\s*)(?:['\"][^'\"]{3,}['\"]|[^\s,;]{8,})"
)
_CREDENTIAL_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
_PRIVATE_KEY_BLOCK = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)


logger = logging.getLogger(__name__)


def _log_model_failure(stage: str, provider: str, exc: Exception) -> None:
    """Record why the model call failed (status, host, connection error), never the prompt."""
    causes, current = [], exc
    while current is not None and len(causes) < 4:
        causes.append(f"{type(current).__name__}: {str(current)[:300]}")
        current = current.__cause__ or current.__context__
    logger.warning("Runtime %s model call failed (provider=%s): %s", stage, provider, " <- ".join(causes))


class AnalysisError(ValueError):
    """A safe, client-actionable Runtime analysis error."""


def _redact_source(source: str) -> str:
    source = _CREDENTIAL_ASSIGNMENT.sub(r"\1[REDACTED]", source)
    source = _CREDENTIAL_BEARER.sub("Bearer [REDACTED]", source)
    return _PRIVATE_KEY_BLOCK.sub("[REDACTED PRIVATE KEY]", source)


def _parse_model_json(content: str) -> dict:
    if not isinstance(content, str) or len(content) > MAX_MODEL_RESPONSE_CHARS:
        raise AnalysisError("The configured model returned an invalid response. Retry the analysis.")
    candidate = content.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I)
    try:
        result = json.loads(candidate)
    except (TypeError, ValueError) as exc:
        raise AnalysisError("The configured model returned invalid structured data. Retry the analysis.") from exc
    if not isinstance(result, dict):
        raise AnalysisError("The configured model returned invalid structured data. Retry the analysis.")
    return result


def _definition_map(definitions: list[dict]) -> dict[str, dict]:
    result = {}
    for item in definitions:
        symbol_id = item.get("symbol_id")
        file = item.get("file")
        line = item.get("line")
        try:
            safe_file = relative_path(file) if isinstance(file, str) else ""
        except ValueError:
            continue
        if (isinstance(symbol_id, str) and safe_file
                and isinstance(line, int) and line > 0):
            result[symbol_id] = {
                "symbol_id": symbol_id,
                "name": str(item.get("fqn") or item.get("name") or symbol_id)[:300],
                "file": safe_file,
                "line": line,
                "end_line": item.get("end_line") if isinstance(item.get("end_line"), int) else line,
            }
    return result


def _source_context(archive: bytes, definitions: dict[str, dict], symbol_ids: list[str]) -> dict[str, dict]:
    paths = []
    for symbol_id in symbol_ids:
        definition = definitions.get(symbol_id)
        if definition and definition["file"] not in paths:
            paths.append(definition["file"])
    paths = paths[:MAX_SOURCE_FILES]
    extracted: dict[str, str] = {}
    try:
        with zipfile.ZipFile(BytesIO(archive)) as bundle:
            for path in paths:
                try:
                    info = bundle.getinfo(path)
                    if info.file_size > MAX_SOURCE_FILE_BYTES:
                        continue
                    extracted[path] = _redact_source(bundle.read(path).decode("utf-8", errors="replace"))
                except KeyError:
                    continue
    except (zipfile.BadZipFile, OSError) as exc:
        raise AnalysisError("Indexed source is unavailable for this revision.") from exc
    output: dict[str, dict] = {}
    budget = MAX_SOURCE_CONTEXT_CHARS
    for symbol_id in symbol_ids:
        definition = definitions.get(symbol_id)
        if not definition or definition["file"] not in extracted or budget <= 0:
            continue
        lines = extracted[definition["file"]].splitlines()
        start = max(1, definition["line"] - 12)
        end = min(len(lines), max(definition["line"], definition["end_line"]) + 12)
        text = "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1))
        text = text[:budget]
        budget -= len(text)
        output[symbol_id] = {**definition, "source": text}
    return output


def _database_evidence(detail: dict) -> dict | None:
    """The grouped database picture: counts, time share, the slowest query and the same query repeated. No SQL."""
    summary = detail.get("database_summary")
    if not isinstance(summary, dict) or not summary.get("query_count"):
        return None
    slowest = summary.get("slowest") if isinstance(summary.get("slowest"), dict) else None
    groups = []
    for group in summary.get("groups", [])[:MAX_DATABASE_GROUPS]:
        if isinstance(group, dict):
            groups.append({key: group.get(key) for key in (
                "operation", "table_ids", "symbol_id", "caller_symbol_ids", "count", "total_ms", "max_ms", "repeated", "framework",
            ) if group.get(key) is not None})
    return {
        "query_count": summary.get("query_count"),
        "total_ms": summary.get("total_ms"),
        "app_query_count": summary.get("app_query_count"),
        "app_total_ms": summary.get("app_total_ms"),
        "framework_query_count": summary.get("framework_query_count"),
        "framework_total_ms": summary.get("framework_total_ms"),
        "request_share": summary.get("request_share"),
        "repeated_min": summary.get("repeated_min"),
        "repeated_groups": summary.get("repeated_groups"),
        "slowest": {key: slowest.get(key) for key in ("operation", "table_ids", "symbol_id", "duration_ms", "framework")
                    if slowest.get(key) is not None} if slowest else None,
        "groups": groups,
    }


def _database_symbols(detail: dict) -> list[str]:
    """Methods behind repeated queries come first, so their source fits the context budget."""
    summary = detail.get("database_summary") if isinstance(detail.get("database_summary"), dict) else {}
    groups = [group for group in summary.get("groups", []) if isinstance(group, dict)]
    ordered = [group for group in groups if group.get("repeated")] + [group for group in groups if not group.get("repeated")]
    symbols: list[str] = []
    for group in ordered:
        for value in [group.get("symbol_id"), *(group.get("caller_symbol_ids") or [])]:
            if isinstance(value, str) and value not in symbols:
                symbols.append(value)
    return symbols


def _evidence(detail: dict, source: dict[str, dict]) -> tuple[dict, list[str]]:
    request = detail.get("request") or {}
    database_calls = []
    for call in detail.get("database_calls", [])[:100]:
        database_calls.append({key: call.get(key) for key in (
            "duration_ms", "db_system", "db_namespace", "operation", "table_ids", "symbol_id", "caller_symbol_id", "span_status",
        ) if call.get(key) is not None})
    activity = detail.get("method_activity") or {}
    method_edges = []
    for edge in activity.get("edges", [])[:100] if isinstance(activity, dict) else []:
        method_edges.append({key: edge.get(key) for key in ("caller_symbol_id", "symbol_id", "calls", "source") if edge.get(key) is not None})
    summary = detail.get("ai_summary") or {}
    profile = [{"symbol_id": item.get("frame"), "sample_percent": item.get("sample_percentage"),
                "event_type": summary.get("profile_event_type")}
               for item in summary.get("excimer_hotspots", [])[:50]
               if isinstance(item, dict) and isinstance(item.get("frame"), str)]
    gaps = []
    if not method_edges:
        gaps.append("No per-request method-hook counts are available; this does not prove that no methods ran.")
    if not database_calls:
        gaps.append("No database spans were recorded for this sampled request.")
    if not profile:
        gaps.append("No retained Excimer profile is available for this request.")
    if not source:
        gaps.append("Exact-revision source mapping is unavailable; code-change findings cannot be diffed.")
    payload = {
        "request": {key: request.get(key) for key in ("method", "route", "status_code", "duration_ms", "php_cpu_time_us", "php_peak_memory_bytes") if request.get(key) is not None},
        "database_summary": _database_evidence(detail),
        "database_calls": database_calls,
        "method_activity": method_edges,
        "excimer_samples": profile,
        "php_estimates": {key: summary.get(key) for key in ("php_app_time_ms", "php_cpu_time_ms", "php_peak_memory_bytes") if summary.get(key) is not None},
        "source": list(source.values()),
    }
    return payload, gaps


def _validate_findings(result: dict, evidence: dict, gaps: list[str]) -> dict:
    refs = {edge.get("symbol_id") for edge in evidence["method_activity"]}
    refs |= {edge.get("caller_symbol_id") for edge in evidence["method_activity"]}
    refs |= {call.get("symbol_id") for call in evidence["database_calls"]}
    refs |= {call.get("caller_symbol_id") for call in evidence["database_calls"]}
    refs |= {item.get("symbol_id") for item in evidence["excimer_samples"]}
    for group in (evidence.get("database_summary") or {}).get("groups", []):
        refs.add(group.get("symbol_id"))
        refs |= set(group.get("caller_symbol_ids") or [])
    refs.discard(None)
    source_ids = {item["symbol_id"] for item in evidence["source"]}
    raw = result.get("findings")
    if not isinstance(raw, list) or len(raw) > 12:
        raise AnalysisError("The configured model returned invalid findings. Retry the analysis.")
    findings = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise AnalysisError("The configured model returned an invalid finding. Retry the analysis.")
        title, summary = item.get("title"), item.get("summary")
        recs = item.get("recommendations", [])
        evidence_refs, source_refs = item.get("evidence_refs", []), item.get("source_refs", [])
        if (not isinstance(title, str) or not isinstance(summary, str) or not isinstance(recs, list)
                or not isinstance(evidence_refs, list) or not isinstance(source_refs, list)):
            raise AnalysisError("The configured model returned invalid finding fields. Retry the analysis.")
        evidence_refs = [ref for ref in evidence_refs if isinstance(ref, str) and ref in refs]
        source_refs = [ref for ref in source_refs if isinstance(ref, str) and ref in source_ids]
        findings.append({
            "id": f"finding-{index + 1}",
            "title": title[:180],
            "summary": summary[:1000],
            "recommendations": [text[:500] for text in recs if isinstance(text, str)][:5],
            "evidence_refs": evidence_refs[:20],
            "source_refs": source_refs[:8],
            "severity": item.get("severity") if isinstance(item.get("severity"), str)
            and item.get("severity") in {"low", "medium", "high"} else "informational",
            "category": item.get("category") if item.get("category") in FINDING_CATEGORIES else "general",
        })
    coverage = result.get("coverage_gaps")
    if coverage is not None and not isinstance(coverage, list):
        raise AnalysisError("The configured model returned invalid coverage gaps. Retry the analysis.")
    model_gaps = [value[:400] for value in coverage if isinstance(value, str)] if isinstance(coverage, list) else []
    return {"findings": findings, "coverage_gaps": list(dict.fromkeys(gaps + model_gaps))}


ANALYSIS_TASK = (
    "Analyze this sampled PHP request. Return JSON with findings[], each with title, summary, recommendations[], "
    "severity, category (database, methods, profile or general), evidence_refs[] (symbol IDs from supplied evidence), "
    "source_refs[] (source symbol IDs); and coverage_gaps[]. Be specific, cite only supplied evidence, distinguish "
    "observations, estimates, samples, and inferences. Do not invent facts. "
    "Database: database_summary groups queries by operation, tables and the issuing method (symbol_id; caller_symbol_ids "
    "are the methods that called it). A group marked repeated ran at least repeated_min times in this one request. "
    "Queries in one group can still differ in their WHERE values, because SQL text is not collected. For each repeated "
    "group from application code, and for a slow query or a high request_share, give a finding with category database "
    "that names the issuing method, states the likely cause (a lookup inside a loop or N+1 relation loading, a value "
    "read again instead of kept for the request, or framework work such as sessions, cache or debugging tools) as an "
    "inference, and recommends a concrete change in that method when its source is supplied. Groups with no symbol_id "
    "come from the framework, not application code; say so rather than suggesting an application change. Groups marked "
    "framework are the framework's own work on its tables (sessions, cache, queues, debugging tools such as Telescope); "
    "mention them only as configuration (for example a debugging tool left on, or the session or cache driver), never as "
    "an application code change."
)


def analyze_request(
    project_id: str,
    revision: dict,
    detail: dict,
    provider: str,
    *,
    definitions_loader: Callable[[str, str], list[dict]],
    archive_loader: Callable[[str], bytes],
    model_call: Callable[..., Any] | None = None,
) -> dict:
    if provider not in {"local", "remote"}:
        raise AnalysisError("Choose a valid configured chat provider and retry.")
    if not detail.get("available") or not detail.get("request"):
        raise AnalysisError("Request telemetry is unavailable for analysis.")
    commit_sha = revision.get("commit_sha")
    if not isinstance(commit_sha, str):
        raise AnalysisError("Indexed revision source is unavailable.")
    try:
        definitions = _definition_map(definitions_loader(project_id, commit_sha))
    except Exception:
        definitions = {}
    symbol_ids = [value for value in _database_symbols(detail) if value in definitions]
    for edge in (detail.get("method_activity") or {}).get("edges", []):
        for key in ("caller_symbol_id", "symbol_id"):
            value = edge.get(key)
            if value in definitions and value not in symbol_ids:
                symbol_ids.append(value)
    for call in detail.get("database_calls", []):
        for key in ("caller_symbol_id", "symbol_id"):
            value = call.get(key)
            if value in definitions and value not in symbol_ids:
                symbol_ids.append(value)
    summary = detail.get("ai_summary") or {}
    for item in summary.get("excimer_hotspots", []):
        value = item.get("symbol_id") or item.get("frame")
        if value in definitions and value not in symbol_ids:
            symbol_ids.append(value)
    source = {}
    if symbol_ids:
        try:
            source = _source_context(archive_loader(revision["archive_key"]), definitions, symbol_ids)
        except (KeyError, OSError, ValueError):
            source = {}
    assembled, gaps = _evidence(detail, source)
    if model_call is None:
        from runtime.llm import complete_with_model
        model_call = complete_with_model
    prompt = json.dumps({"task": ANALYSIS_TASK, "evidence": assembled}, ensure_ascii=False)
    try:
        content, model, used_provider = model_call(
            prompt, system="You analyze privacy-filtered runtime evidence. Return only valid JSON.",
            temperature=0.1, max_tokens=1800, provider=provider,
        )
    except Exception as exc:
        _log_model_failure("analysis", provider, exc)
        raise AnalysisError("The configured model could not complete analysis. Retry the request.") from exc
    validated = _validate_findings(_parse_model_json(content), assembled, gaps)
    return {**validated, "provider": used_provider, "requested_provider": provider, "model": model,
            "revision_id": revision.get("revision_id"), "source_available": bool(source)}


def preview_diff(
    revision: dict,
    selected_finding: dict,
    analysis: dict,
    detail: dict,
    *,
    archive_loader: Callable[[str], bytes],
    model_call: Callable[..., Any] | None = None,
) -> dict:
    if not detail.get("available") or not detail.get("request"):
        raise AnalysisError("Request telemetry is unavailable for diff validation. Reload the request and analyze again.")
    if not isinstance(selected_finding, dict) or not isinstance(selected_finding.get("id"), str):
        raise AnalysisError("Select a finding from the current analysis before requesting a diff.")
    known = next((item for item in analysis.get("findings", []) if item.get("id") == selected_finding["id"]), None)
    if known is None or known != selected_finding:
        raise AnalysisError("The selected finding does not match the current analysis. Analyze again.")
    source_refs = known.get("source_refs", [])
    if not source_refs:
        raise AnalysisError("This finding has no validated source mapping, so a diff preview is unavailable.")
    commit_sha = revision.get("commit_sha")
    if not isinstance(commit_sha, str) or not revision.get("archive_key"):
        raise AnalysisError("Indexed revision source is unavailable for a diff preview.")
    observed_symbols = set()
    for edge in (detail.get("method_activity") or {}).get("edges", []):
        observed_symbols.update(value for value in (edge.get("caller_symbol_id"), edge.get("symbol_id")) if isinstance(value, str))
    for call in detail.get("database_calls", []):
        observed_symbols.update(value for value in (call.get("caller_symbol_id"), call.get("symbol_id")) if isinstance(value, str))
    for item in (detail.get("ai_summary") or {}).get("excimer_hotspots", []):
        value = item.get("symbol_id")
        if isinstance(value, str):
            observed_symbols.add(value)
    if not set(source_refs).issubset(observed_symbols):
        raise AnalysisError("The selected finding is not mapped to this request's evidence. Analyze the request again.")
    # Revalidate source mapping against the exact revision at preview time.
    from telemetry.runtime import snapshot_definitions
    definitions = _definition_map(snapshot_definitions(revision["project_id"], commit_sha))
    paths = {definitions[symbol]["file"] for symbol in source_refs if symbol in definitions}
    if not paths:
        raise AnalysisError("The finding's source mapping is stale. Analyze the request again.")
    archive = archive_loader(revision["archive_key"])
    source = _source_context(archive, definitions, source_refs)
    if not source:
        raise AnalysisError("The finding's source mapping is stale. Analyze the request again.")
    if model_call is None:
        from runtime.llm import complete_with_model
        model_call = complete_with_model
    provider = analysis.get("requested_provider", analysis.get("provider", "local"))
    prompt = json.dumps({"task": "Propose a minimal source edit for the selected finding. Return JSON with exactly file_path, old_text, and new_text. old_text must be one exact contiguous code fragment from the supplied excerpt; new_text replaces only that fragment. Do not include markdown or alter unrelated code.", "finding": known, "source": list(source.values())}, ensure_ascii=False)
    try:
        content, model, used_provider = model_call(prompt, system="Generate a review-only PHP patch proposal. Return only JSON.", temperature=0.1, max_tokens=3000, provider=provider)
    except Exception as exc:
        _log_model_failure("diff preview", provider, exc)
        raise AnalysisError("The configured model could not generate a diff preview. Retry the request.") from exc
    proposal = _parse_model_json(content)
    path, old_text, new_text = proposal.get("file_path"), proposal.get("old_text"), proposal.get("new_text")
    if (path not in paths or not isinstance(old_text, str) or not old_text or len(old_text) > 10_000
            or not isinstance(new_text, str) or len(new_text) > 10_000):
        raise AnalysisError("The model proposed an invalid or out-of-scope diff. Retry after reviewing the finding.")
    try:
        with zipfile.ZipFile(BytesIO(archive)) as bundle:
            original = bundle.read(path).decode("utf-8", errors="replace")
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise AnalysisError("The exact-revision source file is unavailable for this diff.") from exc
    if original.count(old_text) != 1:
        raise AnalysisError("The proposed edit does not match one unique region of the indexed source. Analyze the request again.")
    updated = original.replace(old_text, new_text, 1)
    diff = "".join(difflib.unified_diff(original.splitlines(True), updated.splitlines(True), fromfile=f"a/{path}", tofile=f"b/{path}"))
    if not diff or len(diff) > MAX_DIFF_CHARS:
        raise AnalysisError("The proposed diff is empty or exceeds the preview limit.")
    return {"diff": diff, "file_path": path, "revision_id": revision.get("revision_id"), "provider": used_provider, "model": model, "applied": False}
