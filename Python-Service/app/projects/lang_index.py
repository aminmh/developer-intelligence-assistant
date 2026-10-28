"""Language-dispatched code chunking for the code-index preview (P2).

``produce_chunks`` is the single parse entry point behind the ``index_code``
orchestration route. It walks an extracted workspace once, decides the primary
language from ``projects.structure_profile.language_census`` (unless the caller
already knows it), and hands the file set to the matching producer:

  * php    → the existing pure-PHP CLI (``infra.php_tools.run_code_parse``); the
             returned chunk list is already the ingestion contract.
  * python → a pure-stdlib ``ast`` walker in this module (``_py_chunks``) that
             emits one chunk per top-level class / method / module-level
             function with the same flat shape ``llamaindex_layer`` reads.
  * other  → an honest ``error`` (never a fabricated empty success).

Contract — ``produce_chunks`` NEVER raises:

    {"language": str|None, "chunks": [...], "files": int,
     "producer": str|None, "error": str|None}

Determinism: directory traversal is sorted, skip dirs are pruned
case-insensitively (the same set ``structure_profile`` uses), definitions are
visited in source order, and every list the walker emits is ordered/deduped.
Work is capped at ``_MAX_PY_FILES`` python files per run so a huge vendored-free
tree cannot stall the preview.
"""

from __future__ import annotations

import ast
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

_MAX_PY_FILES = 400
_MAX_CODE_CHARS = 3000
_MAX_DOCBLOCK_CHARS = 400
_MAX_CALLS = 16
_MAX_NAME_CHARS = 120

# Skip-dir names are mirrored at call time from projects.structure_profile so
# the index walk and the census walk can never drift apart.
def _resolve_root(workspace_path: str | Path | None) -> Path | None:
    """Existing directory or None (total helper — never raises)."""
    if workspace_path is None:
        return None
    try:
        path = Path(workspace_path).expanduser()
        return path if path.is_dir() else None
    except (OSError, TypeError, ValueError):
        return None


def _module_dotted(rel: str) -> str:
    """Return a stable PHP-compatible namespace for a Python source path."""
    stripped = re.sub(r"\.py$", "", rel, flags=re.IGNORECASE)
    parts = []
    for part in stripped.split("/"):
        name = re.sub(r"\W", "_", part, flags=re.ASCII)
        if not name or name[0].isdigit():
            name = f"_{name}"
        parts.append(name)
    return "\\".join(parts)


def _annotation_text(annotation: ast.AST | None) -> str | None:
    """Render a type annotation deterministically (string ann kept verbatim)."""
    if annotation is None:
        return None
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value[:200] or None
    unparse = getattr(ast, "unparse", None)
    if unparse is None:
        return None
    try:
        text = unparse(annotation)
    except Exception:  # noqa: BLE001 — annotation rendering is best-effort
        return None
    return text[:200] or None


def _modifiers(node: ast.AST, is_async: bool) -> list[str]:
    """Canonical python modifier list (async first, then decorators)."""
    decorators: set[str] = set()
    for dec in getattr(node, "decorator_list", []) or []:
        if isinstance(dec, ast.Name):
            decorators.add(dec.id)
    mapping = {
        "staticmethod": "static",
        "classmethod": "classmethod",
        "property": "property",
        "abstractmethod": "abstract",
    }
    modifiers = ["async"] if is_async else []
    modifiers += [mapping[name] for name in sorted(mapping) if name in decorators]
    order = {"async": 0, "abstract": 1, "classmethod": 2, "property": 3, "static": 4}
    return sorted(modifiers, key=lambda m: order[m])


