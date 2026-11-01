"""Authenticated, bounded transport to the isolated external analyzer worker."""
from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import os
import re
import socket
import ssl
import zipfile
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

MAX_WORKER_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_WORKER_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_WORKER_FINDINGS = 500
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_ALLOWED_ANALYZERS = {"sonarqube", "joern"}
_ALLOWED_STATUSES = {"complete", "partial", "failed", "unavailable", "disabled"}


class RemoteWorkerError(ValueError):
    """Safe error for worker connection, identity, or response failures."""


def _transport_error(exc: Exception) -> RemoteWorkerError:
    if isinstance(exc, ssl.SSLCertVerificationError):
        message = "The analyzer worker certificate is not trusted for this HTTPS hostname. Verify the worker CA and endpoint."
    elif isinstance(exc, socket.gaierror):
        message = "The API container could not resolve the analyzer worker hostname. Check the endpoint and container DNS."
    elif isinstance(exc, TimeoutError):
        message = "The analyzer worker connection timed out. Check that its HTTPS port is reachable from the API container."
    elif isinstance(exc, ConnectionRefusedError):
        message = "The analyzer worker refused the connection. Start the worker's HTTPS service and check its port. The endpoint must be reachable from the API container."
    elif isinstance(exc, ssl.SSLError):
        message = "The analyzer worker TLS handshake failed. Check that the worker trusts the API client certificate and serves HTTPS on this port."
    else:
        message = "The analyzer worker connection failed. Check the endpoint, worker service, and network access from the API container."
    return RemoteWorkerError(message)


def _connection(target: dict[str, Any], timeout: float,
                require_fingerprint: bool = True) -> tuple[http.client.HTTPSConnection, str]:
    endpoint = target.get("endpoint")
    pin = str(target.get("certificate_sha256") or "").lower()
    parsed = urlsplit(endpoint if isinstance(endpoint, str) else "")
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or (require_fingerprint and not _SHA256.fullmatch(pin))):
        raise RemoteWorkerError("The configured analyzer worker identity is invalid.")
    ca_file = os.environ.get("ANALYZER_WORKER_CA_FILE", "")
    cert_file = os.environ.get("ANALYZER_WORKER_CLIENT_CERT_FILE", "")
    key_file = os.environ.get("ANALYZER_WORKER_CLIENT_KEY_FILE", "")
    if not all(os.path.isfile(path) for path in (ca_file, cert_file, key_file)):
        raise RemoteWorkerError("The analyzer worker mutual-TLS identity is not installed in the API runtime.")
    try:
        context = ssl.create_default_context(cafile=ca_file)
        context.load_cert_chain(certfile=cert_file, keyfile=key_file)
        port = parsed.port or 443
        connection = http.client.HTTPSConnection(parsed.hostname, port, context=context, timeout=timeout)
        return connection, pin
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise RemoteWorkerError("The analyzer worker TLS identity could not be loaded.") from exc


def get_worker_fingerprint(endpoint: str) -> dict[str, str]:
    """Return the TLS peer fingerprint only after CA and hostname validation."""
    connection, _ = _connection({"endpoint": endpoint}, timeout=12, require_fingerprint=False)
    try:
        connection.connect()
        if connection.sock is None:
            raise RemoteWorkerError("The analyzer worker connection did not establish TLS.")
        peer = connection.sock.getpeercert(binary_form=True)
        if not peer:
            raise RemoteWorkerError("The analyzer worker did not present a TLS certificate.")
        return {"certificate_sha256": hashlib.sha256(peer).hexdigest()}
    except RemoteWorkerError:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError) as exc:
        raise _transport_error(exc) from exc
    finally:
        connection.close()


def _request(target: dict[str, Any], method: str, path: str,
             payload: dict[str, Any] | None = None, timeout: float = 12) -> dict[str, Any]:
    connection, expected_pin = _connection(target, timeout)
    try:
        connection.connect()
        if connection.sock is None:
            raise RemoteWorkerError("The analyzer worker connection did not establish TLS.")
        peer = connection.sock.getpeercert(binary_form=True)
        if not peer or not hashlib.sha256(peer).hexdigest() == expected_pin:
            raise RemoteWorkerError("The analyzer worker certificate does not match the saved fingerprint.")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json", "Connection": "close"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(MAX_WORKER_RESPONSE_BYTES + 1)
        if len(raw) > MAX_WORKER_RESPONSE_BYTES:
            raise RemoteWorkerError("The analyzer worker response exceeded its size limit.")
        if response.status < 200 or response.status >= 300:
            detail = ""
            try:
                error_body = json.loads(raw)
                if isinstance(error_body, dict) and isinstance(error_body.get("detail"), str):
                    detail = error_body["detail"].strip()[:240]
            except (ValueError, UnicodeError):
                pass
            if payload:
                for credential_field in ("token", "sonarqube_token"):
                    secret = payload.get(credential_field)
                    if isinstance(secret, str) and secret:
                        detail = detail.replace(secret, "[redacted]")
            raise RemoteWorkerError(detail or "The analyzer worker rejected the request.")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise RemoteWorkerError("The analyzer worker returned invalid structured output.") from exc
        if not isinstance(result, dict):
            raise RemoteWorkerError("The analyzer worker returned invalid structured output.")
        return result
    except RemoteWorkerError:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError) as exc:
        raise _transport_error(exc) from exc
    finally:
        connection.close()


