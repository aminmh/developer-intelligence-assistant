"""Project-scoped configuration and capability reporting for optional analyzers."""
from __future__ import annotations

from datetime import datetime, timezone
import base64
import json
import os
from pathlib import Path
import re
try:
    import resource
except ImportError:  # pragma: no cover - Windows API runtimes do not expose resource limits.
    resource = None
import shutil
import sqlite3
import subprocess
import tempfile
import time
from threading import Thread
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile
from uuid import UUID

from code_quality.service import _store_path

_SONAR_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_CERT_SHA256 = re.compile(r"^[A-Fa-f0-9]{64}$")


def deployment_mode() -> str:
    """Return the explicit application deployment mode, failing closed on unknown values."""
    value = os.environ.get("APP_DEPLOYMENT_MODE", "developer").strip().lower()
    return value if value in {"developer", "production"} else "invalid"


class DeveloperModeStore:
    """Persist the global administrator-controlled Developer Mode feature gate."""

    def __init__(self, path=None):
        self.path = path or _store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS code_quality_runtime_settings (
                setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL, updated_at TEXT NOT NULL)""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def enabled(self) -> bool:
        if deployment_mode() != "developer":
            return False
        with self._connect() as db:
            row = db.execute("SELECT setting_value FROM code_quality_runtime_settings WHERE setting_key=?",
                             ("developer_mode_enabled",)).fetchone()
        return row["setting_value"] == "true" if row else True

    def set_enabled(self, enabled: Any) -> bool:
        if deployment_mode() != "developer":
            raise ExternalAnalyzerError("Developer Mode remote analyzers cannot be changed in Production Mode.")
        if type(enabled) is not bool:
            raise ExternalAnalyzerError("Developer Mode must be enabled or disabled with a boolean value.")
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("""INSERT INTO code_quality_runtime_settings(setting_key,setting_value,updated_at)
                VALUES(?,?,?) ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,
                updated_at=excluded.updated_at""",
                       ("developer_mode_enabled", "true" if enabled else "false", updated_at))
        return enabled


def developer_mode_enabled() -> bool:
    """Return whether Developer Mode remote-analyzer features may contact workers."""
    return DeveloperModeStore().enabled()


