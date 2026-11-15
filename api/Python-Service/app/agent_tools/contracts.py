"""Model-callable tools that read the API contracts attached to the current project.

Spec: @.archcore/frontend-assist/api-contract-sources-and-versions.spec.md (behavior 20).

A project sees a contract only through its own attachments, and each attachment
resolves to one version: the pinned one, or the source's newest valid version.
Contract text goes to the external model, so each call first passes the
project's remote-source consent check.
"""

from __future__ import annotations

import json
from typing import Any

from agent_tools.types import EFFECT_READ_ONLY, SOURCE_BUILTIN, USER_WORKFLOWS, ToolCallRejected, ToolContext, ToolDefinition

_MAX_OPERATIONS = 200
_MAX_SCHEMA_CHARS = 6000
_MAX_REFERENCED_SCHEMAS = 12


def _attached(context: ToolContext) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]]:
    """(attachment, source, resolved version with model) for the run's project."""
    from api_contracts.service import contract_service
    from infra.remote_consent import RemoteSourceNotAllowed, require_remote_source_allowed

    if not context.project_id:
        raise ToolCallRejected("no_project", "API contracts are available only for a selected project.")
    try:
        require_remote_source_allowed(context.project_id)
    except RemoteSourceNotAllowed as exc:
        raise ToolCallRejected("remote_source_not_allowed", str(exc)) from exc
    store = contract_service.store
    rows = []
    for attachment in store.list_attachments(project_id=context.project_id):
        rows.append((attachment, store.get_source(attachment["source_id"]), store.resolve(attachment, with_model=True)))
    return rows


def _pick(rows: list, contract: str | None) -> list:
    if not contract:
        return rows
    wanted = contract.strip().casefold()
    picked = [row for row in rows if wanted in (row[1]["name"].casefold(), row[1]["source_id"].casefold())]
    if not picked:
        names = ", ".join(sorted(row[1]["name"] for row in rows)) or "none"
        raise ToolCallRejected("unknown_contract", f"No attached contract is named '{contract}'. Attached: {names}.")
    return picked


def _list_operations(arguments: dict[str, Any], context: ToolContext) -> str:
    rows = _pick(_attached(context), arguments.get("contract"))
    if not rows:
        return "No API contract is attached to this project."
    needle = str(arguments.get("search") or "").casefold()
    lines: list[str] = []
    shown = 0
    for attachment, source, version in rows:
        if version is None:
            lines.append(f"## {source['name']} ({attachment['role']}) — no valid version available")
            continue
        model = version["model"]
        lines.append(f"## {source['name']} ({attachment['role']}) — {model['title']} {model['api_version']}, "
                     f"{model['dialect']}, version {version['version_id']}")
        if model["base_paths"]:
            lines.append("Base: " + ", ".join(model["base_paths"][:5]))
        matched = 0
        for key, operation in sorted(model["operations"].items(), key=lambda item: (item[1]["path"], item[1]["method"])):
            text = f"{key} {operation['operation_id'] or ''} {operation['summary']} {' '.join(operation['tags'])}"
            if needle and needle not in text.casefold():
                continue
            matched += 1
            if shown >= _MAX_OPERATIONS:
                continue
            shown += 1
            flags = " [deprecated]" if operation["deprecated"] else ""
            summary = f" — {operation['summary'][:120]}" if operation["summary"] else ""
            lines.append(f"- {key}{flags}{summary}")
        if matched == 0:
            lines.append("(no matching operations)")
    if shown >= _MAX_OPERATIONS:
        lines.append(f"(showing the first {_MAX_OPERATIONS} operations; narrow with 'search')")
    return "\n".join(lines)