def _param_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Plain argument-name strings (no annotations), source order."""
    names: list[str] = []
    args = node.args
    for arg in (*args.posonlyargs, *args.args):
        names.append(arg.arg)
    if args.vararg and args.vararg.arg:
        names.append(f"*{args.vararg.arg}")
    for arg in args.kwonlyargs:
        names.append(arg.arg)
    if args.kwarg and args.kwarg.arg:
        names.append(f"**{args.kwarg.arg}")
    return names


def _docstring(node: ast.AST) -> tuple[bool, str]:
    """(has_docblock, docblock) — docstring kept verbatim, capped."""
    try:
        doc = ast.get_docstring(node, clean=False)
    except Exception:  # noqa: BLE001 — a weird docstring never derails the walk
        return False, ""
    if not doc:
        return False, ""
    return True, doc[:_MAX_DOCBLOCK_CHARS]


def _source_slice(lines: list[str], start: int, end: int | None) -> str:
    """Exact source slice for [start, end] (1-based, inclusive)."""
    if not lines or not start:
        return ""
    stop = end if end is not None else start
    return "".join(lines[start - 1 : stop])[:_MAX_CODE_CHARS]


def _module_import_map(module_body: list[ast.stmt]) -> dict[str, str]:
    """Name → dotted module map from module-level imports (best-effort).

    ``import a.b.c`` binds ``a``; ``import x as y`` binds ``y``;
    ``from pkg.mod import name`` binds ``name`` → ``pkg.mod.name``. Relative
    imports are skipped (their module prefix depends on the package layout).
    """
    mapping: dict[str, str] = {}
    for stmt in module_body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                key = alias.asname or alias.name.split(".", 1)[0]
                if key:
                    mapping[key] = alias.name
        elif isinstance(stmt, ast.ImportFrom):
            if stmt.level:  # relative import — module prefix unknowable here
                continue
            if not stmt.module:
                continue
            for alias in stmt.names:
                if alias.name == "*":
                    continue
                mapping[alias.asname or alias.name] = f"{stmt.module}.{alias.name}"
    return mapping


def _iter_call_nodes(node: ast.AST):
    """Yield every ast.Call under ``node``, skipping nested defs/lambdas/classes."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(
            child,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef),
        ):
            continue
        if isinstance(child, ast.Call):
            yield child
        stack.extend(ast.iter_child_nodes(child))


def _call_fqn(
    call: ast.Call, import_map: dict[str, str], module_namespace: str = "",
    class_name: str | None = None,
) -> str | None:
    """Best-effort dotted call target, else the last attribute segment."""
    func = call.func
    if isinstance(func, ast.Name):
        resolved = import_map.get(func.id)
        if resolved:
            return resolved.replace(".", "\\")
        return f"{module_namespace}\\{func.id}" if module_namespace else func.id
    if isinstance(func, ast.Attribute):
        parts: list[str] = []
        node: ast.AST = func
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name) and node.id in {"self", "cls"} and class_name:
            if len(parts) == 1:
                return f"{module_namespace}\\{class_name}::{parts[0]}"
            return None
        if isinstance(node, ast.Name):
            resolved = import_map.get(node.id)
            if resolved:
                suffix = "\\".join(reversed(parts))
                return f"{resolved}\\{suffix}"
        return parts[0]
    return None


def _calls_of(
    node: ast.AST, import_map: dict[str, str], module_namespace: str,
    class_name: str | None,
) -> list[str]:
    """Sorted, deduped, capped call list for a definition node."""
    targets: set[str] = set()
    for call in _iter_call_nodes(node):
        target = _call_fqn(call, import_map, module_namespace, class_name)
        if target:
            targets.add(target)
    return sorted(targets)[:_MAX_CALLS]


