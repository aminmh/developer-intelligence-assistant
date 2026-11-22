"""Index-time symbol driver: a language server feeds the symbol graph.

Decision: @.archcore/agent-tools/language-servers-feed-the-symbol-graph-at-index-time.adr.md.
Contract: external-analyzer spec clauses 32-37 and failure clause 8.

`index_workspace` walks one extracted revision, asks the language server for each
file's `documentSymbol`, then for each definition's `references` (and, for classes
and interfaces, `implementation`), and returns the inspector export shape the
symbol graph already loads (`definitions`, `references`; see
`code_quality/scip_index.convert`) with origin ``lsp``.

Call edges come from each reference and the method or function that encloses it,
as the SCIP conversion does, because not every server offers call hierarchy
(phpactor 2025.12 does not). References outside any method or function (imports,
route files, property defaults) have no source node and are dropped.

Isolation: the server runs with its own empty HOME and XDG directories, so neither
the operator's nor the project's configuration is loaded (phpactor reads a
project's `.phpactor.json` only for trusted folders, and trust lives in XDG data),
and the adapter's settings turn off every feature that runs project code. The
caller provides the no-network boundary (the analyzer worker).

Never raises: failures come back as ``status: unavailable`` with a reason.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from lsp.client import LspClient, LspConnectionError, LspTimeoutError

ORIGIN = "lsp"

# LSP SymbolKind → graph kind.
_CLASS_KINDS = {5: "class", 10: "enum", 11: "interface", 23: "class"}
_MEMBER_KINDS = {6: "method", 9: "method", 7: "property", 8: "property", 14: "class_const", 22: "class_const"}
_FUNCTION_KIND = 12
_TOP_CONST_KIND = 14
_SCOPE_KINDS = {"method", "function"}
_DEFAULT_SKIP_DIRS = frozenset({"vendor", "node_modules", ".git", ".svn", ".hg", ".idea", ".vscode"})


@dataclass(frozen=True)
class IndexLimits:
    request_timeout: float = 1.0
    run_timeout: float = 900.0
    prepare_timeout: float = 300.0
    max_files: int = 5000
    max_definitions: int = 20000
    max_references: int = 50000
    skip_reference_names: frozenset = frozenset()

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "IndexLimits":
        def num(key: str, default: float) -> float:
            try:
                return float(cfg.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            request_timeout=num("request_timeout", cls.request_timeout),
            run_timeout=num("run_timeout", cls.run_timeout),
            prepare_timeout=num("prepare_timeout", cls.prepare_timeout),
            max_files=int(num("max_files", cls.max_files)),
            max_definitions=int(num("max_definitions", cls.max_definitions)),
            max_references=int(num("max_references", cls.max_references)),
            skip_reference_names=frozenset(str(n) for n in cfg.get("skip_reference_names") or []),
        )


@dataclass
class _Symbol:
    kind: str           # graph kind
    name: str           # graph name
    fqn: str
    owner: Optional[str]
    file: str           # path relative to the revision root
    line: int           # 1-based selection line
    character: int      # 0-based selection column
    start: int          # 1-based first line of the full range
    end: int            # 1-based last line of the full range


@dataclass
class _Run:
    root: Path
    limits: IndexLimits
    deadline: float
    stats: Dict[str, Any] = field(default_factory=lambda: {
        "files": 0, "definitions": 0, "reference_requests": 0, "implementation_requests": 0,
        "request_timeouts": 0, "request_errors": 0, "skipped_by_name": 0, "outside_root": 0,
        "unscoped": 0, "name_mismatch": 0,
    })
    lines: Dict[str, List[str]] = field(default_factory=dict)

    def token(self, relpath: str, rng: Dict[str, Any]) -> str:
        """Source text at a single-line LSP range ('' when the range spans lines)."""
        if relpath not in self.lines:
            self.lines[relpath] = (self.root / relpath).read_text(encoding="utf-8", errors="replace").splitlines()
        start, end = rng.get("start") or {}, rng.get("end") or {}
        row = int(start.get("line", -1))
        if row != int(end.get("line", -2)) or not 0 <= row < len(self.lines[relpath]):
            return ""
        return self.lines[relpath][row][int(start.get("character", 0)):int(end.get("character", 0))]
    reasons: List[str] = field(default_factory=list)

    def out_of_time(self) -> bool:
        return time.monotonic() >= self.deadline

    def limit(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)


def source_files(root: Path, extensions: Iterable[str], max_files: int,
                 skip_dirs: frozenset = _DEFAULT_SKIP_DIRS) -> Tuple[List[Path], bool]:
    """Sorted source files under root, skipping dependency trees; (files, truncated)."""
    wanted = {e.lower() for e in extensions}
    skip = {d.lower() for d in skip_dirs}
    found: List[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in skip)
        for name in sorted(filenames):
            path = Path(directory, name)
            if path.suffix.lower() in wanted and not path.is_symlink():
                found.append(path)
    found.sort()
    return found[:max_files], len(found) > max_files


def isolated_env(home: str) -> Dict[str, str]:
    """Minimal environment: no inherited secrets, own HOME and XDG directories."""
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": home, "LANG": "C.UTF-8"}
    for var, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        path = os.path.join(home, sub)
        os.makedirs(path, exist_ok=True)
        env[var] = path
    return env


def _line(position: Dict[str, Any]) -> int:
    return int(position.get("line", 0)) + 1


def _symbols_of(items: List[Dict[str, Any]], adapter: Any, relpath: str, namespace: str,
                owner: Optional[str] = None, lines: Optional[List[str]] = None) -> List[_Symbol]:
    """Flatten hierarchical DocumentSymbols into graph symbols with qualified names."""
    found: List[_Symbol] = []
    refine = getattr(adapter, "declaration_kind", None)
    for item in items or []:
        kind_id = int(item.get("kind") or 0)
        full = item.get("range") or (item.get("location") or {}).get("range") or {}
        selection = item.get("selectionRange") or full
        if not full or not selection:
            continue
        raw_name = str(item.get("name") or "")
        start = selection.get("start") or {}
        common = dict(file=relpath, line=_line(start), character=int(start.get("character", 0)),
                      start=_line(full.get("start") or {}), end=_line(full.get("end") or {}))
        children_owner = owner
        if owner is None and kind_id in _CLASS_KINDS:
            fqn = adapter.qualify(namespace, raw_name)
            kind = _CLASS_KINDS[kind_id]
            row = common["line"] - 1
            if refine and lines is not None and 0 <= row < len(lines):
                kind = refine(kind, lines[row], raw_name) or kind  # e.g. a PHP trait reported as a class
            found.append(_Symbol(kind=kind, name=raw_name, fqn=fqn, owner=None, **common))
            children_owner = fqn
        elif owner is None and kind_id == _FUNCTION_KIND:
            fqn = adapter.qualify(namespace, raw_name)
            found.append(_Symbol(kind="function", name=fqn, fqn=fqn, owner=None, **common))
        elif owner is None and kind_id == _TOP_CONST_KIND:
            fqn = adapter.qualify(namespace, raw_name)
            found.append(_Symbol(kind="const", name=raw_name, fqn=fqn, owner=None, **common))
        elif owner is not None and kind_id in _MEMBER_KINDS:
            kind = _MEMBER_KINDS[kind_id]
            name = "__construct" if kind_id == 9 else adapter.member_name(kind, raw_name)
            found.append(_Symbol(kind=kind, name=name, fqn=f"{owner}::{name}", owner=owner, **common))
        found.extend(_symbols_of(item.get("children") or [], adapter, relpath, namespace, children_owner, lines))
    return found


def _definition(symbol: _Symbol) -> Dict[str, Any]:
    record = {"kind": symbol.kind, "name": symbol.name, "fqn": symbol.fqn, "file": symbol.file,
              "line": symbol.line, "end_line": symbol.end, "origin": ORIGIN}
    if symbol.owner:
        record["owner"] = symbol.owner
    return record


def _reference(target: _Symbol, scope: _Symbol, file: str, line: int) -> Optional[Dict[str, Any]]:
    if target.kind == "method" and target.name == "__construct":
        kind, to_fqn, member = "new", target.owner, None
    elif target.kind == "method":
        kind, to_fqn, member = "method_call", target.owner, target.name
    elif target.kind == "class_const":
        kind, to_fqn, member = "class_const", target.owner, target.name
    elif target.kind == "property":
        kind, to_fqn, member = "property_fetch", target.owner, target.name
    elif target.kind == "function":
        kind, to_fqn, member = "function_call", target.fqn, None
    elif target.kind in ("class", "interface", "enum", "trait", "const"):
        kind, to_fqn, member = "class_reference", target.fqn, None
    else:
        return None
    return {"scope": scope.fqn, "kind": kind, "to_fqn": to_fqn, "to_member": member,
            "file": file, "line": line, "origin": ORIGIN}


def _names_target(token: str, target: _Symbol) -> bool:
    """True when the use site spells the target itself.

    Deep reference search also returns uses of subclasses and of classes that use
    a trait (phpactor: `OrderItem::create()` for the `HasUlid` trait); those name
    another symbol and are dropped.
    """
    token = token.lstrip("\\$").rsplit("\\", 1)[-1]
    if target.kind == "method" and target.name == "__construct":
        owner = (target.owner or "").rsplit("\\", 1)[-1]
        return token in ("__construct", owner, "parent", "self", "static")
    return token == target.name.lstrip("$").rsplit("\\", 1)[-1]


def _relative(uri: str, root: Path) -> Optional[str]:
    if not uri.startswith("file://"):
        return None
    try:
        from urllib.parse import unquote

        path = Path(unquote(uri[len("file://"):])).resolve()
        return path.relative_to(root).as_posix()
    except (ValueError, OSError):
        return None


def _enclosing(scopes: List[_Symbol], line: int) -> Optional[_Symbol]:
    inside = [s for s in scopes if s.start <= line <= s.end]
    return min(inside, key=lambda s: s.end - s.start) if inside else None


def _prepare(adapter: Any, argv: List[str], settings: Dict[str, Any], root: Path,
             env: Dict[str, str], timeout: float) -> Optional[str]:
    """Run the adapter's prepare command; returns an error text or None."""
    command = adapter.prepare_argv(argv, settings) if hasattr(adapter, "prepare_argv") else None
    if not command:
        return None
    try:
        proc = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"prepare step timed out after {timeout:g}s"
    except OSError as exc:
        return f"prepare step could not start: {exc}"
    if proc.returncode != 0:
        return f"prepare step exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:300]}"
    return None


