"""PHP taint analysis with Psalm (plan task 9).

Decision: @.archcore/code-quality/bundled-security-rules-are-redistributable.adr.md. Psalm (MIT,
pinned in @api/Python-Service/php/psalm/composer.lock) follows user input to dangerous sinks
(shell, SQL, file paths, HTML, URLs, ``unserialize``, ``eval`` ...). Framework sources and sinks
come from the detected framework plugins as stubs (capability ``taint_model``); this module never
names a framework.

Safety: no project code is loaded or executed.

* Psalm's ``--root`` is a directory holding only our ``psalm.xml`` and stubs, so Psalm never
  finds, and never ``require``s, a project ``vendor/autoload.php`` or Composer ``files``.
* Only inventoried ``.php`` source files are extracted (never ``vendor/``, never tests).
* Class-likes the project references but defines in ``vendor/`` are declared empty by
  @api/Python-Service/php/psalm/missing-symbols.php (php-parser only), so Psalm does not abort.
* Time and memory are bounded; the environment is minimal.

Runs on the analyzer worker; the API runs it locally when no worker is configured.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from xml.sax.saxutils import quoteattr

NAME = "psalm"
VERSION = "6.20.5"
TIMEOUT_SECONDS = 600
MEMORY_LIMIT = "2G"
MAX_FINDINGS = 500
MAX_STUBS = 8
MAX_STUB_BYTES = 256_000
MAX_TRACE_STEPS = 16
_STUB_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,60}\.phpstub$")
_TEST_DIRS = {"test", "tests", "spec", "specs"}

# Psalm issue type → (CWE, severity, description).
ISSUES: dict[str, tuple[str, str, str]] = {
    "TaintedShell": ("CWE-78", "error", "OS command injection"),
    "TaintedSql": ("CWE-89", "error", "SQL injection"),
    "TaintedEval": ("CWE-95", "error", "Code injection through eval"),
    "TaintedInclude": ("CWE-98", "error", "File inclusion from user input"),
    "TaintedFile": ("CWE-22", "error", "Path traversal"),
    "TaintedUnserialize": ("CWE-502", "error", "Unsafe deserialization"),
    "TaintedSSRF": ("CWE-918", "error", "Server-side request forgery"),
    "TaintedLdap": ("CWE-90", "error", "LDAP injection"),
    "TaintedXpath": ("CWE-643", "error", "XPath injection"),
    "TaintedCallable": ("CWE-470", "error", "User input chooses the code that runs"),
    "TaintedExtract": ("CWE-621", "error", "Variable overwrite through extract"),
    "TaintedHtml": ("CWE-79", "warning", "Cross-site scripting"),
    "TaintedTextWithQuotes": ("CWE-79", "warning", "Cross-site scripting in a quoted context"),
    "TaintedHeader": ("CWE-113", "warning", "HTTP header injection"),
    "TaintedCookie": ("CWE-565", "warning", "Cookie set from user input"),
    "TaintedSleep": ("CWE-400", "warning", "User input controls a delay"),
    "TaintedSystemSecret": ("CWE-200", "warning", "System secret reaches output"),
    "TaintedUserSecret": ("CWE-200", "warning", "User secret reaches output"),
}


def psalm_root() -> Path:
    return Path(os.environ.get("PSALM_ROOT", "/opt/psalm"))


def available() -> bool:
    root = psalm_root()
    return (shutil.which("php") is not None and (root / "vendor" / "bin" / "psalm").is_file()
            and (root / "missing-symbols.php").is_file())


def _result(status: str, reason: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"name": NAME, "version": VERSION, "status": status, "findings": [], "scanned_files": 0,
            "skipped_files": 0, "supported_languages": ["php"], "reason": reason, **extra}


def analysable(path: str) -> bool:
    """A PHP source file worth tracing: not vendored, not a test."""
    from code_quality.service import _is_excluded_source_path

    parts = [part.lower() for part in PurePosixPath(path).parts[:-1]]
    return path.lower().endswith(".php") and not _is_excluded_source_path(path) and not _TEST_DIRS & set(parts)


def validate_stubs(stubs: Any) -> dict[str, str]:
    """``{name: content}`` taint stubs within limits; raises ValueError otherwise."""
    if stubs is None:
        return {}
    if not isinstance(stubs, dict) or len(stubs) > MAX_STUBS:
        raise ValueError("Taint stubs are invalid.")
    for name, content in stubs.items():
        if (not isinstance(name, str) or not _STUB_NAME.fullmatch(name) or not isinstance(content, str)
                or len(content.encode("utf-8")) > MAX_STUB_BYTES or not content.lstrip().startswith("<?php")):
            raise ValueError("Taint stubs are invalid.")
    return dict(stubs)


def _config(project: Path, stubs: list[Path]) -> str:
    stub_lines = "".join(f"        <file name={quoteattr(str(path))}/>\n" for path in stubs)
    return ('<?xml version="1.0"?>\n'
            '<psalm errorLevel="1" resolveFromConfigFile="true" findUnusedCode="false"'
            ' findUnusedBaselineEntry="false" phpVersion="8.4">\n'
            "    <projectFiles>\n"
            f"        <directory name={quoteattr(str(project))}/>\n"
            "    </projectFiles>\n"
            f"    <stubs>\n{stub_lines}    </stubs>\n"
            "</psalm>\n")


def _relative(path: Any, project: Path) -> str | None:
    if not isinstance(path, str):
        return None
    try:
        return Path(path).resolve().relative_to(project.resolve()).as_posix()
    except ValueError:
        return None


def _trace(issue: dict[str, Any], project: Path) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for step in issue.get("taint_trace") or []:
        if not isinstance(step, dict):
            continue
        file = _relative(step.get("file_path"), project)
        line = step.get("line_from")
        if file is None or type(line) is not int or line < 1:
            continue
        item = {"file": file, "line": line, "code": str(step.get("snippet") or step.get("label") or "").strip()[:160]}
        if not steps or (steps[-1]["file"], steps[-1]["line"]) != (file, line):
            steps.append(item)
    if len(steps) > MAX_TRACE_STEPS:
        steps = steps[:8] + steps[-(MAX_TRACE_STEPS - 8):]
    return steps if len(steps) >= 2 else []


def normalize(issues: list[dict[str, Any]], project: Path, line_counts: dict[str, int]) -> list[dict[str, Any]]:
    """Psalm JSON issues → worker findings (security category, CWE in the rule)."""
    findings = []
    for issue in issues:
        kind = str(issue.get("type") or "")
        if not kind.startswith("Tainted"):
            continue
        cwe, severity, description = ISSUES.get(kind, ("CWE-20", "warning", "User input reaches a sensitive operation"))
        file = _relative(issue.get("file_path"), project)
        line = issue.get("line_from")
        if file is None or type(line) is not int or not 1 <= line <= line_counts.get(file, 0):
            continue
        labels = [str(step.get("label")) for step in issue.get("taint_trace") or [] if isinstance(step, dict) and step.get("label")]
        flow = f": {labels[0]} reaches {labels[-1]}" if len(labels) >= 2 else ""
        finding = {"rule": f"psalm-{kind} ({cwe})", "category": "security", "severity": severity, "file": file,
                   "line": line, "message": f"{description} ({cwe}){flow}."[:600], "tool": NAME, "version": VERSION}
        trace = [step for step in _trace(issue, project) if 1 <= step["line"] <= line_counts.get(step["file"], 0)]
        if len(trace) >= 2:
            finding["trace"] = trace
        findings.append(finding)
        if len(findings) >= MAX_FINDINGS:
            break
    return findings


def _environment(home: Path) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(home), "TMPDIR": str(home),
            "LANG": "C.UTF-8"}


def run_taint_analysis(archive_data: bytes, files: list[str], stubs: dict[str, str] | None = None,
                       timeout: int = TIMEOUT_SECONDS) -> dict[str, Any]:
    """Psalm taint analysis over the inventoried PHP files of a source archive. Never raises."""
    selected = [path for path in files if analysable(path)]
    if not selected:
        return _result("complete", "No PHP source to trace.")
    if not available():
        return _result("unavailable", "Psalm is not installed here.")
    try:
        stubs = validate_stubs(stubs)
    except ValueError as exc:
        return _result("failed", str(exc))
    try:
        return _run(archive_data, selected, stubs, timeout)
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        return _result("failed", f"The source archive could not be prepared for taint analysis ({type(exc).__name__}).")


def _run(archive_data: bytes, selected: list[str], stubs: dict[str, str], timeout: int) -> dict[str, Any]:
    from code_quality.service import MAX_FILE_BYTES, _safe_archive_path

    with tempfile.TemporaryDirectory(prefix="psalm-") as temporary:
        base = Path(temporary)
        project, config, home = base / "project", base / "config", base / "home"
        for directory in (project, config, home):
            directory.mkdir()
        wanted, line_counts, written = set(selected), {}, []
        with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
            for member in archive.infolist():
                relative = _safe_archive_path(member.filename)
                if not relative or relative not in wanted or member.is_dir() or member.file_size > MAX_FILE_BYTES:
                    continue
                data = archive.read(member)
                target = project / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                line_counts[relative] = data.count(b"\n") + 1
                written.append(str(target))
        stub_paths = []
        for name, content in sorted(stubs.items()):
            stub_paths.append(config / name)
            stub_paths[-1].write_text(content, encoding="utf-8")
        listing = config / "files.txt"
        listing.write_text("\n".join(written), encoding="utf-8")
        missing = config / "missing.phpstub"
        php, root = shutil.which("php") or "php", psalm_root()
        try:
            subprocess.run([php, "-d", f"memory_limit={MEMORY_LIMIT}", str(root / "missing-symbols.php"), str(listing),
                            str(missing), *map(str, stub_paths)], cwd=config, env=_environment(home),
                           capture_output=True, stdin=subprocess.DEVNULL, check=True, timeout=min(timeout, 180))
        except (OSError, subprocess.SubprocessError):
            return _result("failed", "Psalm could not prepare declarations for classes outside the project.",
                           scanned_files=0, skipped_files=len(selected))
        (config / "psalm.xml").write_text(_config(project, [*stub_paths, missing]), encoding="utf-8")
        threads = str(max(1, min(4, os.cpu_count() or 1)))
        try:
            run = subprocess.run(
                [php, "-d", f"memory_limit={MEMORY_LIMIT}", str(root / "vendor" / "bin" / "psalm"),
                 f"--config={config / 'psalm.xml'}", f"--root={config}", "--taint-analysis", "--no-cache",
                 "--no-progress", f"--threads={threads}", "--output-format=json"],
                cwd=config, env=_environment(home), capture_output=True, stdin=subprocess.DEVNULL, check=False,
                timeout=timeout)
        except subprocess.TimeoutExpired:
            return _result("partial", f"Psalm taint analysis exceeded {timeout} s.", skipped_files=len(selected))
        except OSError:
            return _result("unavailable", "Psalm could not start.")
        try:
            issues = json.loads(run.stdout.decode("utf-8", "replace") or "null")
        except ValueError:
            issues = None
        if not isinstance(issues, list):
            first = (run.stderr.decode("utf-8", "replace").strip().splitlines() or ["no output"])[0]
            first = first.replace(str(base), "")[:200]
            return _result("failed", f"Psalm taint analysis failed: {first}", skipped_files=len(selected))
        findings = normalize(issues, project, line_counts)
        result = _result("complete", None, scanned_files=len(written), skipped_files=len(selected) - len(written))
        result["findings"] = findings
        if len(findings) >= MAX_FINDINGS:
            result["status"], result["reason"] = "partial", f"Only the first {MAX_FINDINGS} taint findings are kept."
        return result
