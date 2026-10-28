"""Persistent Runtime ClickHouse target and stable router configuration."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml


STORAGE_KEY = "runtime_storage"
_HOST = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_SSH_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\$?$")
_STATE_DIR = Path(os.getenv("TELEMETRY_RUNTIME_STATE_DIR", "/var/lib/devassistant/runtime"))
_CONTROL_DIR = Path(os.getenv("TELEMETRY_RUNTIME_CONTROL_DIR", "/var/lib/devassistant/collector-control"))
_BASE_CONFIG = Path(os.getenv("TELEMETRY_COLLECTOR_BASE_CONFIG", "/etc/devassistant/collector-runtime.base.yaml"))


def _registry(registry=None):
    if registry is None:
        from telemetry.registry import default_registry
        registry = default_registry()
    return registry


def clickhouse_credentials(*, require_remote_strength: bool = False) -> tuple[str, str]:
    username = os.getenv("TELEMETRY_CLICKHOUSE_USER", "telemetry")
    password = os.getenv("TELEMETRY_CLICKHOUSE_PASSWORD", "")
    if not _IDENTIFIER.fullmatch(username) or not password:
        raise ValueError("Configure a valid TELEMETRY_CLICKHOUSE_USER and TELEMETRY_CLICKHOUSE_PASSWORD")
    if require_remote_strength and (len(password) < 32 or password == "telemetry-local-dev-only"):
        raise ValueError(
            "Remote ClickHouse requires a strong TELEMETRY_CLICKHOUSE_PASSWORD of at least 32 characters. "
            "Set it in api/.env, restart the API services, then retry setup."
        )
    return username, password


def clickhouse_credentials_status() -> dict:
    try:
        clickhouse_credentials(require_remote_strength=True)
        return {"ready": True, "message": None}
    except ValueError as exc:
        return {"ready": False, "message": str(exc)}


def _validate_host(host: str) -> str:
    value = host.strip().rstrip(".")
    try:
        return str(ipaddress.ip_address(value.strip("[]")))
    except ValueError:
        if not value or len(value) > 253 or not _HOST.fullmatch(value):
            raise ValueError("Enter a valid remote hostname or IP address")
        labels = value.split(".")
        if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in labels):
            raise ValueError("Enter a valid remote hostname or IP address")
    return value


def _database() -> str:
    database = os.getenv("TELEMETRY_CLICKHOUSE_DATABASE", "telemetry")
    if not _IDENTIFIER.fullmatch(database):
        raise RuntimeError("Invalid TELEMETRY_CLICKHOUSE_DATABASE configuration")
    return database


def _base_target() -> dict:
    try:
        username, _ = clickhouse_credentials()
    except ValueError:
        username = None
    return {
        "mode": "local",
        "host": None,
        "http_port": None,
        "native_port": None,
        "ssh_port": None,
        "ssh_user": None,
        "host_fingerprint": None,
        "database": _database(),
        "username": username,
        "remote_configured": False,
        "health": "environment-configured" if os.getenv("TELEMETRY_CLICKHOUSE_URL") else "unavailable",
        "verified_at": None,
        "last_error": None,
    }


def get_storage_target(registry=None) -> dict:
    saved = _registry(registry).get_runtime_setting(STORAGE_KEY)
    if not isinstance(saved, dict) or not isinstance(saved.get("value"), dict):
        return _base_target()
    target = {**_base_target(), **saved["value"]}
    target.pop("source_cidr", None)
    target.pop("ca_certificate", None)
    if target.get("mode") not in {"disabled", "local", "remote"}:
        return _base_target()
    target["database"] = _database()
    target["username"] = _base_target()["username"]
    target["remote_configured"] = all(target.get(key) for key in ("host", "ssh_port", "ssh_user", "host_fingerprint"))
    if target["mode"] == "remote":
        target["http_port"] = 8123
        target["native_port"] = 9000
    return target


def public_storage_target(registry=None) -> dict:
    target = get_storage_target(registry)
    return {key: value for key, value in target.items() if key not in {"ca_certificate", "source_cidr"}}


def _status_target(target: dict, registry=None) -> dict:
    target["remote_configured"] = all(target.get(key) for key in ("host", "ssh_port", "ssh_user", "host_fingerprint"))
    if target["mode"] == "remote":
        target["http_port"] = 8123
        target["native_port"] = 9000
    return {key: value for key, value in target.items() if key not in {"ca_certificate", "source_cidr"}}


def storage_status(registry=None) -> dict:
    target = get_storage_target(registry)
    if target["mode"] == "disabled":
        target["health"] = "disabled"
        return _status_target(target)
    connection = clickhouse_connection(registry)
    if not connection["url"]:
        target["health"] = "unavailable"
        target["last_error"] = "ClickHouse is not configured for the selected storage mode."
    else:
        url = connection["url"] + "/?query=SELECT%201"
        token = base64.b64encode(f"{connection['username']}:{connection['password']}".encode()).decode("ascii")
        request = Request(url, headers={"Authorization": f"Basic {token}"})
        try:
            with urlopen(request, timeout=5, context=connection["ssl_context"]) as response:
                if response.status != 200 or response.read(32).strip() != b"1":
                    raise RuntimeError("ClickHouse health response was invalid")
            target["health"] = "verified"
            target["last_error"] = None
            target["verified_at"] = datetime.now(timezone.utc).isoformat()
        except Exception:
            target["health"] = "unavailable"
            target["last_error"] = "ClickHouse could not be reached or authenticated."
    return _status_target(target)


def validate_remote_target(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Remote ClickHouse settings must be an object")
    host = _validate_host(payload.get("host", ""))
    try:
        ssh_port = int(payload.get("ssh_port", 22))
    except (TypeError, ValueError) as exc:
        raise ValueError("SSH port must be between 1 and 65535") from exc
    if not 1 <= ssh_port <= 65535:
        raise ValueError("SSH port must be between 1 and 65535")
    ssh_user = payload.get("ssh_user")
    if not isinstance(ssh_user, str) or not _SSH_USER.fullmatch(ssh_user):
        raise ValueError("SSH user must be a valid Linux account name")
    fingerprint = payload.get("host_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.startswith("SHA256:") or len(fingerprint) > 100:
        raise ValueError("Confirm the SSH host fingerprint before continuing")
    if payload.get("private_key") or payload.get("private_key_passphrase"):
        raise ValueError("SSH key material must be saved through the desktop credential helper, not sent to the API")
    sudo_password = payload.get("sudo_password") if isinstance(payload.get("sudo_password"), str) else ""
    if len(sudo_password) > 256:
        raise ValueError("Sudo password is too long")
    database = _database()
    if payload.get("database") not in (None, "", database):
        raise ValueError("Remote ClickHouse must use the configured Runtime database")
    return {
        "host": host,
        "ssh_port": ssh_port,
        "ssh_user": ssh_user,
        "host_fingerprint": fingerprint,
        "sudo_password": sudo_password,
        "database": database,
    }


def _stored_fields(target: dict) -> dict:
    fields = (
        "mode", "host", "ssh_port", "ssh_user", "host_fingerprint", "health", "verified_at", "last_error",
    )
    return {key: target.get(key) for key in fields}


def _write_atomic(path: Path, content: str, *, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chown(path.parent, 0, 10001)
    except PermissionError:
        pass
    os.chmod(path.parent, 0o750)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        try:
            os.chown(tmp_name, 0, 10001)
        except PermissionError:
            pass
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def write_router_target(target: dict) -> None:
    remote = target["mode"] == "remote"
    value = {"mode": target["mode"], "host": "runtime-ssh-tunnel" if remote else None,
             "http_port": 8123 if remote else None, "native_port": 9000 if remote else None}
    _write_atomic(_STATE_DIR / "router-target.json", json.dumps(value, separators=(",", ":")))


def write_ssh_target(target: dict) -> None:
    value = {"mode": target["mode"], "host": target.get("host"),
             "ssh_port": target.get("ssh_port"), "ssh_user": target.get("ssh_user"),
             "host_fingerprint": target.get("host_fingerprint")}
    _write_atomic(_STATE_DIR / "ssh-target.json", json.dumps(value, separators=(",", ":")))


def write_collector_config(target: dict | None = None) -> None:
    target = target or get_storage_target()
    if not _BASE_CONFIG.is_file():
        return
    config = yaml.safe_load(_BASE_CONFIG.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(config.get("service"), dict):
        raise RuntimeError("Collector base configuration is invalid")
    if target["mode"] == "disabled":
        config["service"]["pipelines"] = {}
    _write_atomic(_STATE_DIR / "collector-runtime.yaml", yaml.safe_dump(config, sort_keys=False))


def request_collector_restart() -> bool:
    socket_path = _CONTROL_DIR / "collector-reload.sock"
    if not socket_path.exists():
        return False
    import socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(socket_path))
            client.sendall(b"restart\n")
            response = client.recv(128)
    except (ConnectionRefusedError, FileNotFoundError):
        # A stopped Collector can leave its relay socket in the shared volume.
        # Treat that stale endpoint like an absent relay; there is no live process to restart.
        return False
    if response != b"restarting\n":
        raise RuntimeError("Collector could not apply the Runtime storage target")
    return True


def prepare_collector_control() -> None:
    _CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chown(_CONTROL_DIR, 0, 10001)
    except PermissionError:
        pass
    os.chmod(_CONTROL_DIR, 0o770)


def initialize_runtime_storage() -> None:
    prepare_collector_control()
    target = get_storage_target()
    write_router_target(target)
    write_ssh_target(target)
    write_collector_config(target)


def _apply_target(target: dict, previous: dict, store, *, restart_for_disabled: bool) -> bool:
    store.set_runtime_setting(STORAGE_KEY, _stored_fields(target))
    try:
        write_router_target(target)
        write_ssh_target(target)
        write_collector_config(target)
        restart = restart_for_disabled and request_collector_restart()
        return restart
    except Exception:
        store.set_runtime_setting(STORAGE_KEY, _stored_fields(previous))
        write_router_target(previous)
        write_ssh_target(previous)
        write_collector_config(previous)
        raise


def save_storage_target(target: dict, registry=None) -> dict:
    if target.get("mode") != "remote":
        raise ValueError("Provisioned ClickHouse target must use Remote mode")
    _validate_host(str(target.get("host", "")))
    if type(target.get("ssh_port")) is not int or not 1 <= target["ssh_port"] <= 65535:
        raise ValueError("Remote SSH port is invalid")
    if not isinstance(target.get("ssh_user"), str) or not _SSH_USER.fullmatch(target["ssh_user"]):
        raise ValueError("Remote SSH user is invalid")
    if not isinstance(target.get("host_fingerprint"), str) or not target["host_fingerprint"].startswith("SHA256:"):
        raise ValueError("Remote SSH host fingerprint is invalid")
    username, _ = clickhouse_credentials(require_remote_strength=True)
    if target.get("username") != username:
        raise ValueError("Remote ClickHouse username does not match the configured Runtime account")
    store = _registry(registry)
    previous = get_storage_target(store)
    try:
        restarted = _apply_target(target, previous, store, restart_for_disabled=False)
        verify_router_clickhouse_connection()
    except Exception:
        store.set_runtime_setting(STORAGE_KEY, _stored_fields(previous))
        write_router_target(previous)
        write_ssh_target(previous)
        write_collector_config(previous)
        raise
    result = public_storage_target(registry)
    result["collector_restart_requested"] = restarted
    return result


def set_storage_mode(mode: str, registry=None) -> dict:
    if mode not in {"disabled", "local", "remote"}:
        raise ValueError("Storage mode must be disabled, local, or remote")
    previous = get_storage_target(registry)
    if mode == "remote" and not all(previous.get(key) for key in ("host", "ssh_port", "ssh_user", "host_fingerprint")):
        raise ValueError("Provision and verify a Remote ClickHouse target before selecting Remote")
    if mode == "local":
        verify_local_clickhouse_connection()
    candidate = {**previous, "mode": mode}
    store = _registry(registry)
    restart_required = mode == "disabled" or previous["mode"] == "disabled"
    try:
        # The router and SSH forward read their targets from shared state. Apply
        # the Remote target first so the verification request traverses the
        # newly selected tunnel instead of the previously active (often
        # Disabled) target.
        restarted = _apply_target(candidate, previous, store, restart_for_disabled=restart_required)
        if mode == "remote":
            verify_router_clickhouse_connection()
    except Exception:
        store.set_runtime_setting(STORAGE_KEY, _stored_fields(previous))
        write_router_target(previous)
        write_ssh_target(previous)
        write_collector_config(previous)
        raise
    result = public_storage_target(registry)
    result["collector_restart_requested"] = restarted
    return result


def clickhouse_connection(registry=None) -> dict:
    target = get_storage_target(registry)
    if target["mode"] == "disabled":
        return {"mode": "disabled", "url": "", "username": "", "password": "", "database": target["database"], "ssl_context": None}
    username, password = clickhouse_credentials()
    endpoint = (
        "http://telemetry-clickhouse-router:8123"
        if target["mode"] == "remote"
        else os.getenv("TELEMETRY_CLICKHOUSE_URL", "http://telemetry-clickhouse:8123")
    ).rstrip("/")
    return {
        "mode": target["mode"],
        "url": endpoint,
        "username": username,
        "password": password,
        "database": target["database"],
        "ssl_context": None,
    }


def verify_router_clickhouse_connection() -> None:
    url = "http://telemetry-clickhouse-router:8123/?query=SELECT%201"
    username, password = clickhouse_credentials(require_remote_strength=True)
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    request = Request(url, headers={"Authorization": f"Basic {token}"})
    deadline = time.monotonic() + 20
    last_error = None
    while time.monotonic() < deadline:
        try:
            with urlopen(request, timeout=2) as response:
                if response.status == 200 and response.read(32).strip() == b"1":
                    return
                last_error = RuntimeError("Remote ClickHouse verification returned an unexpected response")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError("Remote ClickHouse could not be verified through the SSH tunnel") from last_error


def verify_local_clickhouse_connection() -> None:
    """Check the configured local endpoint before making it the active target."""
    endpoint = os.getenv("TELEMETRY_CLICKHOUSE_URL", "http://telemetry-clickhouse:8123").rstrip("/")
    username, password = clickhouse_credentials()
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    request = Request(endpoint + "/?query=SELECT%201", headers={"Authorization": f"Basic {token}"})
    context = ssl.create_default_context() if endpoint.startswith("https://") else None
    try:
        with urlopen(request, timeout=5, context=context) as response:
            if response.status != 200 or response.read(32).strip() != b"1":
                raise RuntimeError("Local ClickHouse verification failed")
    except (HTTPError, URLError, TimeoutError, OSError, ssl.SSLError) as exc:
        raise RuntimeError("Local ClickHouse could not be reached or authenticated") from exc
