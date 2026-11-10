"""Compact project layout handed to the agent up front.

Without it the model explores the tree call by call (`project_structure` at
depth 1, then 2, then 3), which costs a model round trip and the whole prompt
each time. This overview is built from the file system in one bounded walk,
skips excluded and secret paths, and fits in about 2 KB.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

from agent_tools.source import SourceRoot

MAX_FILES = 20_000
MAX_CHARS = 2_400
_LIST_FILES_UNDER = 24
_MAX_CHILDREN = 20
_JUNK_FILES = {".ds_store", "thumbs.db", "desktop.ini"}

_LANGUAGES = {
    ".php": "PHP", ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".vue": "Vue", ".go": "Go", ".java": "Java", ".kt": "Kotlin",
    ".cs": "C#", ".rb": "Ruby", ".rs": "Rust", ".swift": "Swift", ".scala": "Scala", ".c": "C", ".h": "C",
    ".cpp": "C++", ".hpp": "C++", ".sql": "SQL",
}

_cache: dict[tuple[str, float], str] = {}


def _walk(root: SourceRoot) -> tuple[dict[str, list[str]], Counter[str], int, bool]:
    """Return (files per relative directory, language counts, file count, truncated)."""
    tree: dict[str, list[str]] = {}
    languages: Counter[str] = Counter()
    count = 0
    for dirpath, dirnames, filenames in os.walk(root.path):
        rel_dir = os.path.relpath(dirpath, root.path)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        dirnames[:] = sorted(d for d in dirnames
                             if not os.path.islink(os.path.join(dirpath, d))
                             and not root.is_excluded(f"{rel_dir}/{d}/x" if rel_dir else f"{d}/x"))
        kept = []
        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if name.lower() in _JUNK_FILES or root.is_excluded(rel):
                continue
            kept.append(name)
            languages[_LANGUAGES.get(Path(name).suffix.lower(), "")] += 1
            count += 1
            if count >= MAX_FILES:
                tree[rel_dir] = kept
                return tree, languages, count, True
        tree[rel_dir] = kept
    return tree, languages, count, False


def _subtree_count(tree: dict[str, list[str]], directory: str) -> int:
    prefix = directory + "/"
    return sum(len(files) for path, files in tree.items() if path == directory or path.startswith(prefix))


def _children(tree: dict[str, list[str]], directory: str) -> list[str]:
    depth = directory.count("/") + 1 if directory else 0
    prefix = directory + "/" if directory else ""
    return sorted({p.split("/")[depth] for p in tree if p.startswith(prefix) and p != directory and p.count("/") >= depth})


def build_overview(root: SourceRoot) -> str:
    """Two-level layout with file counts, small directories listed by file name."""
    try:
        stamp = root.path.stat().st_mtime
    except OSError:
        return ""
    key = (str(root.path), stamp)
    if key in _cache:
        return _cache[key]

    tree, languages, count, truncated = _walk(root)
    lines = ["Project layout (paths in tool calls and citations are relative to this root):"]
    # Root files: project files first; dotfiles add little and crowd the line.
    top_files = sorted(tree.get("", []), key=lambda n: (n.startswith("."), n.lower()))
    if top_files:
        shown = [n for n in top_files if not n.startswith(".")][:_MAX_CHILDREN] or top_files[:_MAX_CHILDREN]
        lines.append("./ " + ", ".join(shown) + (" …" if len(top_files) > len(shown) else ""))
    for top in _children(tree, ""):
        total = _subtree_count(tree, top)
        if not total:
            continue
        subdirs = [d for d in _children(tree, top) if _subtree_count(tree, f"{top}/{d}")]
        files = tree.get(top, [])
        if not subdirs and len(files) <= _LIST_FILES_UNDER:
            detail = ", ".join(files)
        else:
            parts = [f"{d}/ ({_subtree_count(tree, f'{top}/{d}')})" for d in subdirs[:_MAX_CHILDREN]]
            if len(subdirs) > _MAX_CHILDREN:
                parts.append("…")
            if files and len(files) <= 4:
                parts.extend(files)
            elif files:
                parts.append(f"{len(files)} files")
            detail = ", ".join(parts)
        lines.append(f"{top}/ ({total} files): {detail}")
    named = [(lang, n) for lang, n in languages.most_common() if lang][:6]
    if named:
        lines.append("Languages: " + ", ".join(f"{lang} {n}" for lang, n in named)
                     + f"; {count}{'+' if truncated else ''} files in total.")
    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rsplit("\n", 1)[0] + "\n…"
    if len(_cache) > 16:
        _cache.clear()
    _cache[key] = text
    return text
