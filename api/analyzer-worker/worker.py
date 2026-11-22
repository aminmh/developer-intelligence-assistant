"""Isolated HTTPS worker for fixed SonarScanner, Joern, SCIP, LSP indexing, Psalm taint and history secret-scan jobs."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID
import zipfile

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from code_quality.external_analyzers import check_sonarqube_access, run_joern, run_sonarqube
from code_quality.scip_index import run_scip
from code_quality.taint_analysis import run_taint_analysis, validate_stubs
from code_quality import history_secrets
from lsp.jobs import run_lsp
from code_quality.service import MAX_ARCHIVE_BYTES, MAX_FILE_BYTES, MAX_FILES, MAX_SCAN_BYTES, _safe_archive_path

logger = logging.getLogger("analyzer_worker")
MAX_REQUEST_BYTES = 90 * 1024 * 1024
MAX_ARCHIVE_B64_BYTES = (MAX_ARCHIVE_BYTES * 4 // 3) + 8
MAX_JOB_FILES = min(MAX_FILES, 20_000)
_SHA = re.compile(r"^[a-fA-F0-9]{40,64}$")
_REVISION_ID = re.compile(r"^rev_[0-9a-f]{32}$")
_ANALYZERS = {"sonarqube", "joern", "scip", "lsp", "psalm"}
_ALLOWED_SONAR_HOSTS = {
    host.strip().lower().rstrip(".")
    for host in os.environ.get("ANALYZER_ALLOWED_SONAR_HOSTS", "sonarqube").split(",")
    if host.strip()
}
_job_slots = asyncio.Semaphore(1)

app = FastAPI(title="Analyzer Worker", docs_url=None, redoc_url=None, openapi_url=None)


def _validate_sonar_target(settings: dict[str, Any]) -> None:
    sonar = settings.get("sonarqube", {})
    if not sonar.get("enabled"):
        return
    endpoint = sonar.get("server_url")
    parsed = urlsplit(endpoint if isinstance(endpoint, str) else "")
    host = (parsed.hostname or "").lower().rstrip(".")
    if os.environ.get("APP_DEPLOYMENT_MODE", "developer").strip().lower() == "production" and host != "sonarqube":
        raise HTTPException(422, "Production Mode permits SonarQube only on the local service.")
    if host not in _ALLOWED_SONAR_HOSTS:
        raise HTTPException(422, "The SonarQube host is not allowed by this worker deployment.")


async def _read_bounded_json(request: Request, max_bytes: int = MAX_REQUEST_BYTES) -> dict[str, Any]:
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > max_bytes):
        raise HTTPException(413, "Analyzer job exceeds the request size limit.")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(413, "Analyzer job exceeds the request size limit.")
        chunks.append(chunk)
    try:
        value = json.loads(b"".join(chunks))
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(422, "Analyzer job must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise HTTPException(422, "Analyzer job must be an object.")
    return value


def _validate_job(payload: dict[str, Any]) -> tuple[str, bytes, list[str], dict[str, int], list[str], dict[str, Any], str | None]:
    allowed_fields = {"schema_version", "job_id", "revision_id", "commit_sha", "archive_base64",
                      "bundle_sha256", "files", "file_line_counts", "languages", "analyzers",
                      "settings", "sonarqube_token", "taint_stubs"}
    if set(payload) - allowed_fields:
        raise HTTPException(422, "Analyzer job contains unsupported fields.")
    if payload.get("schema_version") != 1:
        raise HTTPException(422, "Unsupported analyzer job schema.")
    try:
        job_id = str(UUID(payload.get("job_id", "")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, "Analyzer job identity is invalid.") from exc
    revision_id = payload.get("revision_id")
    if not isinstance(revision_id, str) or not _REVISION_ID.fullmatch(revision_id):
        raise HTTPException(422, "Analyzer job revision identity is invalid.")
    commit_sha = payload.get("commit_sha")
    if commit_sha is not None and (not isinstance(commit_sha, str) or not _SHA.fullmatch(commit_sha)):
        raise HTTPException(422, "Analyzer job revision is invalid.")
    encoded = payload.get("archive_base64")
    if not isinstance(encoded, str) or len(encoded) > MAX_ARCHIVE_B64_BYTES:
        raise HTTPException(413, "Analyzer source archive exceeds the transfer limit.")
    try:
        archive_data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(422, "Analyzer source archive is malformed.") from exc
    if len(archive_data) > MAX_ARCHIVE_BYTES:
        raise HTTPException(413, "Analyzer source archive exceeds the transfer limit.")
    bundle_sha256 = payload.get("bundle_sha256")
    if not isinstance(bundle_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", bundle_sha256):
        raise HTTPException(422, "Analyzer source bundle identity is invalid.")
    if hashlib.sha256(archive_data).hexdigest() != bundle_sha256:
        raise HTTPException(422, "Analyzer source bundle digest does not match its contents.")
    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or len(raw_files) > MAX_JOB_FILES:
        raise HTTPException(422, "Analyzer source inventory is invalid.")
    files: list[str] = []
    seen: set[str] = set()
    for item in raw_files:
        if not isinstance(item, str) or len(item) > 1024:
            raise HTTPException(422, "Analyzer source path is invalid.")
        try:
            safe = _safe_archive_path(item)
        except (ValueError, TypeError):
            raise HTTPException(422, "Analyzer source path is invalid.") from None
        if not safe or safe != item or safe in seen:
            raise HTTPException(422, "Analyzer source inventory contains an invalid or duplicate path.")
        seen.add(safe)
        files.append(safe)
    raw_languages = payload.get("languages")
    if (not isinstance(raw_languages, dict) or len(raw_languages) > 128
            or any(not isinstance(key, str) or len(key) > 40 or type(count) is not int or count < 0
                   for key, count in raw_languages.items())):
        raise HTTPException(422, "Analyzer language inventory is invalid.")
    requested = payload.get("analyzers")
    if not isinstance(requested, list) or not requested or len(requested) > len(_ANALYZERS):
        raise HTTPException(422, "Analyzer job selection is invalid.")
    if any(name not in _ANALYZERS for name in requested) or len(set(requested)) != len(requested):
        raise HTTPException(422, "Analyzer job selection contains an unsupported analyzer.")
    settings = payload.get("settings")
    if not isinstance(settings, dict) or not isinstance(settings.get("sonarqube", {}), dict) or not isinstance(settings.get("joern", {}), dict):
        raise HTTPException(422, "Analyzer settings are invalid.")
    if (set(settings) != {"sonarqube", "joern"}
            or set(settings["sonarqube"]) - {"enabled", "server_url", "project_key", "source_upload_consent"}
            or set(settings["joern"]) - {"enabled"}):
        raise HTTPException(422, "Analyzer settings contain unsupported fields.")
    if ("sonarqube" in requested and
            (not settings.get("sonarqube", {}).get("enabled")
             or not settings.get("sonarqube", {}).get("source_upload_consent"))):
        raise HTTPException(422, "SonarQube source-transfer consent is required.")
    if "joern" in requested and not settings.get("joern", {}).get("enabled"):
        raise HTTPException(422, "Joern is not enabled for this job.")
    token = payload.get("sonarqube_token")
    if token is not None and (not isinstance(token, str) or len(token) > 8192):
        raise HTTPException(422, "SonarQube credential is invalid.")
    if "sonarqube" in requested and not token:
        raise HTTPException(422, "SonarQube credential is missing.")
    if "sonarqube" not in requested and token is not None:
        raise HTTPException(422, "SonarQube credential was provided for an unselected analyzer.")
    if payload.get("taint_stubs") is not None:
        if "psalm" not in requested:
            raise HTTPException(422, "Taint stubs were provided for an unselected analyzer.")
        try:
            validate_stubs(payload["taint_stubs"])
        except ValueError as exc:
            raise HTTPException(422, "Taint stubs are invalid.") from exc
    line_counts = payload.get("file_line_counts")
    if (not isinstance(line_counts, dict) or set(line_counts) != set(files)
            or any(not isinstance(path, str) or type(count) is not int or count < 1
                   or count > MAX_FILE_BYTES + 1 for path, count in line_counts.items())):
        raise HTTPException(422, "Analyzer source line inventory is invalid.")
    selected_files = set(files)
    try:
        with zipfile.ZipFile(__import__("io").BytesIO(archive_data)) as archive:
            if len(archive.infolist()) > MAX_JOB_FILES * 5:
                raise HTTPException(413, "Analyzer source archive contains too many entries.")
            archived: set[str] = set()
            total_source_bytes = 0
            for member in archive.infolist():
                path = _safe_archive_path(member.filename)
                mode = (member.external_attr >> 16) & 0o170000
                if (member.is_dir() or path not in selected_files or path in archived
                        or mode == 0o120000 or member.file_size > MAX_FILE_BYTES):
                    raise HTTPException(422, "Analyzer bundle contains a file outside the selected inventory.")
                archived.add(path)
                total_source_bytes += member.file_size
                if total_source_bytes > MAX_SCAN_BYTES:
                    raise HTTPException(413, "Analyzer source bundle exceeds the scan size limit.")
            if archived != set(files):
                raise HTTPException(422, "Analyzer bundle does not match the selected source inventory.")
    except (zipfile.BadZipFile, OSError) as exc:
        raise HTTPException(422, "Analyzer source archive is invalid.") from exc
    _validate_sonar_target(settings)
    return job_id, archive_data, files, raw_languages, requested, settings, token


def _run_job(payload: dict[str, Any]) -> dict[str, Any]:
    job_id, archive, files, languages, requested, settings, token = _validate_job(payload)
    analyzers: dict[str, Any] = {}
    for name in requested:
        if name == "sonarqube":
            analyzers[name] = run_sonarqube(settings, archive, files, token)
        elif name == "joern":
            analyzers[name] = run_joern(settings, archive, files, languages)
        elif name == "scip":
            # Needs no settings: it reads only the bundled source and never builds the project.
            analyzers[name] = run_scip(archive, files, languages)
        elif name == "lsp":
            # Language servers index the bundled source with project-code features off (lsp/indexer.py).
            analyzers[name] = run_lsp(archive, files, languages)
        elif name == "psalm":
            # Framework taint stubs come from the API's detected plugins; Psalm parses them, never runs them.
            analyzers[name] = run_taint_analysis(archive, files, payload.get("taint_stubs"))
    return {"schema_version": 1, "job_id": job_id,
            "revision_id": payload["revision_id"],
            "commit_sha": payload.get("commit_sha"), "bundle_sha256": payload["bundle_sha256"],
            "analyzers": analyzers}


@app.get("/v1/capabilities")
async def capabilities() -> dict[str, Any]:
    scanner = shutil.which("sonar-scanner")
    joern = shutil.which("joern")
    parser = shutil.which("joern-parse")
    snapshot = {
        "sonarqube": {"enabled": False, "scanner_available": bool(scanner),
                      "status": "ready" if scanner else "unavailable",
                      "reason": None if scanner else "The pinned SonarScanner CLI is not installed."},
        "joern": {"enabled": False, "analyzer_available": bool(joern and parser),
                  "status": "ready" if joern and parser else "unavailable",
                  "reason": None if joern and parser else "Pinned Joern commands are not installed."},
    }
    # Both CLIs print logs or a banner around their version; keep only the version number.
    patterns = {"sonarqube": r"SonarScanner CLI ([0-9][0-9.]*)", "joern": r"Version:\s*([0-9][0-9A-Za-z.+-]*)"}
    for name, executable in (("sonarqube", scanner), ("joern", joern)):
        if executable:
            try:
                version = subprocess.run([executable, "--version"], capture_output=True, stdin=subprocess.DEVNULL,
                                          check=False, timeout=30,
                                          env={"PATH": os.environ.get("PATH", ""), "HOME": "/tmp"})
                output = (version.stdout + version.stderr).decode("utf-8", "replace")
                found = re.search(patterns[name], output)
                snapshot[name]["version"] = found.group(1)[:80] if found else "unknown"
            except (OSError, subprocess.SubprocessError):
                snapshot[name]["version"] = "unknown"
    from code_quality.scip_index import INDEXERS, version as tool_version
    scip_tools = {"scip": tool_version("scip"), **{binary: tool_version(binary) for binary, _ in INDEXERS.values()}}
    missing = sorted(name for name, found in scip_tools.items() if not found)
    snapshot["scip"] = {"enabled": True, "analyzer_available": not missing,
                        "status": "ready" if not missing else "unavailable",
                        "reason": None if not missing else "Not installed: " + ", ".join(missing),
                        "languages": ["typescript", "javascript", "python"],
                        "versions": {name: found for name, found in scip_tools.items() if found}}
    snapshot["lsp"] = _lsp_capability()
    from code_quality.taint_analysis import VERSION as psalm_version, available as psalm_available
    ready = psalm_available()
    snapshot["psalm"] = {"enabled": True, "analyzer_available": ready, "status": "ready" if ready else "unavailable",
                         "reason": None if ready else "The pinned Psalm is not installed.",
                         "languages": ["php"], "version": psalm_version}
    history_ready = history_secrets.available()
    snapshot["betterleaks"] = {"enabled": True, "analyzer_available": history_ready,
                               "status": "ready" if history_ready else "unavailable",
                               "reason": None if history_ready else "The pinned Betterleaks scanner or Git is not installed.",
                               "version": history_secrets.VERSION, "endpoint": "/v1/history-scan"}
    return {"schema_version": 1, "analyzers": snapshot}


def _lsp_capability() -> dict[str, Any]:
    from lsp import registry
    from lsp.jobs import language_extensions

    languages = sorted(language_extensions(registry))
    missing = [lang for lang in languages if not registry.server_installed(lang)]
    ready = bool(languages) and not missing
    return {"enabled": True, "analyzer_available": ready, "status": "ready" if ready else "unavailable",
            "reason": None if ready else ("Language server not installed for: " + ", ".join(missing) if missing
                                          else "No language has LSP indexing enabled."),
            "languages": languages}


@app.post("/v1/sonarqube/check")
async def check_sonarqube_connection(request: Request) -> dict[str, Any]:
    payload = await _read_bounded_json(request, max_bytes=16_000)
    if set(payload) != {"schema_version", "server_url", "project_key", "token"} or payload.get("schema_version") != 1:
        raise HTTPException(422, "SonarQube access-check request is invalid.")
    server_url, project_key, token = payload.get("server_url"), payload.get("project_key"), payload.get("token")
    if (not isinstance(server_url, str) or len(server_url) > 2048
            or not isinstance(project_key, str) or len(project_key) > 200
            or not isinstance(token, str) or not token or len(token) > 8192):
        raise HTTPException(422, "SonarQube access-check settings are invalid.")
    _validate_sonar_target({"sonarqube": {"enabled": True, "server_url": server_url}})
    try:
        return await run_in_threadpool(check_sonarqube_access, server_url, project_key, token)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logger.warning("sonarqube_access_check_failed error_type=%s", type(exc).__name__)
        raise HTTPException(503, "SonarQube access could not be verified; check the token and project permissions.") from exc


@app.post("/v1/jobs")
async def submit_job(request: Request) -> dict[str, Any]:
    payload = await _read_bounded_json(request)
    async with _job_slots:
        try:
            result = await run_in_threadpool(_run_job, payload)
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("analyzer_job_failed error_type=%s", type(exc).__name__)
            raise HTTPException(500, "The analyzer job failed; retry the assessment.") from exc
    logger.info("analyzer_job_finished job_id=%s", result["job_id"])
    return result


def _validate_history_job(payload: dict[str, Any]) -> tuple[str, str, str, bytes]:
    if set(payload) != {"schema_version", "job_id", "revision_id", "commit_sha", "history_base64", "history_sha256"}:
        raise HTTPException(422, "History scan job contains unsupported or missing fields.")
    if payload.get("schema_version") != 1:
        raise HTTPException(422, "Unsupported history scan job schema.")
    try:
        job_id = str(UUID(payload.get("job_id", "")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, "History scan job identity is invalid.") from exc
    revision_id, commit_sha = payload.get("revision_id"), payload.get("commit_sha")
    if not isinstance(revision_id, str) or not _REVISION_ID.fullmatch(revision_id):
        raise HTTPException(422, "History scan revision identity is invalid.")
    if not isinstance(commit_sha, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit_sha):
        raise HTTPException(422, "History scan commit is invalid.")
    encoded, digest = payload.get("history_base64"), payload.get("history_sha256")
    if not isinstance(encoded, str) or len(encoded) > (history_secrets.MAX_HISTORY_BYTES + 1024 * 1024) * 4 // 3 + 8:
        raise HTTPException(413, "History artifact exceeds the transfer limit.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(422, "History artifact is malformed.") from exc
    if not isinstance(digest, str) or hashlib.sha256(data).hexdigest() != digest:
        raise HTTPException(422, "History artifact digest does not match its contents.")
    try:
        history_secrets.unpack_history(data, commit_sha)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return job_id, revision_id, commit_sha, data


@app.post("/v1/history-scan")
async def submit_history_scan(request: Request) -> dict[str, Any]:
    """Scan a Git-synchronized revision's recent history for committed secrets (Betterleaks, no network)."""
    payload = await _read_bounded_json(request)
    job_id, revision_id, commit_sha, data = _validate_history_job(payload)
    async with _job_slots:
        result = await run_in_threadpool(history_secrets.run_history_scan, data, commit_sha)
    logger.info("history_scan_finished job_id=%s status=%s", job_id, result["status"])
    return {"schema_version": 1, "job_id": job_id, "revision_id": revision_id, "commit_sha": commit_sha,
            "history_sha256": payload["history_sha256"], "result": result}


def serve() -> None:
    import uvicorn

    cert = os.environ.get("ANALYZER_WORKER_TLS_CERT_FILE", "/run/analyzer-tls/worker.crt")
    key = os.environ.get("ANALYZER_WORKER_TLS_KEY_FILE", "/run/analyzer-tls/worker.key")
    api_ca = os.environ.get("ANALYZER_WORKER_API_CA_FILE", "/run/analyzer-tls/api-ca.crt")
    if not all(os.path.isfile(path) for path in (cert, key, api_ca)):
        raise RuntimeError("Analyzer worker TLS certificate, key, and API CA must be mounted.")
    config = uvicorn.Config(app, host="0.0.0.0", port=9443, log_level="info",
                            ssl_certfile=cert, ssl_keyfile=key, ssl_ca_certs=api_ca,
                            ssl_cert_reqs=ssl.CERT_REQUIRED, limit_concurrency=4,
                            timeout_keep_alive=5)
    uvicorn.Server(config).run()


if __name__ == "__main__":
    serve()
