"""SCIP indexing for TypeScript/JavaScript and Python, converted to the symbol-graph export.

Runs scip-typescript and scip-python on an extracted revision without project builds,
dependency installs, or package scripts (external-analyzer spec clause 25), prints the
index with the ``scip`` CLI, and converts it to the inspector's export shape
(``definitions`` and ``references``) with origin ``scip`` (clause 26). Languages without
an indexer, or whose indexer fails, are reported as unavailable with a reason (clause 28).

SCIP symbols look like ``scip-typescript npm pkg 1.0.0 src/`auth.ts`/Server#login().``:
namespaces end in ``/``, types in ``#``, terms in ``.``, methods in ``().``. They are
mapped to the graph's names: ``src\\auth\\Server`` for the class and
``src\\auth\\Server::login`` for the method. Local symbols (``local 3``) are skipped.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

ORIGIN = "scip"
MAX_DEFINITIONS = 20_000
MAX_REFERENCES = 50_000
INDEX_TIMEOUT_SECONDS = 240
PRINT_TIMEOUT_SECONDS = 60
_DEFINITION_ROLE = 0x1
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# Indexer per language, with the source suffixes it reads.
INDEXERS: dict[str, tuple[str, frozenset[str]]] = {
    "typescript": ("scip-typescript", frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})),
    "python": ("scip-python", frozenset({".py"})),
}
LANGUAGE_ALIASES = {"javascript": "typescript", "typescript": "typescript", "python": "python"}


def _segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value) or "_"
    return "_" + cleaned if cleaned[0].isdigit() else cleaned


def _descriptors(symbol: str) -> list[tuple[str, str]]:
    """``(name, suffix)`` pairs of a SCIP symbol's descriptor part; suffix is ``/``, ``#``, ``.``, ``()``, …"""
    parts = symbol.split(" ", 4)
    if len(parts) < 5:
        return []
    text, found, i = parts[4], [], 0
    while i < len(text):
        if text[i] == "`":  # escaped name such as `auth.ts`
            end = text.index("`", i + 1)
            name, i = text[i + 1:end], end + 1
        else:
            match = re.match(r"[^/#.():\[\]`]+", text[i:])
            if not match:
                i += 1
                continue
            name, i = match.group(0), i + match.end()
        if text.startswith("().", i):
            found.append((name, "()")); i += 3
        elif text.startswith("(", i):  # disambiguated overload: name(+1).
            close = text.index(").", i)
            found.append((name, "()")); i = close + 2
        elif i < len(text) and text[i] in "/#.:":
            found.append((name, text[i])); i += 1
        elif i < len(text) and text[i] == "[":  # type parameter
            close = text.index("]", i)
            i = close + 1
        else:
            found.append((name, ""))
    return found


def symbol_identity(symbol: str) -> dict[str, str] | None:
    """Graph identity of a SCIP symbol: ``{"kind", "fqn", "name", "owner"?}``; None for locals and modules."""
    if not symbol or symbol.startswith("local "):
        return None
    namespace: list[str] = []
    owners: list[str] = []
    descriptors = _descriptors(symbol)
    for index, (name, suffix) in enumerate(descriptors):
        last = index == len(descriptors) - 1
        if suffix == "/":
            # TS names files (`auth.ts`); scip-python names modules with dots (`auth.login`).
            stem = re.sub(r"\.(py|tsx?|jsx?|mjs|cjs)$", "", name)
            namespace += [_segment(part) for part in stem.split(".") if part]
        elif suffix == "#":
            owners.append(_segment(name))
            if last:
                fqn = "\\".join(namespace + owners)
                return {"kind": "class", "fqn": fqn, "name": _segment(name)}
        elif suffix in {"()", "."}:
            if not last:
                if suffix == ".":  # a term that contains members (object literal, module object)
                    owners.append(_segment(name))
                continue
            if not _IDENT.match(name):
                return None
            if owners:
                owner = "\\".join(namespace + owners)
                kind = "method" if suffix == "()" else "property"
                return {"kind": kind, "fqn": f"{owner}::{name}", "name": name, "owner": owner}
            if suffix == "()":
                fqn = "\\".join(namespace + [name])
                return {"kind": "function", "fqn": fqn, "name": fqn}
            return None  # module-level variables are not graph definitions
    return None


