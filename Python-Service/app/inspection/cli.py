"""Subprocess bridge to the PHP inspection exporter.

Runs ``php bin/project-inspect <kind> …`` (nikic/php-parser based; host CLI
under ``settings.php_cli_root``, default ``/app/php``) and parses its stdout
into Python structures. Each kind has a fixed JSON contract:

    skeleton      file …            -> {file, namespace, declarations:[…]}
    attributes    file [class]      -> [{kind, name, fqn, attributes, doc_tags}]
    method        file method       -> {name, …params, attributes, source}
    symbols       dir               -> [{kind, fqn, file, line, end_line,
                                         visibility, static, abstract, owner,
                                         relations: [{kind, target}]|null}]
    references    dir               -> [{kind, file, line, scope, to_fqn,
                                         to_member, unresolved}]

The exporter is stateless and emits one JSON array/object per run — there is
no daemon, cache or lock to manage here.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from infra.php_tools import php_bin

logger = logging.getLogger(__name__)

INSPECT_BIN = "project-inspect"

INSPECT_TIMEOUT_S = 900  # project-wide symbol export of large trees is slow


class InspectError(RuntimeError):
    """Raised when the exporter fails or its output cannot be parsed."""


def run_inspect(kind: str, *args: str, timeout: int = INSPECT_TIMEOUT_S) -> Any:
    """Run one exporter subcommand and return the parsed stdout payload.

    ``kind`` is user-facing ("symbols", "references", "skeleton",
    "attributes", "method") and validated against the CLI's whitelist by the
    exporter itself. Raises InspectError on non-zero exit or unparseable JSON.
    """
    cmd = [php_bin(INSPECT_BIN), kind, *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,  # non-zero handled explicitly below
        )
    except subprocess.TimeoutExpired as exc:
        raise InspectError(f"project-inspect {kind} timed out after {timeout}s") from exc
    except OSError as exc:
        raise InspectError(f"project-inspect {kind} could not start: {exc}") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise InspectError(
            f"project-inspect {kind} exited {proc.returncode}"
            + (f": {detail[:500]}" if detail else "")
        )

    stdout = (proc.stdout or "").strip()
    if not stdout:
        # Exporters that legitimately find nothing emit [] — tolerate empty.
        return []
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise InspectError(f"project-inspect {kind} produced unparseable output") from exc


def inspect_skeleton(file_path: str) -> dict[str, Any]:
    """Tier-2: API surface of one file (no method bodies)."""
    data = run_inspect("skeleton", file_path)
    return data if isinstance(data, dict) else {}


def inspect_attributes(file_path: str, class_name: str | None = None) -> list[dict[str, Any]]:
    """Tier-3: #[Route]/#[AsEventListener]/DI + docblock tags per member.

    Exporter emits ``{file, class_name, attributes: [{kind,name,fqn,
    attributes:[{name,args,line}], doc_tags}]}``; the member list is the
    usable payload.
    """
    args = [file_path]
    if class_name:
        args.append(class_name)
    data = run_inspect("attributes", *args)
    if isinstance(data, dict):
        members = data.get("attributes")
        return members if isinstance(members, list) else []
    return data if isinstance(data, list) else []


def inspect_method(file_path: str, method_name: str) -> dict[str, Any] | None:
    """Tier-3: exact single-method source + attributes + docblock."""
    try:
        data = run_inspect("method", file_path, method_name)
    except InspectError:
        # Unknown method → exporter exits 1 with a stderr message.
        return None
    return data if isinstance(data, dict) else None


def inspect_symbols(project_dir: str, exclude: list[str] | None = None) -> list[dict[str, Any]]:
    """Tier-4 export: every definition across the tree.

    ``exclude`` = extra relative dir segments to skip (tests, coverage, …);
    the exporter always skips vendor/node_modules/.git/… itself.
    """
    args = [project_dir]
    if exclude:
        args.append(",".join(e for e in exclude if e))
    data = run_inspect("symbols", *args)
    return data if isinstance(data, list) else []


def inspect_references(project_dir: str, exclude: list[str] | None = None) -> list[dict[str, Any]]:
    """Tier-4 export: every use-site across the tree (see ``inspect_symbols``)."""
    args = [project_dir]
    if exclude:
        args.append(",".join(e for e in exclude if e))
    data = run_inspect("references", *args)
    return data if isinstance(data, list) else []
