"""Secrets committed to Git history (plan task 12).

Decision: @.archcore/code-quality/bundled-security-rules-are-redistributable.adr.md. The scanner is
Betterleaks (MIT; the maintained successor of Gitleaks, by the same authors), pinned by version
and checksum in the analyzer worker and API images. Its own bundled rules are used.

Only Git-synchronized revisions carry history. When the API fetches a branch it also fetches up to
``HISTORY_DEPTH`` commits behind the head and stores them next to the source archive as a *history
artifact*: a stored zip with ``history.bundle`` (a Git bundle of ``refs/heads/history``) and
``shallow`` (the commits whose parents were not fetched). Uploaded archives have no history.

Safety:

* The scanner never sees the project's working tree: it scans a bare repository built from the
  bundle, so the project cannot supply a scanner configuration, an ignore file or Git attributes.
  ``betterleaks:allow`` / ``gitleaks:allow`` comments are ignored too.
* Validation (live requests to credential providers) is never enabled; the worker has no network.
* Git runs with no system or global configuration.
* Only the rule, file, line, commit and commit date of a finding are kept. The secret, the matched
  text, the surrounding lines and the commit author never leave this module; the scanner's report
  file is deleted with its temporary directory.
"""

from __future__ import annotations

import io
import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

NAME = "betterleaks"
VERSION = "1.9.0"
CHECK_VERSION = "history-secrets-2"
RULE_PREFIX = "history-secret-"
GENERIC_RULE_PREFIX = "generic-"
MAX_MESSAGE = 240
HISTORY_REF = "refs/heads/history"
HISTORY_DEPTH = 500
MAX_HISTORY_BYTES = 64 * 1024 * 1024
MAX_SHALLOW_COMMITS = 10_000
MAX_FINDINGS = 200
MAX_TARGET_MEGABYTES = 5
TIMEOUT_SECONDS = 300
BUNDLE_ENTRY = "history.bundle"
SHALLOW_ENTRY = "shallow"
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_RULE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_ARCHIVE_KEY = re.compile(r"telemetry-sources/[0-9a-f]{64}/[0-9a-f]{64}\.zip")
_HISTORY_KEY = re.compile(r"telemetry-sources/[0-9a-f]{64}/history-(?:[0-9a-f]{40}|[0-9a-f]{64})\.zip")
_STATUSES = {"complete", "failed", "unavailable", "disabled"}


# --- History artifact ---------------------------------------------------------------------------

def pack_history(bundle: bytes, shallow: list[str]) -> bytes:
    """The stored history artifact: the bundle and its shallow boundary, uncompressed."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(BUNDLE_ENTRY, bundle)
        archive.writestr(SHALLOW_ENTRY, "".join(f"{commit}\n" for commit in shallow))
    return output.getvalue()


def unpack_history(data: bytes, commit_sha: str) -> tuple[bytes, list[str]]:
    """``(bundle, shallow commits)`` from a history artifact of ``commit_sha``; ValueError otherwise."""
    if not isinstance(commit_sha, str) or not _SHA.fullmatch(commit_sha):
        raise ValueError("The revision commit is invalid.")
    if not isinstance(data, bytes) or not data or len(data) > MAX_HISTORY_BYTES + 1024 * 1024:
        raise ValueError("The history artifact exceeds its size limit.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = {info.filename: info for info in archive.infolist()}
            if set(entries) != {BUNDLE_ENTRY, SHALLOW_ENTRY} or len(archive.infolist()) != 2:
                raise ValueError("The history artifact has unexpected entries.")
            if entries[BUNDLE_ENTRY].file_size > MAX_HISTORY_BYTES or entries[SHALLOW_ENTRY].file_size > 70 * MAX_SHALLOW_COMMITS:
                raise ValueError("The history artifact exceeds its size limit.")
            bundle = archive.read(BUNDLE_ENTRY)
            shallow_text = archive.read(SHALLOW_ENTRY).decode("ascii")
    except (zipfile.BadZipFile, UnicodeError, OSError, KeyError) as exc:
        raise ValueError("The history artifact is malformed.") from exc
    shallow = shallow_text.split()
    if len(shallow) > MAX_SHALLOW_COMMITS or any(not _SHA.fullmatch(line) for line in shallow):
        raise ValueError("The history artifact's shallow boundary is invalid.")
    _check_bundle_header(bundle, commit_sha)
    return bundle, shallow


def _check_bundle_header(bundle: bytes, commit_sha: str) -> None:
    """A v2 or v3 bundle with no prerequisites and exactly ``<commit> refs/heads/history``."""
    end = bundle.find(b"\n\n")
    if end < 0 or end > 64 * 1024:
        raise ValueError("The history bundle header is malformed.")
    try:
        lines = bundle[:end].decode("ascii").split("\n")
    except UnicodeError as exc:
        raise ValueError("The history bundle header is malformed.") from exc
    if lines[0] not in {"# v2 git bundle", "# v3 git bundle"}:
        raise ValueError("The history bundle format is not supported.")
    body = [line for line in lines[1:] if not (lines[0] == "# v3 git bundle" and line.startswith("@"))]
    if body != [f"{commit_sha} {HISTORY_REF}"]:
        raise ValueError("The history bundle does not hold exactly the revision's commit.")


def history_key(archive_key: str, commit_sha: str) -> str:
    """Object key of a revision's history artifact, next to its source archive."""
    if not isinstance(archive_key, str) or not _ARCHIVE_KEY.fullmatch(archive_key):
        raise ValueError("Invalid source archive key.")
    if not isinstance(commit_sha, str) or not _SHA.fullmatch(commit_sha):
        raise ValueError("Invalid commit.")
    key = f"{posixpath.dirname(archive_key)}/history-{commit_sha}.zip"
    assert _HISTORY_KEY.fullmatch(key)
    return key