def effective_worker_target(settings: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve project-configured Developer Mode targets or the deployment-owned Production target."""
    mode = deployment_mode()
    if mode == "production":
        endpoint = os.environ.get("ANALYZER_WORKER_URL", "").strip()
        pin = os.environ.get("ANALYZER_WORKER_CERT_SHA256", "").strip().lower()
        if endpoint and _CERT_SHA256.fullmatch(pin):
            return {"enabled": True, "endpoint": endpoint, "certificate_sha256": pin,
                    "source_transfer_consent": True, "deployment_owned": True}
        return None
    if mode != "developer" or not developer_mode_enabled():
        return None
    worker = settings.get("worker", {})
    if worker.get("enabled") and worker.get("source_transfer_consent"):
        return {"enabled": True, "endpoint": worker.get("endpoint"),
                "certificate_sha256": worker.get("certificate_sha256"),
                "source_transfer_consent": True, "deployment_owned": False}
    return None


class ExternalAnalyzerError(ValueError):
    """Safe configuration error for optional external analyzers."""


def _limit_analyzer_process() -> None:
    if resource is None:
        return
    resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (150, 150))


def _normalize_sonar_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 2048:
        raise ExternalAnalyzerError("Enter a valid SonarQube server URL.")
    parsed = urlsplit(value.strip().rstrip("/"))
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ExternalAnalyzerError("Use a SonarQube URL without credentials, query parameters, or fragments.")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1", "sonarqube"}:
        raise ExternalAnalyzerError("Remote SonarQube servers must use HTTPS.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def normalize_settings(value: Any, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExternalAnalyzerError("External analyzer settings must be an object.")
    if deployment_mode() == "invalid":
        raise ExternalAnalyzerError("APP_DEPLOYMENT_MODE must be developer or production.")
    previous = existing or {}
    sonar = value.get("sonarqube", {})
    joern = value.get("joern", {})
    worker = value.get("worker", {})
    if not isinstance(sonar, dict) or not isinstance(joern, dict) or not isinstance(worker, dict):
        raise ExternalAnalyzerError("SonarQube, Joern, and worker settings must be objects.")
    sonar_enabled = sonar.get("enabled", previous.get("sonarqube", {}).get("enabled", False))
    joern_enabled = joern.get("enabled", previous.get("joern", {}).get("enabled", False))
    consent = sonar.get("source_upload_consent", previous.get("sonarqube", {}).get("source_upload_consent", False))
    worker_enabled = worker.get("enabled", previous.get("worker", {}).get("enabled", False))
    worker_consent = worker.get("source_transfer_consent", previous.get("worker", {}).get("source_transfer_consent", False))
    if any(type(flag) is not bool for flag in (sonar_enabled, joern_enabled, consent, worker_enabled, worker_consent)):
        raise ExternalAnalyzerError("Analyzer enablement and source upload consent must be true or false.")
    url_value = sonar.get("server_url", previous.get("sonarqube", {}).get("server_url", ""))
    project_key = sonar.get("project_key", previous.get("sonarqube", {}).get("project_key", ""))
    if not isinstance(url_value, str) or not isinstance(project_key, str):
        raise ExternalAnalyzerError("SonarQube server URL and project key must be text values.")
    server_url = _normalize_sonar_url(url_value) if url_value else ""
    if deployment_mode() == "production" and sonar_enabled:
        if (urlsplit(server_url).hostname or "").lower() != "sonarqube":
            raise ExternalAnalyzerError("Production Mode permits SonarQube only on the local sonarqube service.")
    worker_endpoint = worker.get("endpoint", previous.get("worker", {}).get("endpoint", ""))
    certificate_sha256 = worker.get("certificate_sha256", previous.get("worker", {}).get("certificate_sha256", ""))
    if not isinstance(worker_endpoint, str) or not isinstance(certificate_sha256, str):
        raise ExternalAnalyzerError("Worker endpoint and certificate fingerprint must be text values.")
    worker_endpoint = worker_endpoint.strip()
    certificate_sha256 = certificate_sha256.strip().lower()
    if deployment_mode() == "production" and (worker_enabled or worker_endpoint or certificate_sha256):
        raise ExternalAnalyzerError("Production Mode uses its deployment-installed local worker; remote worker settings cannot be saved.")
    if worker_endpoint:
        parsed_worker = urlsplit(worker_endpoint.rstrip("/"))
        if (parsed_worker.scheme != "https" or not parsed_worker.hostname or parsed_worker.username
                or parsed_worker.password or parsed_worker.query or parsed_worker.fragment
                or parsed_worker.path not in {"", "/"}):
            raise ExternalAnalyzerError("Use an HTTPS worker origin without credentials, a path, query, or fragment.")
        worker_endpoint = urlunsplit(("https", parsed_worker.netloc, "", "", ""))
    if certificate_sha256 and not _CERT_SHA256.fullmatch(certificate_sha256):
        raise ExternalAnalyzerError("Enter the worker TLS certificate's 64-character SHA-256 fingerprint.")
    if worker_enabled and not (worker_endpoint and certificate_sha256 and worker_consent):
        raise ExternalAnalyzerError("Configure and verify the worker certificate, then consent to source transfer before enabling the worker.")
    if project_key and not _SONAR_KEY.fullmatch(project_key):
        raise ExternalAnalyzerError("Use a SonarQube project key containing letters, numbers, period, underscore, colon, or hyphen.")
    credential_id = sonar.get("credential_id", previous.get("sonarqube", {}).get("credential_id"))
    if credential_id:
        try:
            credential_id = str(UUID(credential_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ExternalAnalyzerError("SonarQube credential identity is invalid.") from exc
    if sonar_enabled and not (server_url and project_key and consent):
        raise ExternalAnalyzerError("Enable SonarQube only after configuring its URL, project key, and explicit source upload consent.")
    now = datetime.now(timezone.utc).isoformat()
    return {
        "sonarqube": {"enabled": sonar_enabled, "server_url": server_url,
                      "project_key": project_key, "source_upload_consent": consent,
                      "credential_id": credential_id},
        "joern": {"enabled": joern_enabled},
        "worker": {"enabled": worker_enabled, "endpoint": worker_endpoint,
                   "certificate_sha256": certificate_sha256,
                   "source_transfer_consent": worker_consent},
        "updated_at": now,
    }


class ExternalAnalyzerSettingsStore:
    """Persist non-secret integration configuration per authorized project."""

    def __init__(self, path=None):
        self.path = path or _store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS code_quality_external_analyzers (
                project_id TEXT PRIMARY KEY, settings_json TEXT NOT NULL, updated_at TEXT NOT NULL)""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def get(self, project_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT settings_json FROM code_quality_external_analyzers WHERE project_id=?",
                             (project_id,)).fetchone()
        if not row:
            return normalize_settings({})
        import json
        saved = json.loads(row["settings_json"])
        if deployment_mode() == "production":
            saved["worker"] = {"enabled": False, "endpoint": "", "certificate_sha256": "",
                               "source_transfer_consent": False}
        return normalize_settings(saved)

    def save(self, project_id: str, value: Any) -> dict[str, Any]:
        import json
        current = self.get(project_id)
        settings = normalize_settings(value, current)
        with self._connect() as db:
            db.execute("""INSERT INTO code_quality_external_analyzers(project_id,settings_json,updated_at)
                VALUES(?,?,?) ON CONFLICT(project_id) DO UPDATE SET settings_json=excluded.settings_json,
                updated_at=excluded.updated_at""",
                       (project_id, json.dumps(settings, separators=(",", ":")), settings["updated_at"]))
        return settings


def capability_snapshot(settings: dict[str, Any]) -> dict[str, Any]:
    """Report execution-worker availability without claiming analyzer coverage."""
    target = effective_worker_target(settings)
    if target is None:
        if deployment_mode() == "production":
            reason = "Production Mode requires the local analyzer worker deployment and TLS identity."
        elif deployment_mode() == "developer" and not developer_mode_enabled():
            reason = "Developer Mode remote analyzers are disabled by the global administrator."
        else:
            reason = "Configure a trusted remote worker and consent to source transfer."
        return {"deployment_mode": deployment_mode(),
                "worker": {"enabled": False, "status": "unavailable", "reason": reason},
                "sonarqube": {"enabled": bool(settings.get("sonarqube", {}).get("enabled")),
                              "status": "unavailable", "reason": reason},
                "joern": {"enabled": bool(settings.get("joern", {}).get("enabled")),
                          "status": "unavailable", "reason": reason}}
    try:
        from code_quality.remote_worker import get_worker_capabilities
        capabilities = get_worker_capabilities(target)
        worker_status = "ready"
        worker_reason = None
    except Exception as exc:
        capabilities = {}
        worker_status = "unavailable"
        worker_reason = str(exc)[:240]
    return {
        "deployment_mode": deployment_mode(),
        "worker": {"enabled": bool(target), "status": worker_status, "reason": worker_reason,
                   "deployment_owned": target["deployment_owned"]},
        "sonarqube": capabilities.get("sonarqube", {"enabled": False, "status": worker_status,
                                                       "reason": worker_reason}),
        "joern": capabilities.get("joern", {"enabled": False, "status": worker_status,
                                               "reason": worker_reason}),
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _sonar_json(server_url: str, token: str, path: str, query: dict[str, Any]) -> dict[str, Any]:
    url = server_url + "/api/" + path + "?" + urlencode(query)
    request = Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    try:
        with build_opener(_NoRedirect).open(request, timeout=10) as response:
            raw = response.read(2_000_001)
    except HTTPError as exc:
        if exc.code in {301, 302, 303, 307, 308}:
            raise ExternalAnalyzerError("SonarQube redirected an API request; verify the configured server URL.") from exc
        if exc.code == 401:
            raise ExternalAnalyzerError("SonarQube rejected the saved token. Check that it is valid and not expired.") from exc
        if exc.code == 403:
            raise ExternalAnalyzerError(
                "SonarQube denied access to project results. Use a User token whose owner has Browse permission for this project. Execute Analysis is required separately to submit scans."
            ) from exc
        raise ExternalAnalyzerError("SonarQube rejected the analysis credential or API request.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ExternalAnalyzerError("SonarQube could not be reached. Check the configured server and retry.") from exc
    if len(raw) > 2_000_000:
        raise ExternalAnalyzerError("SonarQube returned more data than the configured response limit.")
    try:
        result = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ExternalAnalyzerError("SonarQube returned an invalid API response.") from exc
    if not isinstance(result, dict):
        raise ExternalAnalyzerError("SonarQube returned an invalid API response.")
    return result


def _sonar_server_version(server_url: str, token: str) -> str:
    request = Request(server_url + "/api/server/version", headers={
        "Authorization": "Bearer " + token, "Accept": "text/plain",
    })
    try:
        with build_opener(_NoRedirect).open(request, timeout=10) as response:
            value = response.read(512).decode("utf-8", "replace").strip()
    except (HTTPError, URLError, TimeoutError, OSError):
        return "unknown"
    return value[:80] if value and len(value) < 512 else "unknown"


def check_sonarqube_access(server_url: str, project_key: str, token: str) -> dict[str, Any]:
    """Verify saved-token access to this project's existing SonarQube results, without source upload."""
    server = _normalize_sonar_url(server_url)
    if not isinstance(project_key, str) or not _SONAR_KEY.fullmatch(project_key):
        raise ExternalAnalyzerError("Use a valid SonarQube project key before testing access.")
    if not isinstance(token, str) or not token.strip() or len(token) > 8192:
        raise ExternalAnalyzerError("Save a valid SonarQube token before testing access.")

    # These are the same read APIs the assessment uses after a scan. A successful
    # check confirms the token can read results, but does not submit an analysis.
    analyses = _sonar_json(server, token, "project_analyses/search",
                           {"project": project_key, "ps": 1})
    if not isinstance(analyses.get("analyses"), list):
        raise ExternalAnalyzerError("SonarQube returned an invalid project analysis response.")
    issues = _sonar_json(server, token, "issues/search",
                         {"componentKeys": project_key, "p": 1, "ps": 1})
    if not isinstance(issues.get("issues"), list):
        raise ExternalAnalyzerError("SonarQube returned an invalid project issues response.")
    return {"schema_version": 1, "status": "ready", "project_key": project_key,
            "server_version": _sonar_server_version(server, token), "read_access": "verified"}


def _extract_assessment_files(archive_data: bytes, files: list[str], destination: str) -> int:
    from code_quality.service import MAX_FILE_BYTES, _safe_archive_path
    selected = set(files)
    extracted: set[str] = set()
    written = 0
    with zipfile.ZipFile(__import__("io").BytesIO(archive_data)) as archive:
        for member in sorted(archive.infolist(), key=lambda item: item.filename):
            relative = _safe_archive_path(member.filename)
            if relative not in selected or relative in extracted or member.is_dir() or member.file_size > MAX_FILE_BYTES:
                continue
            path = os.path.join(destination, *relative.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = archive.read(member)
            if len(data) != member.file_size:
                continue
            with open(path, "xb") as output:
                output.write(data)
            extracted.add(relative)
            written += 1
    return written


def _run_with_bounded_output(command: list[str], *, cwd: str, env: dict[str, str],
                             timeout: int, preexec_fn: Any) -> tuple[int, bytes]:
    """Drain child output without retaining more than the final 16 KiB."""
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, preexec_fn=preexec_fn)
    output = bytearray()

    def drain() -> None:
        if process.stdout is None:
            return
        try:
            while chunk := process.stdout.read(4096):
                output.extend(chunk)
                if len(output) > 16_384:
                    del output[:-16_384]
        except OSError:
            return

    reader = Thread(target=drain, daemon=True)
    reader.start()
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        reader.join()
        if process.stdout is not None:
            process.stdout.close()
    return return_code, bytes(output)


def _scanner_failure_reason(return_code: int, output: bytes, token: str) -> str:
    text = output.decode("utf-8", "replace")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    if token:
        text = text.replace(token, "[redacted]")
    text = re.sub(r"(?i)(authorization|bearer|token|password)(\s*[:=]?\s+)[^\s,;]+",
                  r"\1\2[redacted]", text)
    diagnostic_lines = [line.strip() for line in text.splitlines()
                        if re.search(r"(?i)(error|exception|failed|denied|unsupported)", line)]
    diagnostic = diagnostic_lines[-1] if diagnostic_lines else ""
    diagnostic = re.sub(r"https?://[^\s]+", "configured SonarQube URL", diagnostic)
    diagnostic = re.sub(r"/tmp/[^\s:]+", "temporary scan workspace", diagnostic)
    diagnostic = " ".join(diagnostic.split())[:220]
    if diagnostic:
        return f"SonarScanner exited with code {return_code}: {diagnostic}"
    return f"SonarScanner exited with code {return_code} before submitting an analysis."


def _sonar_category(issue: dict[str, Any]) -> str | None:
    issue_type = str(issue.get("type") or "").upper()
    if issue_type in {"VULNERABILITY", "SECURITY_HOTSPOT"}:
        return "security"
    if issue_type == "BUG":
        return "reliability"
    if issue_type == "CODE_SMELL":
        tags = {str(tag).lower() for tag in issue.get("tags", []) if isinstance(tag, str)} if isinstance(issue.get("tags"), list) else set()
        return "performance" if "performance" in tags else "maintainability"
    return None


def run_sonarqube(settings: dict[str, Any], archive_data: bytes, files: list[str], token: str | None) -> dict[str, Any]:
    """Run the installed scanner on only the selected revision files and retrieve bounded issues."""
    config = settings.get("sonarqube", {})
    result = {"name": "sonarqube", "version": "unknown", "server_version": "unknown", "server_edition": "unknown",
             "status": "unavailable", "scanned_files": 0, "skipped_files": 0,
             "supported_languages": [], "unsupported_languages": [], "findings": [], "reason": None}
    if not config.get("enabled"):
        result["status"] = "disabled"
        result["reason"] = "SonarQube is disabled for this project."
        return result
    if not config.get("source_upload_consent"):
        result["reason"] = "Explicit consent is required before uploading source to SonarQube."
        return result
    executable = shutil.which("sonar-scanner")
    if not executable:
        result["reason"] = "The SonarScanner CLI is not installed in the API runtime."
        return result
    if not isinstance(token, str) or not token:
        result["reason"] = "Save a SonarQube token in the host OS credential store and retry."
        return result
    try:
        server = _normalize_sonar_url(config.get("server_url"))
        project_key = config.get("project_key")
        if not isinstance(project_key, str) or not _SONAR_KEY.fullmatch(project_key):
            raise ExternalAnalyzerError("The configured SonarQube project key is invalid.")
        result["server_version"] = _sonar_server_version(server, token)
        language_response = _sonar_json(server, token, "languages/list", {"ps": 500})
        installed_languages = {str(item.get("key")) for item in language_response.get("languages", [])
                               if isinstance(item, dict) and item.get("key")}
        from code_quality.service import LANGUAGE_BY_SUFFIX
        sonar_language_keys = {
            "python": "py", "javascript": "js", "typescript": "js", "php": "php",
            "java": "java", "kotlin": "kotlin", "go": "go", "ruby": "ruby",
            "c": "cpp", "cpp": "cpp", "csharp": "cs", "swift": "swift",
            "scala": "scala", "sql": "plsql", "html": "web", "css": "css",
        }
        file_language = {path: LANGUAGE_BY_SUFFIX.get(Path(path).suffix.lower()) for path in files}
        supported_files = [path for path in files
                           if sonar_language_keys.get(file_language.get(path) or "") in installed_languages]
        selected_languages = sorted({file_language[path] for path in supported_files if file_language.get(path)})
        all_languages = sorted({language for language in file_language.values() if language})
        result["supported_languages"] = selected_languages
        result["unsupported_languages"] = sorted(set(all_languages) - set(selected_languages))
        result["skipped_files"] = len(files) - len(supported_files)
        if not supported_files:
            result["status"] = "partial"
            result["reason"] = "The configured SonarQube server has no detected language analyzer for this revision."
            return result
        try:
            version_probe = subprocess.run([executable, "--version"], capture_output=True,
                                           timeout=8, check=False, env={"PATH": os.environ.get("PATH", "")})
            version_text = (version_probe.stdout + version_probe.stderr).decode("utf-8", "replace")[:8_000]
            match = re.search(r"SonarScanner CLI\s+([0-9][A-Za-z0-9._+-]*)", version_text)
            if match:
                result["version"] = match.group(1)[:80]
        except (OSError, subprocess.TimeoutExpired):
            pass
        with tempfile.TemporaryDirectory(prefix="code-quality-sonar-") as workdir:
            source_dir = os.path.join(workdir, "source")
            os.makedirs(source_dir)
            result["scanned_files"] = _extract_assessment_files(archive_data, supported_files, source_dir)
            if result["scanned_files"] == 0:
                result["status"] = "partial"
                result["reason"] = "No eligible source files were available to SonarScanner."
                return result
            env = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "HOME", "JAVA_HOME", "LANG", "LC_ALL", "TMPDIR"}}
            env.update({"SONAR_HOST_URL": server, "SONAR_TOKEN": token,
                        "SONAR_USER_HOME": os.path.join(workdir, "sonar-home"),
                        "SONAR_SCANNER_SKIP_JRE_PROVISIONING": "true",
                        "SONAR_SCANNER_JAVA_OPTS": "-Xms128m -Xmx2g -XX:MaxMetaspaceSize=768m"})
            command = [executable, "-Dsonar.projectKey=" + project_key,
                       "-Dsonar.projectBaseDir=" + source_dir,
                       "-Dsonar.sources=.", "-Dsonar.scm.disabled=true",
                       "-Dsonar.nodejs.executable=/usr/local/bin/node",
                       "-Dsonar.scanner.skipNodeProvisioning=true"]
            try:
                return_code, scanner_output = _run_with_bounded_output(
                    command, cwd=source_dir, env=env, timeout=180,
                    preexec_fn=_limit_analyzer_process if resource is not None else None)
            except subprocess.TimeoutExpired as exc:
                result["status"] = "partial"
                result["reason"] = "SonarScanner exceeded its 180-second execution limit."
                return result
            except OSError as exc:
                raise ExternalAnalyzerError("SonarScanner could not start in the API runtime.") from exc
            if return_code != 0:
                result["status"] = "failed"
                result["reason"] = _scanner_failure_reason(return_code, scanner_output, token)
                return result
            task_path = os.path.join(source_dir, ".scannerwork", "report-task.txt")
            if not os.path.isfile(task_path) or os.path.getsize(task_path) > 32_000:
                result["status"] = "failed"
                result["reason"] = "SonarScanner did not produce a bounded analysis task record."
                return result
            task = {}
            with open(task_path, encoding="utf-8", errors="replace") as task_file:
                for line in task_file:
                    if "=" in line:
                        key, value = line.rstrip("\r\n").split("=", 1)
                        task[key] = value
            if task.get("serverUrl", "").rstrip("/") != server or not task.get("ceTaskId"):
                result["status"] = "failed"
                result["reason"] = "SonarScanner returned a task for a different server or no task identity."
                return result
            deadline = time.monotonic() + 90
            task_data: dict[str, Any] = {}
            while time.monotonic() < deadline:
                task_data = _sonar_json(server, token, "ce/task", {"id": task["ceTaskId"]}).get("task", {})
                if task_data.get("status") in {"SUCCESS", "FAILED", "CANCELED"}:
                    break
                time.sleep(2)
            if task_data.get("status") != "SUCCESS" or task_data.get("componentKey") != project_key:
                result["status"] = "partial"
                result["reason"] = "SonarQube analysis was not successfully processed for the configured project."
                return result
            if result["version"] == "unknown":
                result["version"] = str(task.get("scannerVersion") or "unknown")[:80]
            analysis_id = task_data.get("analysisId")
            findings: list[dict[str, Any]] = []
            if not analysis_id:
                result["status"] = "partial"
                result["reason"] = "SonarQube completed without returning an analysis identity."
                return result
            latest = _sonar_json(server, token, "project_analyses/search", {"project": project_key, "ps": 1}).get("analyses", [])
            if not isinstance(latest, list) or not latest or latest[0].get("key") != analysis_id:
                result["status"] = "partial"
                result["reason"] = "A different SonarQube analysis replaced this revision before its findings could be read."
                return result
            for page in range(1, 6):
                payload = _sonar_json(server, token, "issues/search", {
                    "componentKeys": project_key, "p": page, "ps": 100,
                })
                issues = payload.get("issues")
                if not isinstance(issues, list):
                    raise ExternalAnalyzerError("SonarQube returned an invalid issue list.")
                for issue in issues:
                    if not isinstance(issue, dict):
                        continue
                    category = _sonar_category(issue)
                    component = str(issue.get("component") or "")
                    path = component.split(":", 1)[-1]
                    line = issue.get("line")
                    message = issue.get("message")
                    if (category and path in files and type(line) is int and line > 0
                            and isinstance(message, str) and message.strip()):
                        findings.append({"rule": "sonarqube:" + str(issue.get("rule") or "unknown")[:180],
                                         "category": category, "severity": str(issue.get("severity") or "INFO").lower(),
                                         "file": path, "line": line, "message": message[:600],
                                         "tool": "sonarqube", "version": result["version"]})
                if len(issues) < 100 or len(findings) >= 500:
                    break
            latest_after = _sonar_json(server, token, "project_analyses/search", {"project": project_key, "ps": 1}).get("analyses", [])
            if not isinstance(latest_after, list) or not latest_after or latest_after[0].get("key") != analysis_id:
                result["status"] = "partial"
                result["reason"] = "A concurrent SonarQube analysis changed the project while findings were being read."
                return result
            result["status"] = "complete"
            result["findings"] = findings[:500]
            result["reason"] = None
    except ExternalAnalyzerError as exc:
        result["status"] = "failed"
        result["reason"] = str(exc)[:300]
    except Exception as exc:
        result["status"] = "failed"
        result["reason"] = "SonarQube analysis failed (" + type(exc).__name__ + ")."
    return result