def get_worker_capabilities(target: dict[str, Any]) -> dict[str, Any]:
    response = _request(target, "GET", "/v1/capabilities")
    if response.get("schema_version") != 1 or not isinstance(response.get("analyzers"), dict):
        raise RemoteWorkerError("The analyzer worker returned an unsupported capability response.")
    return response["analyzers"]


def check_worker(target: dict[str, Any]) -> dict[str, Any]:
    capabilities = get_worker_capabilities(target)
    return {"status": "ready", "analyzers": capabilities}


def check_sonarqube_access(target: dict[str, Any], server_url: str,
                           project_key: str, token: str) -> dict[str, Any]:
    """Ask the pinned worker to check SonarQube read access without transferring source."""
    if not isinstance(token, str) or not token or len(token) > 8192:
        raise RemoteWorkerError("Save a valid SonarQube token before testing access.")
    response = _request(target, "POST", "/v1/sonarqube/check", {
        "schema_version": 1, "server_url": server_url,
        "project_key": project_key, "token": token,
    }, timeout=30)
    if (response.get("schema_version") != 1 or response.get("status") != "ready"
            or response.get("read_access") != "verified"
            or response.get("project_key") != project_key
            or not isinstance(response.get("server_version"), str)):
        raise RemoteWorkerError("The analyzer worker returned an invalid SonarQube access result.")
    return {"schema_version": 1, "status": "ready", "read_access": "verified",
            "project_key": project_key, "server_version": response["server_version"][:80]}


def _filtered_source_bundle(archive_data: bytes, files: list[str]) -> tuple[bytes, dict[str, int]]:
    """Create a transient archive containing only validated inventory paths."""
    from code_quality.service import MAX_FILE_BYTES, MAX_SCAN_BYTES, _safe_archive_path

    selected = set(files)
    total = 0
    line_counts: dict[str, int] = {}
    output = io.BytesIO()
    try:
        with zipfile.ZipFile(io.BytesIO(archive_data)) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as bundle:
            seen: set[str] = set()
            for member in sorted(source.infolist(), key=lambda item: item.filename):
                path = _safe_archive_path(member.filename)
                if path not in selected or member.is_dir():
                    continue
                mode = (member.external_attr >> 16) & 0o170000
                if mode == 0o120000 or member.file_size > MAX_FILE_BYTES or path in seen:
                    raise RemoteWorkerError("The selected source archive contains an unsafe or duplicate file.")
                total += member.file_size
                if total > MAX_SCAN_BYTES:
                    raise RemoteWorkerError("The selected business source exceeds the worker transfer limit.")
                data = source.read(member)
                if len(data) != member.file_size:
                    raise RemoteWorkerError("The selected source archive contains an incomplete file.")
                bundle.writestr(path, data)
                line_counts[path] = data.count(b"\n") + 1
                seen.add(path)
            if seen != selected:
                raise RemoteWorkerError("The indexed source inventory does not match the selected revision archive.")
    except RemoteWorkerError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise RemoteWorkerError("The selected revision source archive is invalid.") from exc
    result = output.getvalue()
    if len(result) > MAX_WORKER_ARCHIVE_BYTES:
        raise RemoteWorkerError("The selected source bundle exceeds the worker transfer limit.")
    return result, line_counts