def is_history_key(key: str) -> bool:
    return isinstance(key, str) and bool(_HISTORY_KEY.fullmatch(key))


def load_history(project_id: str, snapshot: dict[str, Any], registry: Any = None, storage: Any = None) -> bytes | None:
    """The stored history artifact of a Git revision, or None when the revision has none."""
    commit_sha = snapshot.get("commit_sha")
    if not commit_sha or snapshot.get("dirty") or not _SHA.fullmatch(str(commit_sha)):
        return None
    if registry is None:
        from telemetry.registry import default_registry
        registry = default_registry()
    if storage is None:
        from index.storage import storage as default_storage
        storage = default_storage
    archive_key = registry.archive_key(project_id, commit_sha)
    if not archive_key or not _ARCHIVE_KEY.fullmatch(archive_key):
        return None
    try:
        return storage.get_snapshot_archive(history_key(archive_key, commit_sha))
    except Exception as exc:  # noqa: BLE001 — a missing object means "no history"; anything else propagates
        if getattr(exc, "code", None) in {"NoSuchKey", "NoSuchObject"} or isinstance(exc, KeyError):
            return None
        raise


# --- Scanner ------------------------------------------------------------------------------------

def scanner() -> str | None:
    configured = os.environ.get("BETTERLEAKS_BIN")
    if configured:
        return configured if Path(configured).is_file() and os.access(configured, os.X_OK) else None
    return shutil.which("betterleaks")


def available() -> bool:
    return scanner() is not None and shutil.which("git") is not None


def _result(status: str, reason: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"name": NAME, "version": VERSION, "status": status, "findings": [], "commits_scanned": 0,
            "shallow": False, "reason": reason, **extra}


def not_applicable(reason: str) -> dict[str, Any]:
    return _result("disabled", reason)


def _environment(home: Path) -> dict[str, str]:
    """No inherited scanner or Git configuration (``BETTERLEAKS_CONFIG*`` would replace the rules)."""
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C"}


def _git(args: list[str], env: dict[str, str], cwd: Path, timeout: int) -> str:
    run = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, stdin=subprocess.DEVNULL,
                         check=True, timeout=timeout)
    return run.stdout.decode("utf-8", "replace").strip()