def _field(item: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in item:
            return item[name]
    return None


def _lines(range_value: Any) -> tuple[int, int] | None:
    """SCIP ranges are [line, start, end] or [start_line, start_col, end_line, end_col], zero-based."""
    if not isinstance(range_value, list) or len(range_value) not in (3, 4):
        return None
    start = int(range_value[0]) + 1
    end = int(range_value[2]) + 1 if len(range_value) == 4 else start
    return start, end


def convert(printed: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Convert ``scip print --json`` output to ``{"definitions", "references", "truncated"}``.

    Only references to symbols defined in this index are kept (library and standard-library
    symbols are dropped), and each (scope, target, line) appears once.
    """
    documents = []
    defined: set[str] = set()
    for document in printed.get("documents") or []:
        path = str(_field(document, "relative_path", "relativePath") or "")
        if not path:
            continue
        occurrences = _field(document, "occurrences") or []
        documents.append((f"{prefix}{path}" if prefix else path, occurrences))
        defined.update(str(o.get("symbol") or "") for o in occurrences
                       if int(_field(o, "symbol_roles", "symbolRoles") or 0) & _DEFINITION_ROLE)

    definitions: dict[str, dict[str, Any]] = {}
    references: dict[tuple, dict[str, Any]] = {}
    truncated = False
    for path, occurrences in documents:
        scopes: list[tuple[int, int, dict[str, str]]] = []
        for occurrence in occurrences:
            if not int(_field(occurrence, "symbol_roles", "symbolRoles") or 0) & _DEFINITION_ROLE:
                continue
            identity = symbol_identity(str(occurrence.get("symbol") or ""))
            lines = _lines(occurrence.get("range"))
            if identity is None or lines is None:
                continue
            enclosing = _lines(_field(occurrence, "enclosing_range", "enclosingRange")) or lines
            if identity["kind"] in {"method", "function"}:
                scopes.append((enclosing[0], enclosing[1], identity))
            if identity["kind"] == "property" or identity["fqn"] in definitions:
                continue
            if len(definitions) >= MAX_DEFINITIONS:
                truncated = True
                continue
            definitions[identity["fqn"]] = {**identity, "file": path, "line": lines[0], "end_line": enclosing[1],
                                            "origin": ORIGIN}
        for occurrence in occurrences:
            symbol = str(occurrence.get("symbol") or "")
            if int(_field(occurrence, "symbol_roles", "symbolRoles") or 0) & _DEFINITION_ROLE or symbol not in defined:
                continue
            target = symbol_identity(symbol)
            lines = _lines(occurrence.get("range"))
            if target is None or lines is None or target["kind"] == "property":
                continue
            inside = [s for s in scopes if s[0] <= lines[0] <= s[1]]
            if not inside:
                continue  # module-level use: no enclosing definition to attach it to
            scope = min(inside, key=lambda s: s[1] - s[0])[2]
            if target["kind"] == "method":
                kind, to_fqn, member = "method_call", target["owner"], target["name"]
            elif target["kind"] == "function":
                kind, to_fqn, member = "function_call", target["fqn"], None
            else:
                kind, to_fqn, member = "class_reference", target["fqn"], None
            key = (scope["fqn"], kind, to_fqn, member, lines[0])
            if key in references:
                continue
            if len(references) >= MAX_REFERENCES:
                truncated = True
                break
            references[key] = {"scope": scope["fqn"], "kind": kind, "to_fqn": to_fqn, "to_member": member,
                               "file": path, "line": lines[0], "origin": ORIGIN}
    return {"definitions": list(definitions.values()), "references": list(references.values()),
            "truncated": truncated}


def _run(argv: list[str], cwd: str, timeout: int) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": cwd, "NODE_OPTIONS": "--max-old-space-size=2048",
           "npm_config_offline": "true", "PIP_NO_INDEX": "1"}
    return subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout, check=False, env=env)


def _index_language(language: str, root: str, project_name: str) -> tuple[str | None, str | None]:
    """Run one indexer; returns (index path, error)."""
    binary, _suffixes = INDEXERS[language]
    executable = shutil.which(binary)
    if executable is None:
        return None, f"{binary} is not installed."
    output = os.path.join(tempfile.mkdtemp(prefix=f"scip-{language}-"), "index.scip")
    if language == "typescript":
        argv = [executable, "index", "--output", output]
        if not os.path.exists(os.path.join(root, "tsconfig.json")):
            argv.append("--infer-tsconfig")
    else:
        environment = os.path.join(os.path.dirname(output), "environment.json")
        Path(environment).write_text("[]", encoding="utf-8")  # no pip discovery, no dependency install
        argv = [executable, "index", ".", "--project-name", project_name, "--project-version", "0",
                "--environment", environment, "--output", output]
    try:
        proc = _run(argv, root, INDEX_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None, f"{binary} did not finish within {INDEX_TIMEOUT_SECONDS} seconds."
    if proc.returncode != 0 or not os.path.exists(output):
        detail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        return None, f"{binary} failed: {(detail[-1] if detail else 'no output')[:300]}"
    return output, None


def _print(index_path: str) -> dict[str, Any]:
    scip = shutil.which("scip")
    if scip is None:
        raise RuntimeError("The scip CLI is not installed.")
    proc = _run([scip, "print", "--json", index_path], os.path.dirname(index_path), PRINT_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise RuntimeError(f"scip print failed: {proc.stderr.decode('utf-8', 'replace')[:300]}")
    return json.loads(proc.stdout or b"{}")


def version(binary: str) -> str | None:
    executable = shutil.which(binary)
    if executable is None:
        return None
    try:
        out = subprocess.run([executable, "--version"], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or out.stderr or b"").decode("utf-8", "replace").strip()
    return text.splitlines()[-1][:80] if text else None


def index_source(root: str, languages: list[str], project_name: str = "project") -> dict[str, Any]:
    """Index an extracted revision. Never raises; per-language coverage says what happened."""
    result: dict[str, Any] = {"name": "scip", "origin": ORIGIN, "languages": {}, "definitions": [], "references": [],
                              "truncated": False, "versions": {}}
    wanted = sorted({LANGUAGE_ALIASES[lang] for lang in languages if lang in LANGUAGE_ALIASES})
    for language in sorted(set(languages) - set(LANGUAGE_ALIASES)):
        result["languages"][language] = {"status": "unavailable",
                                         "reason": "No SCIP indexer is supported for this language without a build."}
    for language in wanted:
        binary = INDEXERS[language][0]
        result["versions"][binary] = version(binary)
        index_path, error = _index_language(language, root, _segment(project_name))
        if error:
            result["languages"][language] = {"status": "unavailable", "reason": error}
            continue
        try:
            converted = convert(_print(index_path))
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            result["languages"][language] = {"status": "unavailable", "reason": str(exc)[:300]}
            continue
        finally:
            shutil.rmtree(os.path.dirname(index_path), ignore_errors=True)
        result["definitions"] += converted["definitions"]
        result["references"] += converted["references"]
        result["truncated"] = result["truncated"] or converted["truncated"]
        result["languages"][language] = {"status": "partial" if converted["truncated"] else "complete",
                                         "definitions": len(converted["definitions"]),
                                         "references": len(converted["references"])}
    result["versions"]["scip"] = version("scip")
    return result


def run_scip(archive_data: bytes, files: list[str], languages: dict[str, int]) -> dict[str, Any]:
    """Worker entry point: extract the selected revision files and index them. Never raises."""
    import io
    import zipfile

    from code_quality.service import MAX_FILE_BYTES, _safe_archive_path

    selected = set(files)
    with tempfile.TemporaryDirectory(prefix="scip-source-") as root:
        try:
            with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
                for member in archive.infolist():
                    relative = _safe_archive_path(member.filename)
                    if relative not in selected or member.is_dir() or member.file_size > MAX_FILE_BYTES:
                        continue
                    target = Path(root, relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(member))
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            return {"name": "scip", "origin": ORIGIN, "status": "unavailable", "languages": {},
                    "definitions": [], "references": [], "reason": f"Source archive could not be read: {exc}"[:300]}
        result = index_source(root, [lang for lang, count in languages.items() if count])
    statuses = {entry["status"] for entry in result["languages"].values()}
    result["status"] = ("complete" if statuses == {"complete"} else
                        "unavailable" if not statuses - {"unavailable"} else "partial")
    return result