def index_workspace(
    root: str,
    adapter: Any,
    server_argv: List[str],
    language_id: str,
    limits: Optional[IndexLimits] = None,
    client_factory: Any = LspClient,
) -> Dict[str, Any]:
    """Index one extracted revision with a language server. Never raises.

    ``server_argv`` is the full server command (interpreter, binary, args).
    Returns ``{name, origin, language, status, reason?, definitions, references,
    truncated, stats, limits_reached}`` where status is complete, partial or unavailable.
    """
    limits = limits or IndexLimits()
    root_path = Path(root).resolve()
    result: Dict[str, Any] = {"name": "lsp", "origin": ORIGIN, "language": language_id, "status": "unavailable",
                              "definitions": [], "references": [], "truncated": False, "stats": {},
                              "limits_reached": []}
    if not root_path.is_dir():
        result["reason"] = "Revision root is not a directory."
        return result
    if not hasattr(adapter, "index_settings"):
        result["reason"] = f"The {language_id} adapter does not support indexing."
        return result

    home = tempfile.mkdtemp(prefix=f"lsp-index-{language_id}-")
    client = None
    started = time.monotonic()
    run = _Run(root=root_path, limits=limits, deadline=started + limits.run_timeout)
    try:
        env = isolated_env(home)
        settings = adapter.index_settings()
        files, files_truncated = source_files(root_path, getattr(adapter, "extensions", []), limits.max_files)
        if files_truncated:
            run.limit("max_files")
        if not files:
            result.update(status="complete", reason="No source files for this language.")
            return result

        error = _prepare(adapter, server_argv, settings, root_path, env, limits.prepare_timeout)
        if error:
            result["reason"] = error
            return result
        prepared = time.monotonic()

        client = client_factory(
            binary=server_argv[0], args=server_argv[1:], language_id=language_id,
            timeout=max(limits.request_timeout, 5.0), root_uri=root_path.as_uri(),
            initialization_options=settings, env=env, cwd=str(root_path),
        )
        try:
            client.start()
        except (LspConnectionError, OSError) as exc:
            result["reason"] = f"Language server could not start: {exc}"[:300]
            return result
        if not client.supports("documentSymbol") or not client.supports("references"):
            result["reason"] = "Language server lacks documentSymbol or references support."
            return result

        mark = time.monotonic()
        symbols, scopes = _collect_symbols(client, adapter, files, run)
        definitions = _limit_definitions(symbols, run)
        run.stats["symbols_s"], mark = round(time.monotonic() - mark, 2), time.monotonic()
        references = _collect_references(client, definitions, scopes, run)
        run.stats["references_s"], mark = round(time.monotonic() - mark, 2), time.monotonic()
        relations = _collect_implementations(client, definitions, run)
        run.stats["implementations_s"] = round(time.monotonic() - mark, 2)

        records = []
        for symbol in definitions:
            record = _definition(symbol)
            if symbol.fqn in relations:
                record["relations"] = relations[symbol.fqn]
            records.append(record)
        result["definitions"] = records
        result["references"] = references
        run.stats["definitions"] = len(records)
        run.stats["references"] = len(references)
        run.stats["prepare_s"] = round(prepared - started, 2)
        run.stats["total_s"] = round(time.monotonic() - started, 2)
        if run.stats["request_timeouts"]:
            run.limit("request_timeout")
        if run.stats["request_errors"]:
            run.limit("request_errors")
        result["limits_reached"] = run.reasons
        result["truncated"] = bool(run.reasons)
        result["status"] = "partial" if run.reasons else "complete"
        return result
    except Exception as exc:  # noqa: BLE001 - never raise into indexing
        result["reason"] = f"LSP indexing failed: {exc}"[:300]
        return result
    finally:
        result["stats"] = run.stats
        if client is not None:
            try:
                client.shutdown()
            except Exception:
                pass
        shutil.rmtree(home, ignore_errors=True)


