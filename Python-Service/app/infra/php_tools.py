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
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Optional

from infra.exception_logger import get_exception_logger
from infra.settings import settings

PHP_CLI_BIN = "code-parse"
PHP_STRUCTURE_BIN = "project-export-structure"


def php_bin(name: str) -> str:
    root = Path(settings.php_cli_root or "/app/php")
    return str(root / "bin" / name)


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
    cmd = [php_bin(PHP_CLI_BIN), "--path", project_path, "--output", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode not in (0, 2):
        raise RuntimeError(
            proc.stderr.strip() or proc.stdout.strip() or f"code-parse exited {proc.returncode}"
        )
    stdout = proc.stdout.strip()
    if not stdout:
        return []
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        # The CLI should emit only JSON on stdout, but tolerate a stray leading
        # diagnostic by scanning for the first top-level array.
        match = re.search(r"\[\s*\n?\s*\{", stdout)
        if match:
            depth = 0
            start = match.start()
            for i in range(start, len(stdout)):
                if stdout[i] == "[":
                    depth += 1
                elif stdout[i] == "]":
                    depth -= 1
                    if depth == 0:
                        try:
                            parsed = json.loads(stdout[start : i + 1])
                            return parsed
                        except json.JSONDecodeError:
                            break
        raise RuntimeError("code-parse produced unparseable output")
    return parsed if isinstance(parsed, list) else []


def run_export_structure(project_path: str, depth: int = 4) -> str:
    """Run `php bin/project-export-structure` and return the Markdown blueprint."""
    cmd = [php_bin(PHP_STRUCTURE_BIN), project_path, f"--depth={max(1, depth)}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.strip() or proc.stdout.strip() or f"project-export-structure exited {proc.returncode}"
        )
    return proc.stdout.strip()


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
