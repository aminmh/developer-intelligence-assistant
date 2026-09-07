"""Static Laravel error/semantics extractor.

Answers "what can throw here?", "are custom exceptions used?":
  * custom_exceptions — app/Exceptions/**/*.php classes (custom error types)
  * render_override   — classes overriding render() (custom HTTP handling)
  * throw_hotspots    — per-file counts of `throw new X` in app code (top N)
  * catch_blocks      — total try/catch occurrences per file (top N)
  * handler_file      — bootstrap/app.php withExceptions presence or legacy
                        app/Exceptions/Handler.php

Static only. Honest absence: a project with no app/Exceptions and no throws
reports empty lists.
"""

from __future__ import annotations

import re
from typing import Any

_CLASS_RE = re.compile(r"class\s+(\w+)")
_RENDER_RE = re.compile(r"function\s+render\s*\(")
_REPORT_RE = re.compile(r"function\s+report\s*\(")
_THROW_RE = re.compile(r"throw\s+new\s+[\w\\]+")
_CATCH_RE = re.compile(r"catch\s*\(")
_WITH_EXCEPTIONS_RE = re.compile(r"withExceptions")

_MAX_HOTSPOTS = 8


def extract_errors(
    files: list[tuple[str, str]], bootstrap_app_src: str = ""
) -> dict[str, Any]:
    """Aggregate custom-exception + throw/catch facts.

    ``files`` = [(relative_path, source)] under the framework root.
    """
    exceptions: list[dict[str, Any]] = []
    for rel, src in files:
        if not rel.startswith("app/Exceptions/"):
            continue
        if "/" in rel[len("app/Exceptions/") :] and not rel.endswith(".php"):
            continue
        if not rel.endswith(".php"):
            continue
        cm = _CLASS_RE.search(src)
        exceptions.append(
            {
                "name": cm.group(1) if cm else rel.rsplit("/", 1)[-1].replace(".php", ""),
                "file": rel,
                "extends": (
                    re.search(r"extends\s+([\w\\]+)", src).group(1).split("\\")[-1]
                    if re.search(r"extends\s+([\w\\]+)", src)
                    else None
                ),
                "renders": bool(_RENDER_RE.search(src)),
                "reports": bool(_REPORT_RE.search(src)),
            }
        )

    throw_hotspots: list[dict[str, Any]] = []
    catch_hotspots: list[dict[str, Any]] = []
    for rel, src in files:
        if not rel.startswith("app/"):
            continue
        if rel.endswith(".blade.php"):
            continue
        throws = len(_THROW_RE.findall(src))
        catches = len(_CATCH_RE.findall(src))
        if throws:
            throw_hotspots.append({"file": rel, "throws": throws})
        if catches:
            catch_hotspots.append({"file": rel, "catches": catches})

    throw_hotspots.sort(key=lambda h: h["throws"], reverse=True)
    catch_hotspots.sort(key=lambda h: h["catches"], reverse=True)

    return {
        "custom_exceptions": exceptions,
        "throw_hotspots": throw_hotspots[: _MAX_HOTSPOTS],
        "catch_hotspots": catch_hotspots[: _MAX_HOTSPOTS],
        "handler_configured": bool(
            _WITH_EXCEPTIONS_RE.search(bootstrap_app_src)
            or any(n == "app/Exceptions/Handler.php" for n, _ in files)
        ),
    }
