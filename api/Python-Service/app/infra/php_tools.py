"""Bridge between Python and the in-image PHP CLI.

The merged python-api image ships a pure-PHP CLI (`/app/php`) that owns the two
heavy code tasks that used to live in the Laravel service:
  * `php bin/code-parse --path <dir> --output json`          → AST chunks
  * `php bin/project-export-structure <dir> [--depth=N]`      → Markdown structure

Python runs them as subprocesses and feeds the results into the property-graph /
Qdrant indexing pipeline. ZipSlip-safe ZIP extraction lives here too.
"""

from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path
from typing import Any, Optional

from infra.exception_logger import get_exception_logger
from infra.php_cli import PhpCli

PHP_CLI_BIN = "code-parse"
PHP_STRUCTURE_BIN = "project-export-structure"


def php_bin(name: str) -> str:
    return PhpCli().binary(name)


def extract_zip_safely(zip_bytes: bytes, dest: str) -> int:
    """Extract a ZIP archive to `dest`, guarding against ZipSlip.

    Returns the number of files extracted. Raises ValueError on a security
    violation and OSError on I/O failures.
    """
    dest_dir = Path(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for info in archive.infolist():
            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(f"Security violation in ZIP paths: {name}")

        for info in archive.infolist():
            target = (dest_dir / info.filename).resolve()
            if not str(target).startswith(str(dest_dir.resolve())):
                raise ValueError(f"Security violation in ZIP paths: {info.filename}")
            archive.extract(info, dest_dir)

    count = sum(1 for _ in dest_dir.rglob("*") if _.is_file())
    return count


def run_code_parse(project_path: str) -> list[dict[str, Any]]:
    """Run `php bin/code-parse --output json` and return the chunk list.

    Raises RuntimeError when the CLI fails; returns [] when no PHP is found.
    """
    try:
        return PhpCli().code_parse(project_path)
    except RuntimeError:
        raise


def run_code_index(
    project_path: str,
    exclude: list[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return AST chunks plus the project-wide PHP symbol/reference export."""
    chunks = run_code_parse(project_path)
    index = PhpCli().inspect_index(project_path, exclude=exclude)
    return {
        "chunks": chunks,
        "definitions": index.get("definitions", []),
        "references": index.get("references", []),
    }


def run_export_structure(project_path: str, depth: int = 4) -> str:
    """Run `php bin/project-export-structure` and return the Markdown blueprint."""
    return PhpCli().export_structure(project_path, depth)


def cap_structure(markdown: str, max_bytes: int = 8000) -> str:
    """Cap the structure Markdown to ~2K tokens, matching the old MCP tool."""
    encoded = markdown.encode("utf-8")
    if len(encoded) <= max_bytes:
        return markdown
    truncated = encoded[:max_bytes]
    cut = truncated.rfind(b"\n")
    content = (truncated[:cut] if cut > 0 else truncated).decode("utf-8", errors="ignore")
    percent = round(len(truncated) / len(encoded) * 100)
    return (
        f"{content}\n\n... (truncated, showing ~{percent}% of "
        f"{len(encoded)} total bytes)"
    )


def safe_rmtree(path: str) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:  # pragma: no cover
        get_exception_logger().warning("workspace cleanup failed: %s", exc)