def run_history_scan(data: bytes, commit_sha: str, timeout: int = TIMEOUT_SECONDS) -> dict[str, Any]:
    """Scan a history artifact; never raises (the outcome is in ``status`` and ``reason``)."""
    if not available():
        return _result("unavailable", "The pinned Betterleaks scanner or Git is not installed.")
    try:
        bundle, shallow = unpack_history(data, commit_sha)
    except ValueError as exc:
        return _result("failed", str(exc))
    try:
        return _run(bundle, shallow, commit_sha, timeout)
    except subprocess.TimeoutExpired:
        return _result("failed", f"The history scan did not finish within {timeout} seconds.")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return _result("failed", f"The history scan failed ({type(exc).__name__}).")


def _run(bundle: bytes, shallow: list[str], commit_sha: str, timeout: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="history-secrets-") as temp:
        root = Path(temp)
        home, empty, repo = root / "home", root / "empty", root / "repo.git"
        for folder in (home, empty):
            folder.mkdir(mode=0o700)
        env = _environment(home)
        bundle_path = root / "history.bundle"
        bundle_path.write_bytes(bundle)
        _git(["init", "--quiet", "--bare", str(repo)], env, root, 60)
        if shallow:
            (repo / "shallow").write_text("".join(f"{commit}\n" for commit in shallow), encoding="ascii")
        # unbundle indexes the pack without a connectivity walk, so shallow history is accepted.
        _git(["bundle", "unbundle", str(bundle_path)], env, repo, timeout)
        _git(["update-ref", HISTORY_REF, commit_sha], env, repo, 60)
        if _git(["rev-parse", "--verify", f"{HISTORY_REF}^{{commit}}"], env, repo, 60) != commit_sha:
            raise ValueError("The history bundle does not resolve to the revision's commit.")
        commits = int(_git(["rev-list", "--count", HISTORY_REF], env, repo, 120) or 0)
        bundle_path.unlink()
        report = root / "report.json"
        run = subprocess.run(
            [str(scanner()), "git", str(repo), "--no-banner", "--no-color", "--redact=100",
             "--report-format", "json", "--report-path", str(report), "--exit-code", "0",
             "--log-level", "error", "--max-target-megabytes", str(MAX_TARGET_MEGABYTES),
             "--gitleaks-ignore-path", str(empty), "--ignore-gitleaks-allow",
             "--timeout", str(max(30, timeout - 30))],
            cwd=empty, env=env, capture_output=True, stdin=subprocess.DEVNULL, check=False, timeout=timeout)
        if run.returncode != 0 or not report.is_file():
            # The scanner's own output can quote matched text; report only the exit status.
            return _result("failed", f"Betterleaks exited with status {run.returncode}.", commits_scanned=commits,
                           shallow=bool(shallow))
        raw = json.loads(report.read_text(encoding="utf-8", errors="replace") or "[]")
    if not isinstance(raw, list):
        return _result("failed", "Betterleaks returned an unexpected report.", commits_scanned=commits)
    findings = normalize(raw)
    return _result("complete", None, findings=findings[:MAX_FINDINGS], commits_scanned=commits,
                   shallow=bool(shallow), findings_truncated=max(0, len(findings) - MAX_FINDINGS))


def _clean(value: Any, limit: int) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()[:limit]


def _safe_path(value: Any) -> str | None:
    path = str(value or "").strip()
    if not path or len(path) > 300 or path.startswith("/") or "\\" in path or "\x00" in path:
        return None
    if any(part in {"", ".", ".."} for part in PurePosixPath(path).parts) or path != str(PurePosixPath(path)):
        return None
    return path


def normalize(rows: list[Any]) -> list[dict[str, Any]]:
    """Keep only rule, description, file, line, commit and date; drop secrets and authors.

    A secret carried by several commits (branches, merges, unchanged lines) is one finding at its
    newest commit; ``commits`` counts how many commits reported it."""
    found: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        rule, path, commit = str(row.get("RuleID") or ""), _safe_path(row.get("File")), str(row.get("Commit") or "")
        line = row.get("StartLine")
        if not _RULE.fullmatch(rule) or path is None or not _SHA.fullmatch(commit) or type(line) is not int:
            continue
        date = str(row.get("Date") or "")[:10]
        date = date if _DATE.fullmatch(date) else None
        key = (rule, path, max(1, line))
        seen = found.get(key)
        if seen is None:
            found[key] = {"rule": rule, "description": _clean(row.get("Description"), 120) or rule,
                          "file": path, "line": max(1, line), "commit": commit, "date": date, "_commits": {commit}}
            continue
        seen["_commits"].add(commit)
        if (date or "") > (seen["date"] or ""):
            seen.update(commit=commit, date=date)
    for row in found.values():
        row["commits"] = len(row.pop("_commits"))
    newest = sorted(found.values(), key=lambda f: (f["date"] or "", f["file"], f["line"]), reverse=True)
    # Known credential formats first, so the finding cap never hides them behind keyword matches.
    return sorted(newest, key=lambda f: severity(f["rule"]) != "error")