def _schema_refs(node: Any, found: list[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/schemas/") and ref[10:] not in found:
            found.append(ref[10:])
        for value in node.values():
            _schema_refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _schema_refs(value, found)


def _read_operation(arguments: dict[str, Any], context: ToolContext) -> str:
    rows = _pick(_attached(context), arguments.get("contract"))
    method = str(arguments.get("method") or "").upper()
    path = str(arguments.get("path") or "")
    key = f"{method} {path}"
    for attachment, source, version in rows:
        if version is None or key not in version["model"]["operations"]:
            continue
        model = version["model"]
        operation = model["operations"][key]
        names: list[str] = []
        _schema_refs(operation, names)
        index = 0
        while index < len(names) and len(names) < _MAX_REFERENCED_SCHEMAS:  # follow nested references, bounded
            _schema_refs(model["schemas"].get(names[index]), names)
            index += 1
        names = names[:_MAX_REFERENCED_SCHEMAS]
        payload = {"contract": source["name"], "role": attachment["role"], "version_id": version["version_id"],
                   "operation": operation,
                   "schemas": {name: model["schemas"].get(name) for name in names}}
        text = json.dumps(payload, indent=1, ensure_ascii=False)
        if len(text) > _MAX_SCHEMA_CHARS:
            text = text[:_MAX_SCHEMA_CHARS] + "\n… (truncated; the operation's schemas are larger than this tool returns)"
        return text
    raise ToolCallRejected("unknown_operation",
                           f"No attached contract defines {key}. Call api_contract_operations to list operations.")


def _check(arguments: dict[str, Any], context: ToolContext) -> str:
    from api_contracts.service import ContractInputError, contract_service

    rows = _pick(_attached(context), arguments.get("contract"))
    if not rows:
        return "No API contract is attached to this project, so there is nothing to check it against."
    lines: list[str] = []
    for attachment, source, _version in rows:
        try:
            report = contract_service.conformance(attachment["attachment_id"], context.revision_id)
        except ContractInputError as exc:
            raise ToolCallRejected("check_unavailable", str(exc)) from exc
        if not report.get("available"):
            lines.append(f"## {source['name']}: " + " ".join(report.get("notes") or ["not available"]))
            continue
        summary, coverage = report["summary"], report["coverage"]
        counts = f"{summary['errors']} errors, {summary['warnings']} warnings, {summary['info']} notes"
        if report["check"] == "provider":
            lines.append(f"## {source['name']} (this project provides it) — {counts}; "
                         f"{coverage['routes_documented']} of {coverage['routes_in_scope']} API routes are in the contract; "
                         f"{coverage['operations_implemented']} of {coverage['operations_total']} operations have a route.")
        else:
            lines.append(f"## {source['name']} (this project consumes it) — {counts}; "
                         f"{coverage['requests_matched']} of {coverage['requests_total']} requests match an operation; "
                         f"{coverage['operations_used']} of {coverage['operations_total']} operations are used.")
        for finding in [f for f in report["findings"] if f["severity"] != "info"][:_MAX_FINDINGS]:
            location = finding["location"]
            place = f"{location.get('file')}:{location.get('line')} " if location.get("line") else \
                f"{location.get('file')} " if location.get("file") else ""
            lines.append(f"- [{finding['severity']}] {place}{finding['message']}")
        lines.extend(f"Note: {note}" for note in report.get("notes") or [])
    return "\n".join(lines)


_MAX_GENERATED_CHARS = 14_000


def _generate(arguments: dict[str, Any], context: ToolContext) -> str:
    from api_contracts.service import ContractInputError, contract_service

    rows = [row for row in _pick(_attached(context), arguments.get("contract")) if row[2] is not None]
    if not rows:
        return "No attached API contract has a valid version to generate code from."
    attachment, source, version = rows[0]
    try:
        result = contract_service.generate_code(source["source_id"], version["version_id"], arguments.get("target"),
                                                search=arguments.get("search"))
    except ContractInputError as exc:
        raise ToolCallRejected("generation_failed", str(exc)) from exc
    parts = [f"Generated from {source['name']} ({result['title']} {result['api_version']}), target {result['target']}: "
             f"{result['operations']} operation(s). Base address: {result['base_url'] or '(none declared)'}."]
    used = len(parts[0])
    for file in result["files"]:
        block = f"\n### {file['path']}\n```ts\n{file['content'].rstrip()}\n```"
        if used + len(block) > _MAX_GENERATED_CHARS:
            parts.append(f"\n({file['path']} and any later files were left out because the output is too long. "
                         "Pass 'search' with a path, tag, or operation ID to generate fewer operations.)")
            break
        parts.append(block)
        used += len(block)
    parts.extend(f"\nNote: {note}" for note in result["notes"])
    return "".join(parts)


_MAX_FINDINGS = 40
_CONTRACT = {"type": "string", "maxLength": 120,
             "description": "Optional contract name, when more than one is attached."}

CONTRACT_TOOLS = [
    ToolDefinition(
        name="api_contract_operations",
        description=(
            "List the HTTP operations in the API contracts (Swagger / OpenAPI documents) attached to this project: "
            "method, path, and summary. Use it to find which endpoint serves a need before reading or writing "
            "client code. Pass 'search' to filter by path, operation ID, tag, or summary text."
        ),
        input_schema={"type": "object", "properties": {
            "search": {"type": "string", "maxLength": 100}, "contract": _CONTRACT}, "additionalProperties": False},
        effect=EFFECT_READ_ONLY, source=SOURCE_BUILTIN, workflows=USER_WORKFLOWS,
        handler=_list_operations, timeout_seconds=20,
    ),
    ToolDefinition(
        name="api_contract_operation",
        description=(
            "Read one operation from an attached API contract: its parameters, request body, responses, and the "
            "schemas they reference. Use the exact method and path from api_contract_operations."
        ),
        input_schema={"type": "object", "properties": {
            "method": {"type": "string", "enum": ["GET", "PUT", "POST", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"]},
            "path": {"type": "string", "minLength": 1, "maxLength": 500, "description": "Path template, e.g. /pets/{id}."},
            "contract": _CONTRACT}, "required": ["method", "path"], "additionalProperties": False},
        effect=EFFECT_READ_ONLY, source=SOURCE_BUILTIN, workflows=USER_WORKFLOWS,
        handler=_read_operation, timeout_seconds=20,
    ),
    ToolDefinition(
        name="api_contract_check",
        description=(
            "Compare this project's code with the API contract attached to it. For a front-end project that consumes "
            "the contract: requests that match no operation, wrong methods, unknown or missing parameters and body "
            "properties, and model fields the contract does not define. For a back-end project that provides it: "
            "routes missing from the contract and contract operations no route serves. Each finding has a file. "
            "Use it when asked whether the code agrees with the API contract."
        ),
        input_schema={"type": "object", "properties": {"contract": _CONTRACT}, "additionalProperties": False},
        effect=EFFECT_READ_ONLY, source=SOURCE_BUILTIN, workflows=USER_WORKFLOWS,
        handler=_check, timeout_seconds=90,
    ),
    ToolDefinition(
        name="api_contract_generate",
        description=(
            "Generate TypeScript code from the API contract attached to this project. target: 'models' (types for "
            "the schemas), 'angular' (an injectable HttpClient service), 'react' (typed fetch functions and hooks), "
            "'vue' (typed fetch functions and composables), or 'mocks' (example responses and Mock Service Worker "
            "handlers). Pass 'search' (a path, tag, or operation ID) to generate only matching operations; do this "
            "for large contracts. The code is produced from the contract, not written from memory, so prefer it "
            "whenever the user asks for a client, service, types, or mocks for the API."
        ),
        input_schema={"type": "object", "properties": {
            "target": {"type": "string", "enum": ["models", "angular", "react", "vue", "mocks"]},
            "search": {"type": "string", "maxLength": 100}, "contract": _CONTRACT},
            "required": ["target"], "additionalProperties": False},
        effect=EFFECT_READ_ONLY, source=SOURCE_BUILTIN, workflows=USER_WORKFLOWS,
        handler=_generate, timeout_seconds=30,
    ),
]
