"""Documentation evidence: do the docs match the revision? (gap-closing plan, task 17).

Framework-neutral and read-only (evidence spec clause 6). Reads README, agent instructions
(CLAUDE.md, AGENTS.md, ...), ADRs and other docs, and checks what they name against the revision:

* ``make <target>``, ``composer <script>``, ``npm|yarn|pnpm run <script>`` → the target or script exists;
* file paths in inline code (``config/app.yaml``) → the file or folder exists;
* toolchain versions (``PHP 8.3``, ``Node 20``) → they match what manifests, version files and images declare.

Reports each documentation file (info), a missing README (warning), and each stale reference (warning).
Commands are only read, never run.
"""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any

from agent.core.capability import Capability
from code_quality.evidence_checks import capped, declared_versions, files_of_kind, finding, plural

NAME = "documentation-evidence"
VERSION = "documentation-evidence-1"
CATEGORY = "documentation"
_MARKDOWN = (".md", ".mdx", ".markdown", ".rst", ".adoc", ".txt")
_COMPOSER_BUILTIN = {"install", "update", "require", "remove", "dump-autoload", "dumpautoload", "create-project", "show",
                     "outdated", "validate", "diagnose", "self-update", "global", "init", "config", "exec", "audit",
                     "run", "run-script", "bump", "why", "why-not", "depends", "licenses", "check-platform-reqs",
                     "clear-cache", "status", "search", "fund", "reinstall", "archive", "browse", "help", "list", "i", "u"}
_NPM_BUILTIN = {"install", "i", "ci", "add", "remove", "uninstall", "update", "upgrade", "init", "create", "exec", "dlx",
                "audit", "outdated", "publish", "link", "login", "pack", "version", "why", "list", "ls", "cache", "config",
                "dedupe", "help", "x", "global", "set", "info", "view"}
_COMMAND = re.compile(r"(?:^|[\s`$>;&|(])(make|composer|npm|yarn|pnpm|bun)\s+(?:(?:run|run-script)\s+)?([A-Za-z0-9:_\-.]+)")
_INLINE = re.compile(r"`([^`\n]{3,200})`")
_PATH = re.compile(r"^\.?/?[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+/?$")
# A bare file name (`CONTEXT.md`); present when any file in the revision has that name.
_BARE_FILE = re.compile(r"^[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*\.(md|mdx|rst|txt|ya?ml|json|xml|toml|ini|neon|dist|sh|php|py|js|ts)$")
_VERSION_MENTION = re.compile(r"(?i)\b(PHP|Node(?:\.js)?|Python)\s*(?:version\s*)?(?:>=|≥|v)?\s*(\d+(?:\.\d+)?)(?!\.\d)")
_TOOLCHAIN = {"php": "php", "node": "node", "node.js": "node", "python": "python"}


def _scripts(context: Any, path: str) -> set[str] | None:
    text = context.read_text(path, 512_000)
    if text is None:
        return None
    try:
        scripts = (json.loads(text) or {}).get("scripts")
    except ValueError:
        return None
    return set(scripts) if isinstance(scripts, dict) else set()


def _make_targets(context: Any) -> set[str] | None:
    text = next((context.read_text(name, 256_000) for name in ("Makefile", "makefile", "GNUmakefile") if context.exists(name)), None)
    if text is None:
        return None
    return {m.group(1) for m in re.finditer(r"(?m)^([A-Za-z0-9_.\-/]+)\s*:(?!=)", text)}


def _checkable(token: str) -> bool:
    """Only files the revision tree can hold: source or evidence files outside excluded folders."""
    from code_quality.evidence_files import SECRET, classify, is_excluded_path
    from code_quality.service import LANGUAGE_BY_SUFFIX

    name = PurePosixPath(token).name
    if token.endswith("/") or "." not in name or is_excluded_path(token):
        return False
    kind = classify(token)
    return kind not in (None, SECRET) or PurePosixPath(name).suffix.lower() in LANGUAGE_BY_SUFFIX


def _exists(context: Any, files: set[str], folders: set[str], doc: str, reference: str) -> bool:
    reference = reference.strip().removeprefix("./").rstrip("/")
    candidates = {reference, str(PurePosixPath(doc).parent / reference)}
    if any(c in files or c in folders for c in candidates):
        return True
    # Docs often cite the tail of a path (`Admin/Settings.page` for resources/pages/Admin/Settings.page).
    suffix = "/" + reference
    return any(path.endswith(suffix) for path in files) or any(folder.endswith(suffix) for folder in folders)