def validate_result(value: Any) -> dict[str, Any]:
    """A worker's history-scan result, re-normalized so nothing beyond the kept fields passes."""
    if not isinstance(value, dict) or value.get("status") not in _STATUSES:
        raise ValueError("The history scan result is invalid.")
    findings = []
    for row in (value.get("findings") or [])[:MAX_FINDINGS]:
        if not isinstance(row, dict):
            continue
        rule, path, commit, line = str(row.get("rule") or ""), _safe_path(row.get("file")), str(row.get("commit") or ""), row.get("line")
        if not _RULE.fullmatch(rule) or path is None or not _SHA.fullmatch(commit) or type(line) is not int or line < 1:
            continue
        date = str(row.get("date") or "")
        commits = row.get("commits")
        findings.append({"rule": rule, "description": _clean(row.get("description"), 120) or rule, "file": path,
                         "line": line, "commit": commit, "date": date if _DATE.fullmatch(date) else None,
                         "commits": commits if type(commits) is int and commits >= 1 else 1})
    commits = value.get("commits_scanned")
    return _result(value["status"], _clean(value.get("reason"), 300) or None, findings=findings,
                   version=_clean(value.get("version"), 40) or "unknown",
                   commits_scanned=commits if type(commits) is int and commits >= 0 else 0,
                   shallow=value.get("shallow") is True,
                   findings_truncated=value.get("findings_truncated") if type(value.get("findings_truncated")) is int else 0)


# --- Assessment ---------------------------------------------------------------------------------

def severity(rule: str) -> str:
    """Rules for a known credential format are errors; ``generic-*`` rules match by keyword and
    entropy only (translation labels, test fixtures), so they are warnings to review."""
    return "warning" if rule.startswith(GENERIC_RULE_PREFIX) else "error"


def findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Assessment findings: one per secret and location, at the newest commit that carries it."""
    rows = []
    for row in result.get("findings", []):
        when = f" on {row['date']}" if row.get("date") else ""
        also = row.get("commits", 1) - 1
        more = f" (and {also} other commit{'s' if also > 1 else ''})" if also > 0 else ""
        level = severity(row["rule"])
        description = row["description"].rstrip(" .")
        lead = description if level == "error" else f"Possible secret: {description}"
        # The location and the advice always fit; a long scanner description is shortened instead.
        tail = (f" Committed in {row['commit'][:12]}{when}{more}; it stays in Git history even after the "
                "file changes: rotate it, then remove it from history.")
        room = MAX_MESSAGE - len(tail)
        lead = lead + "." if len(lead) < room else lead[:room - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
        rows.append({"file": row["file"], "line": row["line"], "rule": f"{RULE_PREFIX}{row['rule']}"[:100],
                     "category": "security", "severity": level, "message": lead + tail})
    return rows


def summary(result: dict[str, Any]) -> dict[str, Any]:
    by_rule: dict[str, int] = {}
    for row in result.get("findings", []):
        by_rule[row["rule"]] = by_rule.get(row["rule"], 0) + 1
    return {"name": NAME, "version": result.get("version", VERSION), "check_version": CHECK_VERSION,
            "status": result["status"], "commits_scanned": result.get("commits_scanned", 0),
            "history_depth": HISTORY_DEPTH, "shallow": result.get("shallow", False),
            "finding_count": len(result.get("findings", [])),
            "error_count": sum(severity(row["rule"]) == "error" for row in result.get("findings", [])),
            "by_rule": dict(sorted(by_rule.items())),
            "findings_truncated": result.get("findings_truncated", 0), "reason": result.get("reason"),
            "categories": ["security"]}
