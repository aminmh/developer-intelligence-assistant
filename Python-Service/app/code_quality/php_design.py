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
SRP_METHOD_THRESHOLD = 15
OCP_TYPE_CHECK_THRESHOLD = 3

_CLASS_TYPES = {"class", "interface", "trait", "enum"}
_INFRASTRUCTURE_PREFIXES = (
    "aws\\", "doctrine\\dbal\\", "guzzlehttp\\", "illuminate\\database\\",
    "illuminate\\support\\facades\\db", "predis\\", "stripe\\",
    "symfony\\component\\httpclient\\", "symfony\\contracts\\httpclient\\",
    "app\\infrastructure\\", "app\\infra\\",
)
_INFRASTRUCTURE_NAMES = {"pdo", "redis", "mysqli"}
_UNSUPPORTED_METHOD = re.compile(
    r"\bthrow\s+new\s+[a-z0-9_\\]*(?:"
    r"BadMethodCallException|UnsupportedOperationException|NotImplementedException)\b",
    re.IGNORECASE,
)
_INSTANCEOF = re.compile(r"\binstanceof\b", re.IGNORECASE)
_NEW_TYPE = re.compile(r"\bnew\s+(\\?[A-Za-z_][A-Za-z0-9_]*(?:\\[A-Za-z_][A-Za-z0-9_]*)*)")


def _path(payload: dict[str, Any], allowed_files: set[str]) -> str | None:
    value = payload.get("file_path") or payload.get("file")
    if not isinstance(value, str) or not value or len(value) > 300:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        return None
    normalized = path.as_posix()
    return normalized if normalized in allowed_files else None


def _class_name(payload: dict[str, Any]) -> str | None:
    value = payload.get("class_fqn") or payload.get("fq_symbol_name") or payload.get("class")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lstrip("\\")[:300]


def _method_owner(payload: dict[str, Any]) -> str | None:
    value = payload.get("parent_symbol") or payload.get("class_fqn") or payload.get("class")
    return value.strip().lstrip("\\")[:300] if isinstance(value, str) and value.strip() else None


def _line_number(value: Any) -> int:
    return value if type(value) is int and value > 0 else 1


def _normalized_name(value: str) -> str:
    return value.strip().lstrip("\\").casefold()


def _imports(payload: dict[str, Any]) -> dict[str, str]:
    values = payload.get("imports") or []
    if isinstance(values, dict):
        pairs = values.items()
    elif isinstance(values, list):
        pairs = ((None, item) for item in values)
    else:
        return {}
    result: dict[str, str] = {}
    for alias, target in pairs:
        if isinstance(target, dict):
            alias = target.get("alias", alias)
            target = target.get("name") or target.get("fqn")
        if not isinstance(target, str) or not target.strip():
            continue
        full_name = target.strip().lstrip("\\")
        short_name = str(alias or full_name.rsplit("\\", 1)[-1]).strip()
        if short_name:
            result[short_name.casefold()] = full_name
    return result


def _is_domain_class(name: str, file: str) -> bool:
    namespace_parts = [part.casefold() for part in name.split("\\")]
    path_parts = [part.casefold() for part in PurePosixPath(file).parts]
    return "domain" in namespace_parts or "domain" in path_parts


def _infrastructure_type(name: str, imports: dict[str, str]) -> str | None:
    normalized = name.strip().lstrip("\\")
    imported = imports.get(normalized.casefold())
    if imported:
        normalized = imported
    folded = normalized.casefold()
    if folded in _INFRASTRUCTURE_NAMES or folded.startswith(_INFRASTRUCTURE_PREFIXES):
        return normalized
    return None