_JOERN_FRONTENDS = {
    "c": "C", "cpp": "C", "csharp": "CSHARPSRC", "go": "GOLANG",
    "java": "JAVASRC", "javascript": "JAVASCRIPT", "typescript": "JAVASCRIPT",
    "kotlin": "KOTLIN", "php": "PHP", "python": "PYTHONSRC", "ruby": "RUBYSRC",
}
_DYNAMIC_CALLS = {"eval", "exec", "system", "popen", "passthru", "shell_exec",
                  "runtime.exec", "processbuilder", "spawn", "child_process.exec"}


# PHP request-to-sink data flow (external-analyzer spec clause 31). Application-owned; project
# files never contribute Joern code. Sources: Laravel request accessors, request(), and $_GET-style
# superglobal reads. Sinks: raw SQL, shell execution, eval, unserialize, file paths, redirects.
_PHP_FLOW_SCRIPT = r'''@main def exec(cpgFile: String, outFile: String) = {
  importCpg(cpgFile)
  def enc(value: String): String = java.util.Base64.getEncoder.encodeToString(Option(value).getOrElse("").getBytes("UTF-8"))
  val requestNames = "(input|query|get|post|all|only|except|validated|safe|string|integer|boolean|date|cookie|header|file|json|route|collect)"
  val requestCalls = cpg.call.name(requestNames).filter { c =>
    c.methodFullName.startsWith("Illuminate\\Http\\Request") ||
      c.receiver.code.l.exists(r => r == "$request" || r.startsWith("request()") || r.startsWith("$this->request"))
  }.l
  val helperCalls = cpg.call.name("request").filter(_.argument.size > 0).l
  // $_GET["x"] is an index access on the identifier _GET; the access is the value that flows.
  val superglobals = cpg.call.name("<operator>.indexAccess")
    .filter(_.argument.isIdentifier.name("_(GET|POST|REQUEST|COOKIE|FILES|SERVER)").nonEmpty).l
  val sources: List[io.shiftleft.codepropertygraph.generated.nodes.CfgNode] = requestCalls ++ helperCalls ++ superglobals
  val sinkKinds: List[(String, String)] = List(
    "sql" -> "(?i)(raw|whereRaw|selectRaw|orderByRaw|havingRaw|groupByRaw|unprepared|statement|mysqli_query|pg_query)",
    "command" -> "(exec|system|shell_exec|passthru|proc_open|popen|pcntl_exec)",
    "code" -> "(eval|assert|create_function)",
    "deserialization" -> "unserialize",
    "file_path" -> "(file_get_contents|file_put_contents|fopen|readfile|unlink|include|include_once|require|require_once)",
    "redirect" -> "(header|away)")
  val lines = sinkKinds.flatMap { case (kind, pattern) =>
    cpg.call.name(pattern).argument.reachableByFlows(sources.iterator).take(200).map { path =>
      val first = path.elements.head
      val last = path.elements.last
      // Long paths keep their first steps and always the sink.
      val steps = if (path.elements.size > 16) path.elements.take(15) :+ last else path.elements
      val elements = steps.map { e =>
        enc(e.file.name.headOption.getOrElse("") + "\u001f" + e.lineNumber.getOrElse(0).toString + "\u001f" + e.code.take(160))
      }
      (List(enc(kind)) ++ elements).mkString("\t")
    }.l
  }.distinct.take(300)
  lines #> outFile
}
'''
_PHP_FLOW_SEVERITY = {"sql": "error", "command": "error", "code": "error", "deserialization": "error",
                      "file_path": "warning", "redirect": "warning"}