def _definition_chunk(
    *,
    kind: str,
    name: str,
    class_name: str | None,
    module_dotted: str,
    rel: str,
    lines: list[str],
    node: ast.AST,
    start: int,
    end: int | None,
    doc: str,
    has_doc: bool,
    parameters: list[str] | None = None,
    return_type: str | None = None,
    modifiers: list[str] | None = None,
    import_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One flat chunk dict in the exact shape llamaindex_layer consumes."""
    if kind == "class":
        class_fqn = f"{module_dotted}\\{name}"
    elif kind == "method" and class_name:
        class_fqn = f"{module_dotted}\\{class_name}"
    else:
        class_fqn = f"{module_dotted}\\{name}"
    return {
        "type": kind,
        "name": name[: _MAX_NAME_CHARS],
        "class": class_name if kind != "class" else name,
        "class_fqn": class_fqn,
        "namespace": module_dotted,
        "file": rel,
        "start_line": start,
        "end_line": end if end is not None else start,
        "modifiers": modifiers or [],
        "parameters": parameters or [],
        "return_type": return_type,
        "calls": _calls_of(node, import_map or {}, module_dotted, class_name),
        "callers": [],
        "has_docblock": has_doc,
        "docblock": doc,
        "code": _source_slice(lines, start, end),
    }


def _py_file_chunks(
    rel: str, path: Path, lines: list[str], module_dotted: str,
    coverage: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Chunks for one python file — deterministic, never raises."""
    try:
        tree = ast.parse("".join(lines))
    except (SyntaxError, ValueError) as exc:
        if coverage is not None:
            coverage.setdefault("malformed_files", []).append(rel)
            coverage.setdefault("unsupported_constructs", []).append({
                "file": rel,
                "line": getattr(exc, "lineno", None),
                "construct": "malformed_python_syntax",
            })
        return []
    chunks: list[dict[str, Any]] = []
    supported_nodes: set[int] = set()
    import_map = _module_import_map(tree.body)
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            supported_nodes.add(id(stmt))
            has_doc, doc = _docstring(stmt)
            class_name = stmt.name
            chunks.append(
                _definition_chunk(
                    kind="class",
                    name=class_name,
                    class_name=None,
                    module_dotted=module_dotted,
                    rel=rel,
                    lines=lines,
                    node=stmt,
                    start=getattr(stmt, "lineno", 1),
                    end=getattr(stmt, "end_lineno", None),
                    doc=doc,
                    has_doc=has_doc,
                )
            )
            for child in stmt.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    supported_nodes.add(id(child))
                    has_doc, doc = _docstring(child)
                    chunks.append(
                        _definition_chunk(
                            kind="method",
                            name=child.name,
                            class_name=class_name,
                            module_dotted=module_dotted,
                            rel=rel,
                            lines=lines,
                            node=child,
                            start=getattr(child, "lineno", 1),
                            end=getattr(child, "end_lineno", None),
                            doc=doc,
                            has_doc=has_doc,
                            parameters=_param_names(child),
                            return_type=_annotation_text(getattr(child, "returns", None)),
                            modifiers=_modifiers(child, isinstance(child, ast.AsyncFunctionDef)),
                            import_map=import_map,
                        )
                    )
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            supported_nodes.add(id(stmt))
            has_doc, doc = _docstring(stmt)
            chunks.append(
                _definition_chunk(
                    kind="function",
                    name=stmt.name,
                    class_name=None,
                    module_dotted=module_dotted,
                    rel=rel,
                    lines=lines,
                    node=stmt,
                    start=getattr(stmt, "lineno", 1),
                    end=getattr(stmt, "end_lineno", None),
                    doc=doc,
                    has_doc=has_doc,
                    parameters=_param_names(stmt),
                    return_type=_annotation_text(getattr(stmt, "returns", None)),
                    modifiers=_modifiers(stmt, isinstance(stmt, ast.AsyncFunctionDef)),
                    import_map=import_map,
                )
            )
    if coverage is not None:
        local_functions = {
            stmt.name for stmt in tree.body
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        local_methods = {
            (stmt.name, child.name)
            for stmt in tree.body if isinstance(stmt, ast.ClassDef)
            for child in stmt.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        imported_names = set(import_map)
        for node in ast.walk(tree):
            if isinstance(node, ast.Lambda):
                coverage.setdefault("unsupported_constructs", []).append({
                    "file": rel, "line": getattr(node, "lineno", None),
                    "construct": "lambda",
                })
            elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and id(node) not in supported_nodes:
                coverage.setdefault("unsupported_constructs", []).append({
                    "file": rel, "line": getattr(node, "lineno", None),
                    "construct": "nested_definition",
                })
            elif isinstance(node, ast.Call):
                target = None
                if isinstance(node.func, ast.Name):
                    target = node.func.id
                    if target in local_functions:
                        continue
                    construct = "external_or_unresolved_call"
                elif isinstance(node.func, ast.Attribute):
                    receiver = node.func.value
                    if (
                        isinstance(receiver, ast.Name)
                        and receiver.id in {"self", "cls"}
                        and any(method == node.func.attr for _owner, method in local_methods)
                    ):
                        continue
                    target = ast.unparse(node.func)
                    construct = "external_or_dynamic_call"
                else:
                    target = ast.unparse(node.func)
                    construct = "dynamic_call"
                if target in imported_names:
                    construct = "external_import_call"
                coverage.setdefault("unsupported_constructs", []).append({
                    "file": rel, "line": getattr(node, "lineno", None),
                    "construct": construct, "target": str(target)[:160],
                })
    return chunks


def _py_files(root: Path, skip_lower: frozenset[str]) -> list[tuple[str, Path]]:
    """Sorted .py files under ``root`` with skip dirs pruned (never raises)."""
    out: list[tuple[str, Path]] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames if d.lower() not in skip_lower
            )
            parent_rel = ""
            if str(dirpath) != str(root):
                parent_rel = os.path.relpath(str(dirpath), str(root)).replace(
                    os.sep, "/"
                )
            for name in sorted(filenames):
                if Path(name).suffix.lower() != ".py":
                    continue
                rel = f"{parent_rel}/{name}" if parent_rel else name
                out.append((rel, Path(dirpath) / name))
    except OSError:
        return out
    return out


def _py_chunks(
    root: Path,
    skip_lower: frozenset[str],
    progress: Callable[[int, str], None] | None = None,
    coverage: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """AST chunks for up to ``_MAX_PY_FILES`` python files. Pure + total."""
    all_files = _py_files(root, skip_lower)
    files = all_files[:_MAX_PY_FILES]
    if coverage is not None and len(all_files) > _MAX_PY_FILES:
        coverage.setdefault("unsupported_constructs", []).append({
            "construct": "file_count_limit", "limit": _MAX_PY_FILES,
        })
    total = len(files)
    chunks: list[dict[str, Any]] = []
    files_done = 0
    for index, (rel, path) in enumerate(files):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            files_done += 1
            if coverage is not None:
                coverage.setdefault("malformed_files", []).append(rel)
                coverage.setdefault("unsupported_constructs", []).append({
                    "file": rel, "construct": "source_read_error",
                })
            _report(progress, int((index + 1) / total * 100), f"{index + 1}/{total} files")
            continue
        module_dotted = _module_dotted(rel)
        file_chunks = _py_file_chunks(rel, path, lines, module_dotted, coverage)
        chunks.extend(file_chunks)
        files_done += 1
        _report(progress, int((index + 1) / total * 100), f"{index + 1}/{total} files")
    return chunks, files_done


def _report(
    progress: Callable[[int, str], None] | None,
    percent: int,
    label: str,
) -> None:
    """Best-effort progress callback — produce_chunks must never raise."""
    if progress is None:
        return
    try:
        progress(percent, label)
    except Exception:  # noqa: BLE001 — progress reporting must never raise
        return


def _produce_php(workspace_path: str, php_files: int) -> tuple[list[dict[str, Any]], int]:
    """Delegate to the existing pure-PHP CLI code-parse path."""
    from infra.php_tools import run_code_parse

    return run_code_parse(workspace_path), php_files


#: producer name per census primary language.
_PRODUCER_NAMES: dict[str, str] = {
    "php": "code-parse",
    "python": "python-ast",
    "javascript": "tree-sitter-javascript",
    "typescript": "tree-sitter-typescript",
}


def _skip_dir_names_lower() -> frozenset[str]:
    from projects.structure_profile import _SKIP_DIR_NAMES

    return frozenset(name.lower() for name in _SKIP_DIR_NAMES)


def produce_chunks(
    workspace_path: str | Path | None,
    language: str | None = None,
    progress: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    """Dispatch code-chunk production by primary language. Never raises.

    Returns ``{"language", "chunks", "files", "producer", "error"}`` — with a
    truthful error (no chunks) for unsupported languages or a missing
    workspace. The python producer is capped at ``_MAX_PY_FILES`` files.
    ``progress(pct, label)`` fires per parsed file on the python producer only;
    the php single-shot path stays coarse (one CLI call). Callers that omit it
    behave exactly as before.
    """
    root = _resolve_root(workspace_path)
    if root is None:
        return {
            "language": language,
            "chunks": [],
            "files": 0,
            "producer": None,
            "error": f"workspace not found: {workspace_path}",
        }
    try:
        from projects.structure_profile import language_census

        census = language_census(root)
        languages = census.get("languages") or []
        primary = language or census.get("primary")
        if primary is None:
            return {
                "language": None,
                "chunks": [],
                "files": int(census.get("total_files") or 0),
                "producer": None,
                "error": "no language detected in workspace",
            }
        producer = _PRODUCER_NAMES.get(primary)
        if producer is None:
            return {
                "language": primary,
                "chunks": [],
                "files": 0,
                "producer": None,
                "error": f"no indexer for {primary}",
            }
        lang_files = next(
            (int(lang.get("files") or 0) for lang in languages if lang.get("language") == primary),
            0,
        )
        if primary == "php":
            chunks, files = _produce_php(str(root), lang_files)
            return {
                "language": primary,
                "chunks": chunks,
                "files": files,
                "producer": producer,
                "error": None,
                "coverage": "complete",
                "unsupported_constructs": [],
                "malformed_files": [],
            }
        if primary == "python":
            coverage: dict[str, Any] = {"unsupported_constructs": [], "malformed_files": []}
            chunks, files = _py_chunks(root, _skip_dir_names_lower(), progress, coverage)
            return {
                "language": primary,
                "chunks": chunks,
                "files": files,
                "producer": producer,
                "error": None,
                **coverage,
                "coverage": "partial" if coverage["unsupported_constructs"] else "complete",
            }
        if primary in {"javascript", "typescript"}:
            from projects.js_ts_index import produce_chunks as produce_js_ts_chunks

            result = produce_js_ts_chunks(root, primary)
            result.update({
                "language": primary,
                "producer": producer,
                "files": int(result.get("files") or lang_files),
            })
            return result
        # unreachable — kept for total-ness if _PRODUCER_NAMES grows
        return {
            "language": primary,
            "chunks": [],
            "files": 0,
            "producer": None,
            "error": f"no indexer for {primary}",
        }
    except Exception as exc:  # noqa: BLE001 — produce_chunks never raises
        return {
            "language": language,
            "chunks": [],
            "files": 0,
            "producer": None,
            "error": str(exc)[:300],
        }
