"""PHP ``declare(strict_types=1)`` coverage as a maintainability finding (plan task 10).

Without ``strict_types``, PHP silently coerces scalar arguments and return values
(``"5 apples"`` to ``5``), which hides type errors until runtime. The share of
production PHP files that declare it comes from the PHP facts export (nikic/php-parser;
decision: framework-knowledge-lives-in-plugins-over-language-facts), not from text search.

Counted over production code files: PHP files that declare a class-like or a function
(configuration files that only return an array, tests and vendored code are left out).

One finding per revision (not one per file, which would crowd the maintainability
evidence): the ratio and a few example files, located at the first file without the
declaration. The full per-file list goes in the report's analyzer coverage.
"""

from __future__ import annotations

from typing import Any

NAME = "php-strict-types"
VERSION = "php-strict-types-1"
RULE_ID = "php-missing-strict-types"
MAX_LISTED_FILES = 500
EXAMPLES = 3
_SOURCE_ROOTS = ("src/", "app/", "lib/")
MAX_MESSAGE = 240   # the assessment's evidence message limit


def summarize(facts: Any, files: list[str]) -> dict[str, Any] | None:
    """Declared / total over the revision's production PHP code files; None without any."""
    from code_quality.taint_analysis import analysable

    selected = set(files)
    rows = [file for file in facts.files()
            if file.get("path") in selected and analysable(str(file["path"])) and "parse_error" not in file
            and (file.get("classes") or file.get("functions"))]
    if not rows:
        return None
    # Application source first, so examples point at code rather than tooling scripts.
    missing = sorted((str(file["path"]) for file in rows if not file.get("strict_types")),
                     key=lambda path: (not path.startswith(_SOURCE_ROOTS), path))
    total, declared = len(rows), len(rows) - len(missing)
    folders: dict[str, list[int]] = {}
    for file in rows:
        path = str(file["path"])
        counts = folders.setdefault(path.split("/", 1)[0] if "/" in path else ".", [0, 0])
        counts[0] += 1 if file.get("strict_types") else 0
        counts[1] += 1
    by_folder = {name: {"declared": d, "total": t} for name, (d, t) in
                 sorted(folders.items(), key=lambda item: -item[1][1])[:10]}
    return {"name": NAME, "version": VERSION, "total": total, "declared": declared,
            "ratio": round(declared / total, 4), "by_folder": by_folder,
            "missing_files": missing[:MAX_LISTED_FILES], "missing_truncated": len(missing) > MAX_LISTED_FILES}


def finding(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    if not summary or summary["declared"] == summary["total"]:
        return None
    missing = summary["total"] - summary["declared"]
    percent = round(summary["ratio"] * 100)
    message = (f"declare(strict_types=1) in {summary['declared']} of {summary['total']} production PHP code files "
               f"({percent}%); {missing} {'file lets' if missing == 1 else 'files let'} PHP coerce scalar types silently")
    examples: list[str] = []
    for path in summary["missing_files"][:EXAMPLES]:   # whole paths only, within the 240-character evidence limit
        more = ", …" if len(examples) + 1 < missing else ""
        if len(message) + len(", for example ") + len(", ".join([*examples, path])) + len(more) + 1 > MAX_MESSAGE:
            break
        examples.append(path)
    if examples:
        message += ", for example " + ", ".join(examples) + (", …" if len(examples) < missing else "")
    return {"file": summary["missing_files"][0], "line": 1, "rule": RULE_ID, "category": "maintainability",
            "severity": "warning" if summary["ratio"] < 0.5 else "info", "message": message + "."}

def run_on_archive(archive_data: bytes, files: list[str],
                   archive_sha256: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """``(finding, summary)`` for a revision archive; ``(None, None)`` when PHP facts are unavailable."""
    from code_quality.framework_evidence import archive_context

    with archive_context(archive_data, files, archive_sha256) as context:
        facts = context.facts("php")
        summary = summarize(facts, files) if facts is not None else None
    return finding(summary), summary