def run_worker(target: dict[str, Any], settings: dict[str, Any], archive_data: bytes,
               files: list[str], languages: dict[str, int], revision_id: str,
               commit_sha: str | None, token: str | None) -> dict[str, Any]:
    if not target.get("source_transfer_consent"):
        reason = "Explicit consent is required before transferring source to the configured analyzer worker."
        return {name: _unavailable(name, reason) for name in _ALLOWED_ANALYZERS}
    analyzers = [name for name in sorted(_ALLOWED_ANALYZERS)
                 if settings.get(name if name == "joern" else "sonarqube", {}).get("enabled")]
    if not analyzers:
        return {name: _unavailable(name, "The analyzer is disabled for this project.", "disabled")
                for name in _ALLOWED_ANALYZERS}
    try:
        source_bundle, line_counts = _filtered_source_bundle(archive_data, files)
    except RemoteWorkerError as exc:
        return {name: _unavailable(name, str(exc)) for name in analyzers} | {
            name: _unavailable(name, "The analyzer is disabled for this project.", "disabled")
            for name in _ALLOWED_ANALYZERS - set(analyzers)}
    bundle_sha256 = hashlib.sha256(source_bundle).hexdigest()
    payload = {
        "schema_version": 1,
        "job_id": str(uuid4()),
        "revision_id": revision_id,
        "commit_sha": commit_sha,
        "archive_base64": base64.b64encode(source_bundle).decode("ascii"),
        "bundle_sha256": bundle_sha256,
        "files": files,
        "file_line_counts": line_counts,
        "languages": languages,
        "analyzers": analyzers,
        "settings": {
            "sonarqube": {key: value for key, value in settings.get("sonarqube", {}).items()
                          if key in {"enabled", "server_url", "project_key", "source_upload_consent"}},
            "joern": {"enabled": bool(settings.get("joern", {}).get("enabled"))},
        },
        "sonarqube_token": token if "sonarqube" in analyzers else None,
    }
    try:
        result = _request(target, "POST", "/v1/jobs", payload, timeout=450)
        if (result.get("schema_version") != 1 or result.get("job_id") != payload["job_id"]
                or result.get("revision_id") != revision_id or result.get("commit_sha") != commit_sha
                or result.get("bundle_sha256") != bundle_sha256):
            raise RemoteWorkerError("The analyzer worker returned a result for a different job.")
        outputs = result.get("analyzers")
        if not isinstance(outputs, dict) or set(outputs) != set(analyzers):
            raise RemoteWorkerError("The analyzer worker response is missing analyzer results.")
        normalized: dict[str, Any] = {}
        for name in _ALLOWED_ANALYZERS:
            output = outputs.get(name)
            if name not in analyzers:
                normalized[name] = _unavailable(name, "The analyzer is disabled for this project.", "disabled")
                continue
            normalized[name] = _validate_result(name, output, set(files), line_counts)
        return normalized
    except RemoteWorkerError as exc:
        return {name: _unavailable(name, str(exc)) for name in analyzers} | {
            name: _unavailable(name, "The analyzer is disabled for this project.", "disabled")
            for name in _ALLOWED_ANALYZERS - set(analyzers)}


def _unavailable(name: str, reason: str, status: str = "unavailable") -> dict[str, Any]:
    return {"name": name, "version": "unknown", "status": status, "findings": [],
            "scanned_files": 0, "skipped_files": 0, "reason": reason[:300]}


def _validate_result(name: str, value: Any, selected_files: set[str],
                     line_counts: dict[str, int]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("name") != name or value.get("status") not in _ALLOWED_STATUSES:
        raise RemoteWorkerError("The analyzer worker returned a malformed analyzer result.")
    findings = value.get("findings", [])
    if not isinstance(findings, list) or len(findings) > MAX_WORKER_FINDINGS:
        raise RemoteWorkerError("The analyzer worker returned findings outside the configured limit.")
    safe_findings = []
    for finding in findings:
        if not isinstance(finding, dict):
            raise RemoteWorkerError("The analyzer worker returned a malformed finding.")
        path, line, message = finding.get("file"), finding.get("line"), finding.get("message")
        if (path not in selected_files or type(line) is not int or line < 1
                or line > line_counts.get(path, 0) or not isinstance(message, str)
                or not message.strip() or len(message) > 600
                or not isinstance(finding.get("rule"), str) or len(finding["rule"]) > 180
                or finding.get("category") not in {"security", "clean_code", "maintainability", "reliability", "performance"}
                or finding.get("tool") != name or not isinstance(finding.get("severity"), str)
                or len(finding["severity"]) > 24 or not isinstance(finding.get("version"), str)
                or len(finding["version"]) > 80):
            raise RemoteWorkerError("The analyzer worker returned an out-of-revision finding.")
        safe_findings.append({key: finding.get(key) for key in
                              ("rule", "category", "severity", "file", "line", "message", "tool", "version")})
    result = {key: value.get(key) for key in
              ("name", "version", "status", "scanned_files", "skipped_files", "supported_languages",
               "unsupported_languages", "languages", "unresolved", "reason")}
    result["findings"] = safe_findings
    result["version"] = str(result.get("version") or "unknown")[:80]
    result["reason"] = str(result["reason"] or "")[:300] or None
    for list_key in ("supported_languages", "unsupported_languages", "languages", "unresolved"):
        values = result.get(list_key)
        if values is not None and (not isinstance(values, list) or len(values) > 100
                                   or any(not isinstance(item, str) or len(item) > 240 for item in values)):
            raise RemoteWorkerError("The analyzer worker returned invalid coverage details.")
    for count_key in ("scanned_files", "skipped_files"):
        count = result.get(count_key, 0)
        if type(count) is not int or count < 0 or count > len(selected_files):
            raise RemoteWorkerError("The analyzer worker returned invalid coverage counts.")
    return result