_PHP_FLOW_LABEL = {"sql": "raw SQL", "command": "shell command", "code": "code evaluation",
                   "deserialization": "unserialize", "file_path": "file path", "redirect": "redirect"}
MAX_PHP_FLOWS = 100


def _php_flow_findings(output_path: str, php_files: list[str], version: str) -> list[dict[str, Any]]:
    """Parse the flow query output into bounded findings, one per (kind, source, sink)."""
    def match(file_path: str) -> str | None:
        relative = file_path.replace("\\", "/").lstrip("./")
        return next((path for path in php_files if relative == path or relative.endswith("/" + path)), None)

    findings: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    with open(output_path, encoding="ascii", errors="ignore") as output:
        for line in output:
            try:
                fields = [base64.b64decode(value, validate=True).decode("utf-8", "replace")
                          for value in line.rstrip("\r\n").split("\t")]
            except (ValueError, UnicodeError):
                continue
            kind, raw_steps = fields[0], fields[1:]
            if kind not in _PHP_FLOW_SEVERITY or len(raw_steps) < 2:
                continue
            steps = []
            for raw in raw_steps[:16]:
                parts = raw.split("\x1f")
                path = match(parts[0]) if len(parts) == 3 else None
                if path is None or not parts[1].isdigit() or int(parts[1]) < 1:
                    continue
                steps.append({"file": path, "line": int(parts[1]), "code": parts[2][:160]})
            if len(steps) < 2:
                continue
            source, sink = steps[0], steps[-1]
            key = (kind, source["file"], source["line"], sink["file"], sink["line"])
            if key in seen:
                continue
            seen.add(key)
            findings.append({
                "rule": "joern:php-request-to-" + kind.replace("_", "-"), "category": "security",
                "severity": _PHP_FLOW_SEVERITY[kind], "file": sink["file"], "line": sink["line"],
                "message": (f"Request input from {source['file']}:{source['line']} reaches a "
                            f"{_PHP_FLOW_LABEL[kind]} sink without passing a recognized sanitizer."),
                "tool": "joern", "version": version, "trace": steps})
            if len(findings) >= MAX_PHP_FLOWS:
                break
    return findings