def _finding(
    project_id: str,
    revision_id: str,
    rule: str,
    file: str,
    line: int,
    message: str,
    snippet: str,
    principle: str,
) -> dict[str, Any]:
    evidence_id = "ev_" + sha256(
        f"{project_id}:{revision_id}:{DESIGN_ANALYZER_VERSION}:{rule}:{file}:{line}".encode()
    ).hexdigest()[:16]
    return {
        "evidence_id": evidence_id,
        "kind": "finding",
        "category": "clean_code",
        "rule": rule,
        "file": file,
        "line": line,
        "message": message[:240],
        "severity": "INFO",
        "tool": "php-solid-design-signals",
        "tool_version": DESIGN_ANALYZER_VERSION,
        "design_principle": principle,
        "context_snippet": snippet.strip()[:MAX_CONTEXT_CHARS],
    }


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

    allowed_files = {path for path in business_files if isinstance(path, str)}
    classes: dict[str, dict[str, Any]] = {}
    methods: dict[str, list[dict[str, Any]]] = {}
    valid_chunks = 0
    for payload in chunks[:MAX_DESIGN_CHUNKS]:
        if not isinstance(payload, dict):
            continue
        if (payload.get("project_id") != project_id
                or payload.get("revision_id") != revision_id
                or payload.get("commit_sha") != snapshot.get("commit_sha")
                or payload.get("source_archive_sha256") != snapshot.get("source_archive_sha256")
                or payload.get("dirty") is not False
                or payload.get("collection") != expected_collection):
            continue
        file = _path(payload, allowed_files)
        node_type = str(payload.get("node_type") or payload.get("type") or "").casefold()
        code = payload.get("code")
        if file is None or not isinstance(code, str) or not code.strip():
            continue
        valid_chunks += 1
        if node_type in _CLASS_TYPES:
            name = _class_name(payload)
            if name:
                classes[_normalized_name(name)] = {
                    "name": name, "type": node_type, "file": file,
                    "line": _line_number(payload.get("start_line") or payload.get("line")),
                    "code": code, "implements": payload.get("implements") or [],
                    "imports": _imports(payload),
                }
        elif node_type in {"method", "class_method", "classmethod"}:
            owner = _method_owner(payload)
            method_name = payload.get("method_name") or payload.get("name")
            line = payload.get("start_line") or payload.get("line") or 1
            if (owner and isinstance(method_name, str) and method_name.strip()
                    and type(line) is int and line > 0):
                methods.setdefault(_normalized_name(owner), []).append({
                    "name": method_name.strip(), "file": file, "line": line,
                    "code": code, "imports": _imports(payload),
                })

    if valid_chunks == 0:
        return _unavailable(
            revision_id,
            "The selected revision index contains no matching PHP AST chunks.",
        ), []

    signals: list[dict[str, Any]] = []
    class_methods: dict[str, list[dict[str, Any]]] = {
        owner: rows for owner, rows in methods.items()
    }
    interfaces = {
        name: set(method["name"].casefold() for method in methods.get(name, []))
        for name, item in classes.items() if item["type"] == "interface"
    }

    # SRP review signal: a high method count is a prompt to inspect cohesion,
    # not evidence by itself that the class has multiple responsibilities.
    for owner, item in classes.items():
        if item["type"] != "class":
            continue
        method_count = len(class_methods.get(owner, []))
        line = item["line"]
        if method_count >= SRP_METHOD_THRESHOLD and type(line) is int and line > 0:
            signals.append(_finding(
                project_id, revision_id, "php-solid-srp-large-class", item["file"], line,
                f"SRP signal: class exposes {method_count} indexed methods; review method cohesion and responsibility boundaries.",
                item["code"], "SRP",
            ))

        # DIP review signal: only known infrastructure types constructed from
        # a Domain namespace/path are reported; arbitrary `new` calls are not.
        if _is_domain_class(item["name"], item["file"]):
            for method in class_methods.get(owner, []):
                for match in _NEW_TYPE.finditer(method["code"]):
                    dependency = _infrastructure_type(match.group(1), method["imports"] or item["imports"])
                    if dependency:
                        signals.append(_finding(
                            project_id, revision_id, "php-solid-dip-direct-infrastructure", method["file"], method["line"],
                            f"DIP signal: domain code directly constructs infrastructure type {dependency}; review whether it should be injected behind an abstraction.",
                            method["code"], "DIP",
                        ))
                        break
        # OCP review signal: several `instanceof` checks in one method can
        # indicate type dispatch that grows whenever a new implementation is
        # introduced. It remains an INFO-level candidate for semantic review.
        for method in class_methods.get(owner, []):
            checks = len(_INSTANCEOF.findall(method["code"]))
            if checks >= OCP_TYPE_CHECK_THRESHOLD:
                signals.append(_finding(
                    project_id, revision_id, "php-solid-ocp-type-dispatch", method["file"], method["line"],
                    f"OCP signal: method contains {checks} instanceof checks; review whether type dispatch makes extensions require edits here.",
                    method["code"], "OCP",
                ))

        # ISP review signal: an implementation throws a conventional
        # unsupported-operation exception for a method declared by an
        # interface it implements.
        implemented = item["implements"] if isinstance(item["implements"], list) else []
        implemented_names = {_normalized_name(value) for value in implemented if isinstance(value, str)}
        for interface_name, interface_methods in interfaces.items():
            short_interface = interface_name.rsplit("\\", 1)[-1]
            matches_interface = False
            for implemented_name in implemented_names:
                resolved_name = item["imports"].get(implemented_name, implemented_name)
                if _normalized_name(resolved_name) == interface_name:
                    matches_interface = True
                    break
                if "\\" not in implemented_name and implemented_name == short_interface:
                    matches_interface = True
                    break
            if not matches_interface:
                continue
            for method in class_methods.get(owner, []):
                if (method["name"].casefold() in interface_methods
                        and _UNSUPPORTED_METHOD.search(method["code"])):
                    signals.append(_finding(
                        project_id, revision_id, "php-solid-isp-unsupported-interface-method",
                        method["file"], method["line"],
                        f"ISP signal: {item['name']} implements an interface method with an unsupported-operation stub; review whether clients depend on a broader interface than needed.",
                        method["code"], "ISP",
                    ))

    unique: dict[str, dict[str, Any]] = {}
    for signal in sorted(signals, key=lambda row: (row["file"], row["line"], row["rule"], row["message"])):
        unique.setdefault(signal["evidence_id"], signal)
        if len(unique) >= MAX_SIGNALS:
            break
    signals_truncated = len(signals) > MAX_SIGNALS
    result = list(unique.values())
    truncated_input = truncated or len(chunks) > MAX_DESIGN_CHUNKS
    summary = {
        "status": "partial" if truncated_input else "complete",
        "revision_id": revision_id,
        "analyzer_version": DESIGN_ANALYZER_VERSION,
        "languages": ["php"],
        "principles_with_static_signals": ["SRP", "OCP", "ISP", "DIP"],
        "principles_requiring_semantic_evidence": ["LSP", "SRP", "OCP", "ISP", "DIP"],
        "chunk_count": valid_chunks,
        "class_count": len(classes),
        "method_count": sum(map(len, class_methods.values())),
        "signal_count": len(result),
        "signals_truncated": signals_truncated,
        "limits": {"chunks": MAX_DESIGN_CHUNKS, "signals": MAX_SIGNALS,
                   "context_chars": MAX_CONTEXT_CHARS,
                   "srp_method_threshold": SRP_METHOD_THRESHOLD,
                   "ocp_instanceof_threshold": OCP_TYPE_CHECK_THRESHOLD},
        "reason": "Signals are review candidates, not proof of SOLID violations.",
    }
    return summary, result


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
        if not isinstance(commit_sha, str) or not isinstance(archive_hash, str):
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