def _collect_symbols(client: Any, adapter: Any, files: List[Path], run: _Run):
    symbols: List[_Symbol] = []
    scopes: Dict[str, List[_Symbol]] = {}
    for path in files:
        if run.out_of_time():
            run.limit("run_timeout")
            break
        relpath = path.relative_to(run.root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        uri = path.as_uri()
        client.open_document(uri, text)
        try:
            items = client.document_symbols(uri, timeout=max(run.limits.request_timeout, 5.0))
        except LspTimeoutError:
            run.stats["request_timeouts"] += 1
            continue
        except LspConnectionError:
            run.stats["request_errors"] += 1
            continue
        finally:
            client.close_document(uri)
        found = _symbols_of(items, adapter, relpath, adapter.namespace_of(text), lines=text.splitlines())
        symbols.extend(found)
        scopes[relpath] = [s for s in found if s.kind in _SCOPE_KINDS]
        run.stats["files"] += 1
    return symbols, scopes


def _limit_definitions(symbols: List[_Symbol], run: _Run) -> List[_Symbol]:
    seen, kept = set(), []
    for symbol in symbols:
        key = (symbol.kind, symbol.fqn)
        if key in seen:
            continue
        if len(kept) >= run.limits.max_definitions:
            run.limit("max_definitions")
            break
        seen.add(key)
        kept.append(symbol)
    return kept


class _OpenFile:
    """Keeps one document open at a time: servers such as phpactor answer only for open documents."""

    def __init__(self, client: Any, root: Path) -> None:
        self._client, self._root, self._uri = client, root, None

    def uri(self, relpath: str) -> str:
        uri = (self._root / relpath).as_uri()
        if uri != self._uri:
            self.close()
            text = (self._root / relpath).read_text(encoding="utf-8", errors="replace")
            self._client.open_document(uri, text)
            self._uri = uri
        return uri

    def close(self) -> None:
        if self._uri is not None:
            self._client.close_document(self._uri)
            self._uri = None


def _collect_references(client: Any, definitions: List[_Symbol], scopes: Dict[str, List[_Symbol]],
                        run: _Run) -> List[Dict[str, Any]]:
    references: Dict[Tuple, Dict[str, Any]] = {}
    opened = _OpenFile(client, run.root)
    try:
        _reference_loop(client, definitions, scopes, run, references, opened)
    finally:
        opened.close()
    return sorted(references.values(), key=lambda r: (r["file"], r["line"], r["scope"], r["to_fqn"] or "",
                                                       r["to_member"] or ""))


def _reference_loop(client: Any, definitions: List[_Symbol], scopes: Dict[str, List[_Symbol]], run: _Run,
                    references: Dict[Tuple, Dict[str, Any]], opened: "_OpenFile") -> None:
    for target in definitions:
        if target.name in run.limits.skip_reference_names:
            run.stats["skipped_by_name"] += 1
            continue
        if run.out_of_time():
            run.limit("run_timeout")
            break
        if len(references) >= run.limits.max_references:
            run.limit("max_references")
            break
        run.stats["reference_requests"] += 1
        try:
            locations = client.references(opened.uri(target.file), target.line - 1, target.character,
                                          timeout=run.limits.request_timeout)
        except LspTimeoutError:
            run.stats["request_timeouts"] += 1
            run.stats.setdefault("timed_out", []).append(target.fqn)
            continue
        except LspConnectionError:
            run.stats["request_errors"] += 1
            continue
        for location in locations:
            relpath = _relative(str(location.get("uri") or ""), run.root)
            if relpath is None:
                run.stats["outside_root"] += 1
                continue
            rng = location.get("range") or {}
            line = _line(rng.get("start") or {})
            if relpath == target.file and line == target.line:
                continue  # the declaration itself
            if not _names_target(run.token(relpath, rng), target):
                run.stats["name_mismatch"] += 1
                continue
            scope = _enclosing(scopes.get(relpath, []), line)
            if scope is None:
                run.stats["unscoped"] += 1
                continue
            record = _reference(target, scope, relpath, line)
            if record is None:
                continue
            key = (record["scope"], record["kind"], record["to_fqn"], record["to_member"], line)
            if key not in references:
                if len(references) >= run.limits.max_references:
                    run.limit("max_references")
                    break
                references[key] = record


def _collect_implementations(client: Any, definitions: List[_Symbol], run: _Run) -> Dict[str, List[Dict[str, str]]]:
    """`implementation` on classes and interfaces → extends/implements relations on the implementors."""
    if not client.supports("implementation"):
        return {}
    by_location = {(s.file, s.line): s for s in definitions if s.kind in ("class", "interface", "enum", "trait")}
    relations: Dict[str, List[Dict[str, str]]] = {}
    opened = _OpenFile(client, run.root)
    try:
        _implementation_loop(client, definitions, run, by_location, relations, opened)
    finally:
        opened.close()
    return relations


def _implementation_loop(client: Any, definitions: List[_Symbol], run: _Run, by_location: Dict,
                         relations: Dict[str, List[Dict[str, str]]], opened: "_OpenFile") -> None:
    for target in definitions:
        if target.kind not in ("class", "interface", "trait"):
            continue
        if run.out_of_time():
            run.limit("run_timeout")
            break
        run.stats["implementation_requests"] += 1
        try:
            locations = client.implementations(opened.uri(target.file), target.line - 1,
                                               target.character, timeout=run.limits.request_timeout)
        except LspTimeoutError:
            run.stats["request_timeouts"] += 1
            continue
        except LspConnectionError:
            run.stats["request_errors"] += 1
            continue
        for location in locations:
            relpath = _relative(str(location.get("uri") or location.get("targetUri") or ""), run.root)
            rng = location.get("range") or location.get("targetSelectionRange") or {}
            implementor = by_location.get((relpath, _line(rng.get("start") or {}))) if relpath else None
            if implementor is None or implementor.fqn == target.fqn:
                continue
            kind = {"interface": "implements", "trait": "uses_trait"}.get(target.kind, "extends")
            entry = {"kind": kind, "target": target.fqn}
            bucket = relations.setdefault(implementor.fqn, [])
            if entry not in bucket:
                bucket.append(entry)