def _doc_files(context: Any) -> list[str]:
    return sorted(p for p in files_of_kind(context, "docs") if p.lower().endswith(_MARKDOWN) or "." not in p.rsplit("/", 1)[-1])


def run(collections: dict[Capability, Any], context: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from agent.core.revision import is_secret_path

    docs = _doc_files(context)
    # Existence is checked against every file in the revision, not only the files extracted for checks.
    files = set(getattr(context, "archive_files", None) or context.files)
    folders = {str(parent) for path in files for parent in PurePosixPath(path).parents if str(parent) != "."}
    basenames = {PurePosixPath(path).name for path in files}
    make = _make_targets(context)
    composer = _scripts(context, "composer.json")
    package = _scripts(context, "package.json")
    declared: dict[str, set[str]] = {}
    for row in declared_versions(context):
        declared.setdefault(row.toolchain, set()).add(row.version)
    rows, checked, stale = [], 0, 0
    for doc in docs:
        text = context.read_text(doc, 512_000)
        if text is None:
            continue
        lines = text.splitlines()
        rows.append(finding(doc, 1, "documentation-file", CATEGORY, "info",
                            f"{doc}: {plural(len(lines), 'line')}, {plural(sum(1 for row in lines if row.startswith('#')), 'heading')}."))
        in_fence = False
        for number, row in enumerate(lines, 1):
            if row.lstrip().startswith(("```", "~~~")):
                in_fence = not in_fence
            # In code blocks, shell comments are prose ("# run composer inside the container").
            code = re.sub(r"(^|\s)#.*$", "", row) if in_fence else " ".join(_INLINE.findall(row))
            for tool, name in _COMMAND.findall(code):
                known = {"make": make, "composer": composer, "npm": package, "yarn": package, "pnpm": package, "bun": package}[tool]
                builtin = _COMPOSER_BUILTIN if tool == "composer" else set() if tool == "make" else _NPM_BUILTIN
                if known is None or name in builtin or name.startswith("-"):
                    continue
                checked += 1
                if name not in known:
                    stale += 1
                    where = "Makefile" if tool == "make" else "composer.json" if tool == "composer" else "package.json"
                    rows.append(finding(doc, number, "documentation-stale-command", CATEGORY, "warning",
                                        f"{doc} tells readers to run '{tool} {name}', but {where} defines no '{name}'."))
            for token in ([] if in_fence else _INLINE.findall(row)):
                token = token.strip()
                if _BARE_FILE.match(token) and not is_secret_path(token) and "." in token:
                    checked += 1
                    if token not in basenames:
                        stale += 1
                        rows.append(finding(doc, number, "documentation-missing-path", CATEGORY, "warning",
                                            f"{doc} refers to {token}, which is not in this revision."))
                    continue
                if (not _PATH.match(token) or "://" in token or token.startswith(("vendor/", "node_modules/", "/"))
                        or is_secret_path(token) or re.search(r"[<>{}*$]", token) or not _checkable(token)):
                    continue
                checked += 1
                if not _exists(context, files, folders, doc, token):
                    stale += 1
                    rows.append(finding(doc, number, "documentation-missing-path", CATEGORY, "warning",
                                        f"{doc} refers to {token}, which is not in this revision."))
            for tool, version in _VERSION_MENTION.findall(row):
                toolchain = _TOOLCHAIN.get(tool.lower())
                versions = declared.get(toolchain or "")
                if not versions or "." not in version:
                    continue
                checked += 1
                if not any(v == version or v.startswith(version + ".") or version.startswith(v + ".") for v in versions):
                    stale += 1
                    rows.append(finding(doc, number, "documentation-version-mismatch", CATEGORY, "warning",
                                        f"{doc} says {tool} {version}, but the revision declares {tool} {', '.join(sorted(versions))}."))
    has_readme = any(PurePosixPath(p).name.lower().startswith("readme") and "/" not in p for p in docs)
    manifests = sorted(files_of_kind(context, "manifest"))
    if not has_readme and manifests:
        rows.append(finding(manifests[0], 1, "documentation-no-readme", CATEGORY, "warning",
                            "There is no README at the project root, so setup and run instructions are not documented."))
    summary = {"documentation_files": len(docs), "references_checked": checked, "stale_references": stale}
    if not rows:
        summary["reason"] = "No documentation files were found in this revision."
    return capped(rows, summary)


def check():
    from code_quality.framework_evidence import FrameworkCheck

    return FrameworkCheck(NAME, VERSION, (), run, accepts_context=True, runs_without_providers=True, category=CATEGORY)
