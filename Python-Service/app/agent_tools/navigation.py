"""Read-only source code navigation tools for the agent.

Normative behavior: @.archcore/agent-tools/source-code-navigation-tools.spec.md.
Every tool reads one resolved source root (see ``agent_tools.source``), runs
external programs as argument lists with no shell, sorts results by path and
line before cutting them, and reports when a limit cut the result.
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agent_tools.source import SECRET_GLOBS, SourceRoot, resolve_source
from agent_tools.types import EFFECT_READ_ONLY, SOURCE_BUILTIN, USER_WORKFLOWS, ToolCallRejected, ToolContext, ToolDefinition

_MAX_LINE_CHARS = 300
_MAX_STDOUT_BYTES = 8 * 1024 * 1024
_SUBPROCESS_TIMEOUT = 25
_READ_MAX_LINES = 200
_READ_DEFAULT_LINES = 120
_READ_MAX_FILE_BYTES = 2 * 1024 * 1024
_OUTLINE_MAX_ITEMS = 300
_OUTLINE_MAX_DEPTH = 3
_STRUCTURAL_LANGUAGES = {
    "php": "php", "python": "python", "javascript": "javascript", "typescript": "typescript",
    "tsx": "tsx", "go": "go", "java": "java", "kotlin": "kotlin", "csharp": "csharp",
    "ruby": "ruby", "rust": "rust", "c": "c", "cpp": "cpp", "scala": "scala", "swift": "swift",
}


def _excluded_dir_globs() -> list[str]:
    from code_quality.service import EXCLUDED_SOURCE_DIRS

    return [f"!**/{d}/**" for d in sorted(EXCLUDED_SOURCE_DIRS)] + [f"!**/{g}" for g in SECRET_GLOBS]


def _subprocess_limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (_SUBPROCESS_TIMEOUT + 5, _SUBPROCESS_TIMEOUT + 5))
    resource.setrlimit(resource.RLIMIT_AS, (2_000_000_000, 2_000_000_000))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))


def _require(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise ToolCallRejected("unavailable", f"{binary} is not installed in this image.")
    return path


def _stream_lines(argv: list[str], cwd: Path) -> Iterator[str]:
    """Yield stdout lines of a bounded, shell-free subprocess; kill it when the caller stops."""
    proc = subprocess.Popen(
        argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent", "LANG": "C.UTF-8"},
        preexec_fn=_subprocess_limits, start_new_session=True,
    )
    read = 0
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            read += len(raw)
            if read > _MAX_STDOUT_BYTES:
                break
            yield raw.decode("utf-8", errors="replace").rstrip("\n")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        stderr = (proc.stderr.read(2000) if proc.stderr else b"").decode("utf-8", errors="replace")
        if proc.returncode not in (0, 1, -9) and not read:
            raise ToolCallRejected("tool_failed", stderr.strip()[:300] or f"{argv[0]} exited with {proc.returncode}")


def _clip(text: str) -> str:
    text = text.rstrip()
    return text if len(text) <= _MAX_LINE_CHARS else text[:_MAX_LINE_CHARS] + "…"


def _result(root: SourceRoot, items: list[dict], limit: int, **extra: Any) -> dict[str, Any]:
    items.sort(key=lambda i: (i.get("path", ""), i.get("line", 0)))
    return {"source": root.origin, "items": items[:limit], "truncated": len(items) > limit,
            "limit": limit, **extra}


# --- code_text_search ----------------------------------------------------------------

def _text_search(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    root = resolve_source(context)
    limit = int(args.get("max_results", 30))
    argv = [_require("rg"), "--json", "--no-config", "--sort", "path", "--max-filesize", "2M",
            "--max-count", "5", "--max-columns", str(_MAX_LINE_CHARS)]
    if args.get("fixed_string", True):
        argv.append("--fixed-strings")
    if not args.get("case_sensitive", False):
        argv.append("--ignore-case")
    for glob in _excluded_dir_globs():
        argv += ["--glob", glob]
    if args.get("path_glob"):
        argv += ["--glob", str(args["path_glob"])]
    argv += ["-e", str(args["pattern"]), "--", "."]

    items: list[dict[str, Any]] = []
    for line in _stream_lines(argv, root.path):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "match":
            continue
        data = event.get("data") or {}
        path = ((data.get("path") or {}).get("text") or "").removeprefix("./")
        if not path or root.is_excluded(path):
            continue
        items.append({"path": path, "line": data.get("line_number"),
                      "text": _clip((data.get("lines") or {}).get("text") or "")})
        if len(items) > limit:
            break
    return _result(root, items, limit)


# --- code_structural_search ---------------------------------------------------------------

def _structural_search(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    root = resolve_source(context)
    limit = int(args.get("max_results", 50))
    language = _STRUCTURAL_LANGUAGES[str(args["language"])]
    argv = [_require("ast-grep"), "run", "--pattern", str(args["pattern"]), "--lang", language, "--json=stream"]
    for glob in _excluded_dir_globs():
        argv += ["--globs", glob]
    if args.get("path_glob"):
        argv += ["--globs", str(args["path_glob"])]
    argv.append(".")

    items: list[dict[str, Any]] = []
    for line in _stream_lines(argv, root.path):
        try:
            match = json.loads(line)
        except ValueError:
            continue
        path = str(match.get("file") or "").removeprefix("./")
        if not path or root.is_excluded(path):
            continue
        start = ((match.get("range") or {}).get("start") or {}).get("line", 0)
        first_line = str(match.get("lines") or match.get("text") or "").splitlines()[:1]
        items.append({"path": path, "line": int(start) + 1, "text": _clip(first_line[0] if first_line else "")})
        if len(items) >= 1000:
            break
    return _result(root, items, limit)


# --- code_read -------------------------------------------------------------------------------

def read_lines(context: ToolContext, path: str, start_line: int, end_line: int) -> dict[str, Any]:
    """Read a numbered line window with the ``code_read`` checks (also used by citation previews)."""
    return _read({"path": path, "start_line": start_line, "end_line": end_line}, context)


def _read(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    root = resolve_source(context)
    path = root.resolve_file(str(args["path"]))
    if path.stat().st_size > _READ_MAX_FILE_BYTES:
        raise ToolCallRejected("too_large", "The file is larger than 2 MB; search it instead.")
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        raise ToolCallRejected("binary_file", "The file is binary.")
    lines = data.decode("utf-8", errors="replace").splitlines()
    start = max(1, int(args.get("start_line", 1)))
    end = min(len(lines), int(args.get("end_line", start + _READ_DEFAULT_LINES - 1)), start + _READ_MAX_LINES - 1)
    numbered = "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1)) if start <= len(lines) else ""
    return {"source": root.origin, "path": root.relative(path), "start_line": start, "end_line": end,
            "total_lines": len(lines), "content": numbered, "truncated": end < len(lines)}


# --- code_outline ---------------------------------------------------------------------------------

def _outline(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    import tree_sitter_language_pack as tsp

    root = resolve_source(context)
    path = root.resolve_file(str(args["path"]))
    relative = root.relative(path)
    language = tsp.detect_language_from_path(str(path))
    if not language or language not in set(tsp.downloaded_languages()):
        return {"source": root.origin, "path": relative, "supported": False,
                "items": [], "detail": f"No syntax grammar is installed for {language or 'this file type'}."}
    if path.stat().st_size > _READ_MAX_FILE_BYTES:
        raise ToolCallRejected("too_large", "The file is larger than 2 MB.")
    source = path.read_text(encoding="utf-8", errors="replace")
    result = tsp.process(source, tsp.ProcessConfig(language=language, structure=True, imports=False,
                                                   exports=False, parse_timeout_ms=5000))
    items: list[dict[str, Any]] = []

    def walk(nodes: list[Any], depth: int) -> None:
        for node in nodes:
            if len(items) >= _OUTLINE_MAX_ITEMS + 1:
                return
            items.append({"line": node.span.start_line + 1, "end_line": node.span.end_line + 1,
                          "kind": str(node.kind), "name": node.name, "depth": depth,
                          "signature": _clip(node.signature or node.name or "")})
            if depth + 1 < _OUTLINE_MAX_DEPTH:
                walk(list(node.children or []), depth + 1)

    walk(list(result.structure or []), 0)
    return {"source": root.origin, "path": relative, "language": language, "supported": True,
            "items": items[:_OUTLINE_MAX_ITEMS], "truncated": len(items) > _OUTLINE_MAX_ITEMS}


# --- code_symbol_lookup ------------------------------------------------------------------------------

def _graph_collection(context: ToolContext, projects: list[dict[str, Any]]) -> str | None:
    from inspection.service import sym_collection_for

    if context.project_id:
        for item in projects:
            if item.get("project_id") == context.project_id and (
                    not context.commit_sha or item.get("commit_sha") == context.commit_sha):
                return str(item.get("collection"))
    if context.filename:
        wanted = sym_collection_for(Path(context.filename).stem)
        if any(item.get("collection") == wanted for item in projects):
            return wanted
    return None


def _graph_path(value: Any, root: SourceRoot | None) -> str | None:
    """Project-relative path for a graph file; graph exports carry absolute paths."""
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("\\", "/")
    if root is not None:
        relative = root.relative(text)
        if relative:
            return relative
    parts = [p for p in text.split("/") if p]
    found: str | None = None
    for marker in ("workspaces", "snapshots"):
        if marker in parts:
            found = "/".join(parts[parts.index(marker) + 2:]) or None
            break
    else:
        for index, part in enumerate(parts):
            if part.startswith(("source_", "extract_")):
                found = "/".join(parts[index + 1:]) or None
                break
        else:
            found = text if not text.startswith("/") else None
    prefix = root.wrapper_prefix if root is not None else ""
    if found and prefix and found.startswith(prefix + "/"):
        found = found[len(prefix) + 1:]
    return found


def _symbol_row(record: dict[str, Any], root: SourceRoot | None) -> dict[str, Any] | None:
    node = record.get("ref") if isinstance(record.get("ref"), dict) else record
    path = _graph_path(node.get("file") or node.get("source_file"), root)
    if path and root is not None and root.is_excluded(path):
        return None
    return {"path": path, "line": node.get("line") or node.get("start_line"),
            "kind": node.get("kind"), "symbol": node.get("fqn") or node.get("name"), "origin": "php-parser"}


def _symbol_lookup(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    from inspection.service import inspection_service

    limit = int(args.get("max_results", 50))
    try:
        service = inspection_service()
        collection = _graph_collection(context, service.list_projects())
    except Exception as exc:  # noqa: BLE001 — graph store down is a coverage state, not a crash
        return {"coverage": "unavailable", "items": [], "detail": f"The symbol graph is unavailable: {str(exc)[:200]}"}
    if not collection:
        return {"coverage": "unavailable", "items": [],
                "detail": "No symbol graph is indexed for this project; use code_text_search instead."}
    try:
        root = resolve_source(context)
    except ToolCallRejected:
        root = None

    name = str(args["symbol"]).strip()
    relation = str(args.get("relation", "definition"))
    definitions = service.resolve_symbol(collection, name)
    if relation == "definition":
        rows = definitions
    else:
        if not definitions:
            return {"coverage": "complete", "items": [], "detail": f"No definition named {name} in the symbol graph."}
        fqn = str(definitions[0].get("fqn") or name)
        if relation == "references":
            rows = service.find_references(collection, fqn, name=name)
        elif relation == "implementations":
            rows = service.find_implementations(collection, fqn)
        else:
            hierarchy = service.call_hierarchy(collection, fqn, name=name) or {}
            rows = hierarchy.get(relation, []) if isinstance(hierarchy, dict) else []
    items = [row for row in (_symbol_row(r, root) for r in rows if isinstance(r, dict)) if row]
    items.sort(key=lambda i: (i.get("path") or "", i.get("line") or 0))
    return {"coverage": "complete", "relation": relation, "items": items[:limit], "truncated": len(items) > limit,
            "detail": "Static PHP graph: dynamic calls, reflection, and container resolution may be missing."}


# --- code_stats -------------------------------------------------------------------------------------------

def _stats(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    from code_quality.service import EXCLUDED_SOURCE_DIRS

    root = resolve_source(context)
    argv = [_require("scc"), "--format", "json", "--no-cocomo", "--exclude-dir", ",".join(sorted(EXCLUDED_SOURCE_DIRS)), "."]
    output = "\n".join(_stream_lines(argv, root.path))
    try:
        languages = json.loads(output or "[]")
    except ValueError as exc:
        raise ToolCallRejected("tool_failed", "scc returned output that is not JSON.") from exc
    rows = [{"language": lang.get("Name"), "files": lang.get("Count"), "code": lang.get("Code"),
             "comments": lang.get("Comment"), "blank": lang.get("Blank"), "complexity": lang.get("Complexity")}
            for lang in languages if isinstance(lang, dict)]
    rows.sort(key=lambda r: (-(r["code"] or 0), str(r["language"])))
    return {"source": root.origin, "languages": rows[:30],
            "note": "Complexity is scc's branch-count estimate, not a quality score."}


# --- definitions ------------------------------------------------------------------------------------------

def _definition(name: str, description: str, schema: dict[str, Any], handler) -> ToolDefinition:
    return ToolDefinition(name=name, description=description, input_schema=schema, effect=EFFECT_READ_ONLY,
                          source=SOURCE_BUILTIN, workflows=USER_WORKFLOWS, handler=handler, timeout_seconds=30)


_MAX_RESULTS = {"type": "integer", "minimum": 1, "maximum": 200, "default": 30}
_PATH_GLOB = {"type": "string", "maxLength": 200, "description": "Optional glob to limit files, e.g. 'app/**/*.php'."}
_PATH = {"type": "string", "minLength": 1, "maxLength": 500, "description": "Project-relative file path."}

NAVIGATION_TOOLS = [
    _definition(
        "code_text_search",
        "Search the project's source files for text, like grep. Returns path, line, and the matching line, "
        "at most 5 matches per file. Use it for identifiers, error messages, config keys, and route names; "
        "add path_glob to search one area.",
        {"type": "object", "properties": {
            "pattern": {"type": "string", "minLength": 1, "maxLength": 300},
            "fixed_string": {"type": "boolean", "default": True, "description": "False treats pattern as a regex."},
            "case_sensitive": {"type": "boolean", "default": False},
            "path_glob": _PATH_GLOB, "max_results": _MAX_RESULTS},
         "required": ["pattern"], "additionalProperties": False},
        _text_search,
    ),
    _definition(
        "code_structural_search",
        "Find code by syntax pattern with ast-grep, e.g. '$X->where($$$)' in PHP or 'console.log($A)' in "
        "JavaScript. $NAME matches one node, $$$ matches any number of nodes.",
        {"type": "object", "properties": {
            "pattern": {"type": "string", "minLength": 1, "maxLength": 500},
            "language": {"type": "string", "enum": sorted(_STRUCTURAL_LANGUAGES)},
            "path_glob": _PATH_GLOB, "max_results": _MAX_RESULTS},
         "required": ["pattern", "language"], "additionalProperties": False},
        _structural_search,
    ),
    _definition(
        "code_read",
        "Read lines of one project file, numbered. Reads 120 lines by default and at most 200; "
        "pass start_line and end_line to read just the part you need.",
        {"type": "object", "properties": {
            "path": _PATH,
            "start_line": {"type": "integer", "minimum": 1, "default": 1},
            "end_line": {"type": "integer", "minimum": 1}},
         "required": ["path"], "additionalProperties": False},
        _read,
    ),
    _definition(
        "code_outline",
        "List the classes, functions, and methods in one file with their signatures and line numbers, "
        "without bodies. Use it before reading a large file.",
        {"type": "object", "properties": {"path": _PATH}, "required": ["path"], "additionalProperties": False},
        _outline,
    ),
    _definition(
        "code_symbol_lookup",
        "Look up a PHP symbol in the project's symbol graph: its definition, references, callers, callees, "
        "or implementations. Symbol may be a short name or a fully qualified name.",
        {"type": "object", "properties": {
            "symbol": {"type": "string", "minLength": 1, "maxLength": 300},
            "relation": {"type": "string", "enum": ["definition", "references", "callers", "callees", "implementations"],
                         "default": "definition"},
            "max_results": _MAX_RESULTS},
         "required": ["symbol"], "additionalProperties": False},
        _symbol_lookup,
    ),
    _definition(
        "code_stats",
        "Count files and lines of code per language in the project, excluding dependencies and build output.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        _stats,
    ),
]