def _run_php_flows(joern: str, workdir: str, cpg_file: str, php_files: list[str], version: str,
                   deadline: float, findings: list[dict[str, Any]]) -> str | None:
    """Run the PHP request-to-sink query on a built CPG; append findings; return an error or None."""
    script_path = os.path.join(workdir, "php-flows.sc")
    output_path = os.path.join(workdir, "php-flows.tsv")
    with open(script_path, "w", encoding="utf-8") as script_file:
        script_file.write(_PHP_FLOW_SCRIPT)
    remaining = max(1, min(90, int(deadline - time.monotonic())))
    try:
        queried = subprocess.run([joern, "--script", script_path, "--param", "cpgFile=" + cpg_file,
                                  "--param", "outFile=" + output_path], cwd=workdir,
                                 env={"PATH": os.environ.get("PATH", ""), "HOME": workdir, "LANG": "C.UTF-8",
                                      "JAVA_TOOL_OPTIONS": "-Xms128m -Xmx2g -XX:MaxMetaspaceSize=768m"},
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=remaining,
                                 check=False, preexec_fn=_limit_analyzer_process if resource is not None else None)
    except subprocess.TimeoutExpired:
        return "php data-flow query exceeded the Joern execution limit"
    except (OSError, subprocess.SubprocessError):
        return "php data-flow query could not start"
    if queried.returncode != 0 or not os.path.isfile(output_path):
        return "php data-flow query failed"
    if os.path.getsize(output_path) > 4_000_000:
        return "php data-flow query output exceeded the limit"
    findings.extend(_php_flow_findings(output_path, php_files, version))
    return None


def run_joern(settings: dict[str, Any], archive_data: bytes, files: list[str], languages: dict[str, int]) -> dict[str, Any]:
    """Build bounded Joern CPGs and report dynamic-execution call signals."""
    result = {"name": "joern", "version": "unknown", "status": "unavailable",
              "languages": [], "scanned_files": 0, "skipped_files": 0,
              "findings": [], "reason": None, "unresolved": []}
    if not settings.get("joern", {}).get("enabled"):
        result["status"] = "disabled"
        result["reason"] = "Joern is disabled for this project."
        return result
    joern = shutil.which("joern")
    parser = shutil.which("joern-parse")
    if not joern or not parser:
        result["reason"] = "Joern and joern-parse are not both installed in the API runtime."
        return result
    from code_quality.service import LANGUAGE_BY_SUFFIX, _safe_archive_path
    file_languages = {}
    for relative in files:
        language = LANGUAGE_BY_SUFFIX.get(Path(relative).suffix.lower())
        frontend = _JOERN_FRONTENDS.get(language or "")
        if frontend and language in languages:
            file_languages.setdefault(language, []).append(relative)
    result["skipped_files"] = max(0, len(files) - sum(len(paths) for paths in file_languages.values()))
    if not file_languages:
        result["status"] = "unavailable"
        result["reason"] = "No detected source language has an installed Joern frontend mapping."
        return result
    try:
        # `joern --version` opens the shell and prints a banner; read its "Version: x" line.
        version = subprocess.run([joern, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, timeout=30, check=False,
                                 env={"PATH": os.environ.get("PATH", ""), "HOME": tempfile.gettempdir()})
        found = re.search(r"Version:\s*([0-9][0-9A-Za-z.+-]*)", version.stdout.decode("utf-8", "replace"))
        result["version"] = found.group(1)[:80] if found else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        result["version"] = "unknown"
    findings: list[dict[str, Any]] = []
    deadline = time.monotonic() + 210
    languages_completed: list[str] = []
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="code-quality-joern-") as workdir:
        archive_sources = os.path.join(workdir, "source")
        os.makedirs(archive_sources)
        from code_quality.service import MAX_FILE_BYTES
        selected = set(files)
        with zipfile.ZipFile(__import__("io").BytesIO(archive_data)) as archive:
            for member in sorted(archive.infolist(), key=lambda item: item.filename):
                relative = _safe_archive_path(member.filename)
                if relative not in selected or member.is_dir() or member.file_size > MAX_FILE_BYTES:
                    continue
                target = os.path.join(archive_sources, *relative.split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                try:
                    with open(target, "xb") as output:
                        output.write(archive.read(member))
                except FileExistsError:
                    failures.append("duplicate archive path")
        script_path = os.path.join(workdir, "dynamic-calls.sc")
        output_path = os.path.join(workdir, "calls.tsv")
        script = r'''@main def exec(cpgFile: String, outFile: String) = {
  importCpg(cpgFile)
  def enc(value: String): String = java.util.Base64.getEncoder.encodeToString(value.getBytes("UTF-8"))
  cpg.call.name("(?i)(eval|exec|system|popen|passthru|shell_exec|spawn|processbuilder)").take(500).map { call =>
    List(call.name, call.file.name.headOption.getOrElse(""), call.lineNumber.getOrElse(0).toString)
      .map(value => enc(Option(value).getOrElse(""))).mkString("\t")
  }.l #> outFile
}
'''
        # The regex and script are application-owned; project files never contribute Joern code.
        with open(script_path, "w", encoding="utf-8") as script_file:
            script_file.write(script)
        for language in sorted(file_languages):
            if time.monotonic() >= deadline:
                failures.append("overall Joern execution limit reached")
                break
            frontend = _JOERN_FRONTENDS[language]
            result["scanned_files"] += len(file_languages[language])
            language_root = os.path.join(workdir, "languages", language)
            os.makedirs(language_root, exist_ok=True)
            for relative in file_languages[language]:
                src = os.path.join(archive_sources, *relative.split("/"))
                if not os.path.isfile(src):
                    continue
                dst = os.path.join(language_root, *relative.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copyfile(src, dst)
            cpg_file = os.path.join(workdir, language + ".cpg.bin")
            remaining = max(1, min(75, int(deadline - time.monotonic())))
            try:
                parsed = subprocess.run([parser, language_root, "--language", frontend, "--output", cpg_file],
                                        cwd=workdir, env={"PATH": os.environ.get("PATH", ""),
                                                          "HOME": workdir, "LANG": "C.UTF-8",
                                                          "JAVA_TOOL_OPTIONS": "-Xms128m -Xmx2g -XX:MaxMetaspaceSize=768m"},
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        timeout=remaining, check=False,
                                        preexec_fn=_limit_analyzer_process if resource is not None else None)
                if parsed.returncode != 0 or not os.path.isfile(cpg_file):
                    failures.append(language + " frontend could not build a CPG")
                    continue
                remaining = max(1, min(75, int(deadline - time.monotonic())))
                queried = subprocess.run([joern, "--script", script_path, "--param", "cpgFile=" + cpg_file,
                                          "--param", "outFile=" + output_path], cwd=workdir,
                                         env={"PATH": os.environ.get("PATH", ""), "HOME": workdir,
                                              "LANG": "C.UTF-8",
                                              "JAVA_TOOL_OPTIONS": "-Xms128m -Xmx2g -XX:MaxMetaspaceSize=768m"}, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, timeout=remaining, check=False,
                                         preexec_fn=_limit_analyzer_process if resource is not None else None)
                if queried.returncode != 0 or not os.path.isfile(output_path):
                    failures.append(language + " query execution failed")
                    continue
                if os.path.getsize(output_path) > 2_000_000:
                    failures.append(language + " query output exceeded the limit")
                    continue
                with open(output_path, encoding="ascii", errors="ignore") as output:
                    for line in output:
                        values = line.rstrip("\r\n").split("\t")
                        if len(values) != 3:
                            continue
                        try:
                            name, file_path, line_number = [base64.b64decode(value, validate=True).decode("utf-8", "replace") for value in values]
                            line_number = int(line_number)
                        except (ValueError, UnicodeError):
                            continue
                        relative = file_path.replace("\\", "/").lstrip("./")
                        matched_file = next((path for path in file_languages[language]
                                             if relative == path or relative.endswith("/" + path)), None)
                        normalized_name = name.lower()
                        if matched_file and normalized_name in _DYNAMIC_CALLS and 0 < line_number:
                            findings.append({"rule": "joern:dynamic-execution-call", "category": "security",
                                             "severity": "warning", "file": matched_file,
                                             "line": line_number, "message": "Dynamic or shell execution call requires review: " + name[:80],
                                             "tool": "joern", "version": result["version"]})
                if language == "php":
                    flow_error = _run_php_flows(joern, workdir, cpg_file, file_languages[language],
                                                result["version"], deadline, findings)
                    if flow_error:
                        failures.append(flow_error)
                languages_completed.append(language)
                try:
                    os.remove(output_path)
                except OSError:
                    pass
            except subprocess.TimeoutExpired:
                failures.append(language + " exceeded the Joern execution limit")
            except (OSError, subprocess.SubprocessError):
                failures.append(language + " Joern process could not start")
    result["findings"] = findings[:500]
    result["languages"] = languages_completed
    result["unresolved"] = failures[:20]
    if not failures and len(languages_completed) == len(file_languages):
        result["status"] = "complete"
        result["reason"] = None
    else:
        result["status"] = "partial" if languages_completed else "failed"
        result["reason"] = "; ".join(failures[:3]) or "Joern did not complete every supported frontend."
    return result
