#!/usr/bin/env python3
"""Interactive local Docker Runtime setup using Gum in a terminal."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import base64
import hmac
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API = ROOT / "api"
CERT_DIR = API / "telemetry-dev" / "host" / "certs"
ENV_FILE = API / ".env"
LABEL_PREFIX = "com.devassistant.telemetry."
LABELS = {
    "enabled": LABEL_PREFIX + "enabled",
    "project": LABEL_PREFIX + "project",
    "environment": LABEL_PREFIX + "environment",
    "commit": LABEL_PREFIX + "commit",
    "service": LABEL_PREFIX + "service",
}
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
HELPER_HOST = "127.0.0.1"
HELPER_PORT = 47831
HELPER_COMMAND_TIMEOUT_SECONDS = 600
TERMINAL_OPERATION_START_TIMEOUT_SECONDS = 30
TERMINAL_OPERATION_TIMEOUT_SECONDS = 3600
LOCAL_DOCKER_COMMAND_TIMEOUT_SECONDS = 120
HELPER_STATE: dict = {"status": "idle", "message": "Ready", "updated": time.time()}
HELPER_LOCK = threading.Lock()
HELPER_USED_TOKENS: set[str] = set()


class ApprovalCancelled(RuntimeError):
    """The operator explicitly cancelled a grouped approval dialog."""


class GumError(RuntimeError):
    """A Gum prompt or its terminal could not be run."""


def state_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "devassistant" / "runtime" / "local-setup.json"


def read_state() -> dict | None:
    path = state_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read setup state at {path}: {exc}") from exc


def helper_secret() -> bytes:
    value = os.environ.get("RAG_CALLBACK_SECRET", "")
    if not value:
        env_path = API / ".env"
        if env_path.is_file():
            for line in env_path.read_text().splitlines():
                if line.startswith("RAG_CALLBACK_SECRET="):
                    value = line.split("=", 1)[1].strip().strip("\"'")
                    break
    if len(value) < 32:
        raise RuntimeError("Runtime helper authorization is unavailable: configure the shared RAG_CALLBACK_SECRET in api/.env and web/.env.")
    return hmac.new(value.encode(), b"devassistant-runtime-helper-v1", hashlib.sha256).digest()


def helper_origin() -> str:
    value = os.environ.get("WEB_ORIGIN", "")
    if not value:
        env_path = ROOT / "web" / ".env"
        if env_path.is_file():
            for line in env_path.read_text().splitlines():
                if line.startswith("WEB_ORIGIN="):
                    value = line.split("=", 1)[1].strip().strip("\"'")
                    break
    return value or "http://localhost:3000"


def verify_helper_token(token: str, origin: str, *, operation: str | None = None,
                        project_id: str | None = None, target_id: str | None = None,
                        unscoped: bool = False) -> bool:
    try:
        head, body, signature = token.split(".")
        expected = base64.urlsafe_b64encode(hmac.new(helper_secret(), f"{head}.{body}".encode(), hashlib.sha256).digest()).decode().rstrip("=")
        if not hmac.compare_digest(expected, signature): return False
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return (claims.get("aud") == "runtime-helper" and claims.get("origin") == origin
                and isinstance(claims.get("exp"), (int, float)) and claims["exp"] >= time.time()
                and (operation is None or claims.get("operation") == operation)
                and (project_id is None or claims.get("project_id") == project_id)
                and (target_id is None or claims.get("target_id") == target_id)
                and (not unscoped or (claims.get("operation") is None and claims.get("project_id") is None)))
    except (ValueError, OSError, RuntimeError, json.JSONDecodeError):
        return False


def resolve_ssh_config_target(payload: dict) -> dict:
    host = payload.get("host")
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}", host):
        raise ValueError("Enter a valid SSH host or alias from ~/.ssh/config.")
    command = ["ssh", "-G"]
    try:
        port = int(payload.get("ssh_port", 22))
    except (TypeError, ValueError) as exc:
        raise ValueError("SSH port must be between 1 and 65535.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SSH port must be between 1 and 65535.")
    if port != 22:
        command.extend(["-p", str(port)])
    user = payload.get("ssh_user", "")
    if user:
        if not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}\$?", user):
            raise ValueError("Enter a valid Linux SSH account name.")
        command.extend(["-l", user])
    command.append(host)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("OpenSSH configuration could not be read on this desktop.") from exc
    if result.returncode:
        raise RuntimeError("OpenSSH could not resolve this host alias. Check ~/.ssh/config.")
    options = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        options[key.lower()] = value.strip()
    if options.get("proxyjump", "none") != "none" or options.get("proxycommand", "none") != "none":
        raise ValueError("This Runtime tunnel requires a directly reachable SSH host; SSH ProxyJump and ProxyCommand aliases are not supported yet.")
    hostname = options.get("hostname")
    resolved_user = options.get("user")
    try:
        resolved_port = int(options.get("port", "22"))
    except ValueError as exc:
        raise RuntimeError("OpenSSH returned an invalid configured SSH port.") from exc
    if not hostname or not resolved_user or not 1 <= resolved_port <= 65535:
        raise RuntimeError("OpenSSH did not return a usable host, user, and port.")
    return {"alias": host, "host": hostname, "ssh_user": resolved_user, "ssh_port": resolved_port}


def serve_helper() -> None:
    import runtime_ssh_credentials as ssh_credentials

    try:
        ssh_credentials.reload_credentials()
    except Exception:
        # Keep the helper available so the Settings UI can explain unlock/retry.
        pass

    class Handler(BaseHTTPRequestHandler):
        server_version = "RuntimeSetupHelper/1"

        def _json(self, status: int, data: dict) -> None:
            payload = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            origin = self.headers.get("Origin", "")
            allowed = helper_origin()
            if origin == allowed:
                self.send_header("Access-Control-Allow-Origin", allowed)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(payload)

        def _authorized(self, *, operation: str | None = None,
                        project_id: str | None = None, target_id: str | None = None,
                        unscoped: bool = False) -> bool:
            origin = self.headers.get("Origin", "")
            allowed = helper_origin()
            host = self.headers.get("Host", "")
            if origin != allowed or host not in {f"127.0.0.1:{HELPER_PORT}", f"localhost:{HELPER_PORT}"}:
                return False
            scheme, _, authority = origin.partition("://")
            if scheme not in {"http", "https"} or not authority or authority.startswith("["):
                return False
            token = self.headers.get("Authorization", "")
            return token.startswith("Bearer ") and verify_helper_token(
                token[7:], origin, operation=operation, project_id=project_id,
                target_id=target_id, unscoped=unscoped)

        def _consume_authorization(self) -> bool:
            token_id = hashlib.sha256(self.headers.get("Authorization", "").encode()).hexdigest()
            with HELPER_LOCK:
                if token_id in HELPER_USED_TOKENS:
                    return False
                HELPER_USED_TOKENS.add(token_id)
            return True

        def do_OPTIONS(self) -> None:
            origin = self.headers.get("Origin", "")
            if origin != helper_origin():
                return self._json(403, {"detail": "Invalid browser origin."})
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Max-Age", "60")
            self.end_headers()

        def do_POST(self) -> None:
            if self.path in {"/code-quality-bearer", "/code-quality-bearer-use"}:
                length = self.headers.get("Content-Length", "0")
                if not length.isdigit() or not 1 <= int(length) <= 12 * 1024:
                    return self._json(400, {"detail": "Invalid Code Quality credential request."})
                try:
                    body = json.loads(self.rfile.read(int(length)))
                    if not isinstance(body, dict):
                        raise ValueError
                    target_id = body.get("target_id")
                    if not isinstance(target_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", target_id):
                        raise ValueError
                except (ValueError, json.JSONDecodeError):
                    return self._json(400, {"detail": "Invalid Code Quality target identity."})
                if self.path == "/code-quality-bearer-use":
                    if not self._authorized(operation="code-quality-benchmark-use", target_id=target_id):
                        return self._json(403, {"detail": "Benchmark credential authorization is invalid or expired."})
                    if not self._consume_authorization():
                        return self._json(403, {"detail": "This benchmark credential authorization has already been used."})
                    try:
                        token = ssh_credentials.use_bearer_token_for_benchmark(target_id)
                        return self._json(200, {"token": token})
                    except ssh_credentials.CredentialStoreError as exc:
                        return self._json(423 if exc.locked else 503, {"detail": str(exc)})
                    except ValueError as exc:
                        return self._json(422, {"detail": str(exc)})
                if not self._authorized(operation="code-quality-bearer-manage", target_id=target_id):
                    return self._json(403, {"detail": "Code Quality credential authorization is invalid or expired."})
                if not self._consume_authorization():
                    return self._json(403, {"detail": "This Code Quality credential authorization has already been used."})
                try:
                    if body.get("action") == "save":
                        result = ssh_credentials.save_bearer_token(target_id, body.get("token"))
                    elif body.get("action") == "status":
                        result = ssh_credentials.bearer_token_status(target_id)
                    else:
                        raise ValueError("Choose save or status.")
                    return self._json(200, result)
                except (ValueError, ssh_credentials.CredentialStoreError) as exc:
                    status = 423 if isinstance(exc, ssh_credentials.CredentialStoreError) and exc.locked else 422
                    return self._json(status, {"detail": str(exc)})
                except Exception:
                    return self._json(503, {"detail": "The Bearer token could not be saved to the OS credential store."})
            if self.path == "/runtime-ssh-config":
                if not self._authorized(unscoped=True):
                    return self._json(403, {"detail": "Runtime credential authorization is invalid or expired."})
                if not self._consume_authorization():
                    return self._json(403, {"detail": "This Runtime SSH configuration authorization has already been used."})
                length = self.headers.get("Content-Length", "0")
                if not length.isdigit() or not 1 <= int(length) <= 4096:
                    return self._json(400, {"detail": "Provide an SSH host or alias to resolve."})
                try:
                    payload = json.loads(self.rfile.read(int(length)))
                    if not isinstance(payload, dict):
                        raise ValueError("Provide an SSH host or alias to resolve.")
                    return self._json(200, resolve_ssh_config_target(payload))
                except ValueError as exc:
                    return self._json(422, {"detail": str(exc)})
                except RuntimeError as exc:
                    return self._json(503, {"detail": str(exc)})
            if self.path == "/runtime-ssh-credentials":
                if not self._authorized(unscoped=True):
                    return self._json(403, {"detail": "Runtime credential authorization is invalid or expired."})
                if not self._consume_authorization():
                    return self._json(403, {"detail": "This Runtime credential authorization has already been used."})
                length = self.headers.get("Content-Length", "0")
                if not length.isdigit() or not 1 <= int(length) <= 70 * 1024:
                    return self._json(400, {"detail": "Provide a valid SSH key and optional passphrase."})
                try:
                    body = json.loads(self.rfile.read(int(length)))
                    if not isinstance(body, dict):
                        raise ValueError("Provide a valid SSH key and optional passphrase.")
                    if body.get("action") == "reload":
                        ssh_credentials.reload_credentials()
                        return self._json(200, ssh_credentials.credential_status())
                    result = ssh_credentials.save_credentials(body.get("private_key"), body.get("passphrase", ""))
                    return self._json(200, result)
                except (ValueError, ssh_credentials.CredentialStoreError) as exc:
                    status = 423 if isinstance(exc, ssh_credentials.CredentialStoreError) and exc.locked else 422
                    return self._json(status, {"detail": str(exc)})
                except Exception:
                    return self._json(503, {"detail": "The SSH credential could not be saved. Unlock the OS keyring and retry."})
            if self.path not in {"/setup", "/cleanup", "/remove-project-containers"}:
                return self._json(404, {"detail": "Not found."})
            if not self._authorized(): return self._json(403, {"detail": "Runtime setup authorization is invalid or expired."})
            if self.path in {"/setup", "/cleanup"} and not self._authorized(unscoped=True):
                return self._json(403, {"detail": "This authorization is not valid for Runtime setup or cleanup."})
            project_name = None
            project_id = None
            if self.path in {"/setup", "/remove-project-containers"}:
                length = self.headers.get("Content-Length", "0")
                if not length.isdigit() or int(length) > 4096:
                    return self._json(400, {"detail": "Invalid setup request."})
                try:
                    body = json.loads(self.rfile.read(int(length)) or b"{}")
                except (ValueError, json.JSONDecodeError):
                    return self._json(400, {"detail": "Invalid setup request."})
                if not isinstance(body, dict):
                    return self._json(400, {"detail": "Invalid Runtime operation request."})
                if self.path == "/setup":
                    if (body.get("project_name") is not None and
                            (not isinstance(body["project_name"], str) or
                             not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", body["project_name"]))):
                        return self._json(400, {"detail": "Invalid Compose project selection."})
                    project_name = body.get("project_name")
                else:
                    project_id = body.get("project_id")
                    if not isinstance(project_id, str) or not SLUG.fullmatch(project_id):
                        return self._json(400, {"detail": "Invalid Runtime project ID."})
                    if not self._authorized(operation="runtime-local-container-removal", project_id=project_id):
                        return self._json(403, {"detail": "Runtime removal authorization is not scoped to this project."})
            token_id = hashlib.sha256(self.headers.get("Authorization", "").encode()).hexdigest()
            with HELPER_LOCK:
                if token_id in HELPER_USED_TOKENS:
                    return self._json(403, {"detail": "This Runtime setup authorization has already been used."})
                if HELPER_STATE["status"] in {"starting", "running"}:
                    return self._json(409, {"detail": "A Runtime operation is already running. Wait for it to finish before starting another."})
                HELPER_USED_TOKENS.add(token_id)
                action = ("cleanup" if self.path == "/cleanup" else
                          "remove local containers" if self.path == "/remove-project-containers" else "setup")
                HELPER_STATE.update(status="starting", message=f"Starting Runtime {action} in Gum", updated=time.time())
                for key in ("project_id", "removed_count", "target_count", "already_absent", "failed_containers"):
                    HELPER_STATE.pop(key, None)
                if project_id is not None:
                    HELPER_STATE["project_id"] = project_id
            def execute() -> None:
                with HELPER_LOCK: HELPER_STATE.update(status="running", message=f"Runtime {action} is running", updated=time.time())
                try:
                    result = run_helper_operation(action, project_name=project_name, project_id=project_id)
                    outcome = result.get("status", "complete") if isinstance(result, dict) else "complete"
                    message = result.get("message", f"Runtime {action} completed") if isinstance(result, dict) else f"Runtime {action} completed"
                    with HELPER_LOCK:
                        HELPER_STATE.update(status=outcome, message=message, updated=time.time())
                        if isinstance(result, dict):
                            HELPER_STATE.update({key: result[key] for key in
                                ("project_id", "removed_count", "target_count", "already_absent", "failed_containers")
                                if key in result})
                except Exception as exc:
                    with HELPER_LOCK:
                        message = str(exc)
                        cancelled = isinstance(exc, ApprovalCancelled) or "cancel" in message.lower() or "not approved" in message.lower()
                        HELPER_STATE.update(status="cancelled" if cancelled else "error", message=message, updated=time.time())
            threading.Thread(target=execute, name="runtime-setup", daemon=True).start()
            return self._json(202, {"status": "starting"})

        def do_GET(self) -> None:
            if self.path == "/health": return self._json(200, {"ok": True, "api_version": 25})
            if self.path == "/runtime-ssh-credentials":
                if not self._authorized(unscoped=True):
                    return self._json(403, {"detail": "Runtime credential authorization is invalid or expired."})
                return self._json(200, ssh_credentials.credential_status())
            if self.path not in {"/status", "/setup-state"}: return self._json(404, {"detail": "Not found."})
            if self.path == "/setup-state":
                if not self._authorized(unscoped=True):
                    return self._json(403, {"detail": "Runtime setup authorization is invalid or expired."})
                try:
                    saved = read_state()
                    if not saved: return self._json(200, {"configured": False})
                    project = saved.get("project") if isinstance(saved.get("project"), dict) else {}
                    return self._json(200, {"configured": True, "phase": saved.get("phase", "incomplete"),
                        "project_id": saved.get("project_id"), "environment": saved.get("environment"),
                        "commit": saved.get("commit"), "compose_project": project.get("name"),
                        "services": saved.get("services", [])})
                except RuntimeError as exc:
                    return self._json(500, {"detail": str(exc)})
            with HELPER_LOCK: state = dict(HELPER_STATE)
            project_id = state.get("project_id")
            if project_id:
                authorized = self._authorized(operation="runtime-local-container-removal", project_id=project_id)
            else:
                authorized = self._authorized(unscoped=True)
            if not authorized:
                return self._json(403, {"detail": "Runtime operation status authorization is invalid or expired."})
            return self._json(200, state)

        def do_DELETE(self) -> None:
            if self.path == "/code-quality-bearer":
                length = self.headers.get("Content-Length", "0")
                if not length.isdigit() or not 1 <= int(length) <= 2048:
                    return self._json(400, {"detail": "Invalid Code Quality target identity."})
                try:
                    body = json.loads(self.rfile.read(int(length)))
                    target_id = body.get("target_id") if isinstance(body, dict) else None
                except (ValueError, json.JSONDecodeError):
                    target_id = None
                if not isinstance(target_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", target_id):
                    return self._json(400, {"detail": "Invalid Code Quality target identity."})
                if not self._authorized(operation="code-quality-bearer-manage", target_id=target_id):
                    return self._json(403, {"detail": "Code Quality credential authorization is invalid or expired."})
                if not self._consume_authorization():
                    return self._json(403, {"detail": "This Code Quality credential authorization has already been used."})
                try:
                    return self._json(200, ssh_credentials.remove_bearer_token(target_id))
                except ssh_credentials.CredentialStoreError as exc:
                    return self._json(423 if exc.locked else 503, {"detail": str(exc)})
                except ValueError as exc:
                    return self._json(422, {"detail": str(exc)})
            if self.path != "/runtime-ssh-credentials":
                return self._json(404, {"detail": "Not found."})
            if not self._authorized(unscoped=True):
                return self._json(403, {"detail": "Runtime credential authorization is invalid or expired."})
            if not self._consume_authorization():
                return self._json(403, {"detail": "This Runtime credential authorization has already been used."})
            try:
                return self._json(200, ssh_credentials.remove_credentials())
            except ssh_credentials.CredentialStoreError as exc:
                status = 423 if exc.locked else 503
                return self._json(status, {"detail": str(exc)})

        def log_message(self, _format: str, *_args) -> None: return

    try:
        helper_secret()
        server = ThreadingHTTPServer((HELPER_HOST, HELPER_PORT), Handler)
    except OSError as exc:
        raise RuntimeError(f"Cannot bind Runtime setup helper to loopback port {HELPER_PORT}: {exc}") from exc
    print(f"Runtime setup helper listening on http://{HELPER_HOST}:{HELPER_PORT}")
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    server.serve_forever(poll_interval=0.5)
    server.server_close()


def write_state(value: dict | None) -> None:
    path = state_path()
    if value is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix="setup-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def gum_executable() -> str:
    executable = shutil.which("gum")
    if not executable:
        raise GumError("Runtime setup requires Gum. Install Gum, then restart the Runtime helper with 'just up'.")
    return executable


def terminal_device() -> str:
    return "CON" if os.name == "nt" else "/dev/tty"


def terminal_streams() -> tuple[object, object, int | None]:
    if sys.stdin.isatty() and sys.stderr.isatty():
        return sys.stdin, sys.stderr, None
    try:
        descriptor = os.open(terminal_device(), os.O_RDWR)
    except OSError as exc:
        raise GumError(f"Could not open the Runtime terminal for Gum: {exc}. Run 'just up' from a terminal session.") from exc
    if not os.isatty(descriptor):
        os.close(descriptor)
        raise GumError("Runtime setup needs an interactive terminal. Run 'just up' from a terminal session.")
    return descriptor, descriptor, descriptor


def _run_gum(args: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run Gum against the host terminal, even when the helper's standard streams are redirected."""
    command = [gum_executable(), *args]
    terminal_input, terminal_error, owned_descriptor = terminal_streams()
    try:
        return subprocess.run(command, stdin=terminal_input, stdout=subprocess.PIPE, stderr=terminal_error,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise GumError(f"Gum prompt timed out after {timeout} seconds.") from exc
    finally:
        if owned_descriptor is not None:
            os.close(owned_descriptor)


def _gum_cancelled(result: subprocess.CompletedProcess, label: str) -> None:
    if result.returncode in {1, 130, 143}:
        raise ApprovalCancelled(f"Runtime {label} was cancelled in Gum.")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise GumError(f"Gum {label} failed (exit {result.returncode}){': ' + detail if detail else '.'}")


def gum_choose(items: list[str], *, title: str, multi: bool = False) -> list[str]:
    if not items:
        return []
    args = ["choose", "--height", "12", "--header", title, "--cursor", "› ",
            "--cursor.foreground", "213", "--selected.foreground", "255"]
    if multi:
        args.extend(["--no-limit", "--ordered", "--selected-prefix", "☑ ", "--unselected-prefix", "☐ "])
    args.extend(items)
    result = _run_gum(args)
    _gum_cancelled(result, "selection")
    return [choice for choice in result.stdout.splitlines() if choice]


def gum_input(text: str, default: str = "") -> str:
    args = ["input", "--header", text, "--width", "80"]
    if default:
        args.extend(["--value", default])
    result = _run_gum(args)
    _gum_cancelled(result, "input")
    return (result.stdout or "").rstrip("\r\n")


def gum_confirm(text: str) -> bool:
    result = _run_gum(["confirm", "--affirmative", "Continue", "--negative", "Cancel", text])
    if result.returncode in {130, 143}:
        raise ApprovalCancelled("Runtime confirmation was cancelled in Gum.")
    if result.returncode == 1:
        return False
    _gum_cancelled(result, "confirmation")
    return True


def required_approval(title: str, items: list[str], summary: str = "") -> None:
    """Require explicit continuation; checklist boxes are optional acknowledgements."""
    if not items:
        return
    header = f"{title} · x toggles acknowledgements; Enter continues or Esc cancels"
    if summary:
        header = f"{header}\n{summary}"
    gum_choose(items, title=header, multi=True)


def prompt(text: str, default: str | None = None) -> str:
    answer = gum_input(text, default=default or "")
    return answer.strip() or (default or "")


def confirm(text: str) -> bool:
    return gum_confirm(text)


def require_terminal() -> None:
    gum_executable()
    terminal_input, _, owned_descriptor = terminal_streams()
    del terminal_input
    if owned_descriptor is not None:
        os.close(owned_descriptor)


def docker_socket_group_id(socket_path: Path) -> str:
    # Docker Desktop and OrbStack expose the host socket inside Linux containers
    # as root:root, even when the host socket has a different group ID.
    if sys.platform == "darwin":
        return "0"
    return str(socket_path.stat().st_gid)


def run(args: list[str], *, cwd: Path | None = None, env: dict | None = None,
        capture: bool = False, check: bool = True,
        timeout: float = HELPER_COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, cwd=cwd, env=env, text=True,
                                stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE if capture else None, check=False,
                                timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required command not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out after {timeout:g} seconds: {args[0]}") from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(args)}\n{detail}")
    return result


def local_docker_command_prefix() -> list[str]:
    """Pin Docker commands to a local Unix socket or Windows named pipe."""
    docker_host = os.environ.get("DOCKER_HOST", "").strip()
    if docker_host:
        endpoint = docker_host
    else:
        context_name = os.environ.get("DOCKER_CONTEXT", "").strip()
        if not context_name:
            context_name = run(["docker", "context", "show"], capture=True,
                               timeout=LOCAL_DOCKER_COMMAND_TIMEOUT_SECONDS).stdout.strip()
        if not context_name:
            raise RuntimeError("A local Docker context could not be identified.")
        try:
            contexts = json.loads(run(["docker", "context", "inspect", context_name], capture=True,
                                      timeout=LOCAL_DOCKER_COMMAND_TIMEOUT_SECONDS).stdout)
            endpoint = contexts[0]["Endpoints"]["docker"]["Host"]
        except (AttributeError, IndexError, KeyError, TypeError, ValueError, RuntimeError):
            raise RuntimeError("The selected Docker context could not be verified as local.") from None
    if not isinstance(endpoint, str):
        raise RuntimeError("The selected Docker endpoint could not be verified as local.")
    scheme, separator, socket_path = endpoint.partition("://")
    is_local_socket = separator and scheme.lower() == "unix" and socket_path.startswith("/")
    is_local_named_pipe = separator and scheme.lower() == "npipe" and socket_path.startswith("/")
    if not (is_local_socket or is_local_named_pipe):
        raise RuntimeError("The selected Docker endpoint is not local. Switch to a local socket or named-pipe context and retry.")
    # Pin every following command to the endpoint that was just validated so
    # a concurrent Docker context switch cannot redirect a mutation.
    return ["docker", "--host", endpoint]


def inspect_local_runtime_container(docker: list[str], container_id: str) -> dict | None:
    """Read and validate one container without exposing Docker errors to the UI."""
    result = run(docker + ["inspect", "--format", "{{json .}}", container_id], capture=True, check=False,
                 timeout=LOCAL_DOCKER_COMMAND_TIMEOUT_SECONDS)
    if result.returncode:
        detail = (result.stderr or "").lower()
        if "no such object" in detail or "not found" in detail:
            return None
        raise RuntimeError("Docker could not inspect a local Runtime container.")
    try:
        record = json.loads(result.stdout)
        if isinstance(record, list):
            record = record[0]
        if not isinstance(record, dict) or record.get("Id") != container_id:
            raise ValueError
        labels = record.get("Config", {}).get("Labels") or {}
        if not isinstance(labels, dict):
            labels = {}
        return {
            "container_id": container_id,
            "enabled": labels.get(LABELS["enabled"]) == "true",
            "project_id": labels.get(LABELS["project"]),
            "running": bool(record.get("State", {}).get("Running")),
        }
    except (IndexError, AttributeError, TypeError, ValueError, json.JSONDecodeError):
        raise RuntimeError("Docker returned unreadable metadata for a local container.") from None


def discover_local_runtime_containers(docker: list[str], project_id: str) -> list[dict]:
    result = run(docker + ["ps", "--all", "--quiet", "--no-trunc",
                           "--filter", f"label={LABELS['enabled']}=true",
                           "--filter", f"label={LABELS['project']}={project_id}"], capture=True,
                 timeout=LOCAL_DOCKER_COMMAND_TIMEOUT_SECONDS)
    containers = []
    for container_id in dict.fromkeys(line.strip() for line in result.stdout.splitlines() if line.strip()):
        record = inspect_local_runtime_container(docker, container_id)
        if record and record["enabled"] and record["project_id"] == project_id:
            containers.append(record)
    return containers


def remove_project_containers(project_id: str) -> dict:
    """Stop and remove only this project's opted-in containers on the local daemon."""
    require_terminal()
    docker = local_docker_command_prefix()
    targets = discover_local_runtime_containers(docker, project_id)
    if not targets:
        return {"status": "complete", "project_id": project_id, "removed_count": 0,
                "target_count": 0, "already_absent": 0, "failed_containers": [],
                "message": f"No matching local Runtime containers were found for {project_id}; nothing was changed."}

    running_count = sum(bool(item["running"]) for item in targets)
    stopped_count = len(targets) - running_count
    approval_item = (f"Stop and remove {len(targets)} local containers for project {project_id} "
                     f"({running_count} running, {stopped_count} stopped). Container writable-layer data will be lost.")
    summary = (f"Project: {project_id}\nLocal Docker endpoint: verified socket or named pipe\n"
               f"Matching containers: {len(targets)}\n"
               "Only exact Runtime-enabled and project labels are in scope. Images, volumes, Runtime history, and remote hosts remain.")
    required_approval("Remove local Runtime containers", [approval_item], summary=summary)

    approved_ids = {item["container_id"] for item in targets}
    current = discover_local_runtime_containers(docker, project_id)
    added_ids = {item["container_id"] for item in current} - approved_ids
    if added_ids:
        return {"status": "error", "project_id": project_id, "removed_count": 0,
                "target_count": len(targets), "already_absent": 0,
                "failed_containers": sorted(added_ids),
                "message": "New matching local containers appeared after approval. Nothing was removed; refresh and approve again."}

    removed = []
    already_absent = 0
    failures = []
    for container_id in sorted(approved_ids):
        try:
            before_stop = inspect_local_runtime_container(docker, container_id)
            if before_stop is None:
                already_absent += 1
                continue
            if not before_stop["enabled"] or before_stop["project_id"] != project_id:
                failures.append(container_id)
                continue
            if before_stop["running"]:
                stopped = run(docker + ["stop", container_id], capture=True, check=False,
                              timeout=LOCAL_DOCKER_COMMAND_TIMEOUT_SECONDS)
                if stopped.returncode:
                    failures.append(container_id)
                    continue
            before_remove = inspect_local_runtime_container(docker, container_id)
            if before_remove is None:
                already_absent += 1
                continue
            if not before_remove["enabled"] or before_remove["project_id"] != project_id:
                failures.append(container_id)
                continue
            removed_result = run(docker + ["rm", container_id], capture=True, check=False,
                                 timeout=LOCAL_DOCKER_COMMAND_TIMEOUT_SECONDS)
            if removed_result.returncode:
                failures.append(container_id)
                continue
            removed.append(container_id)
        except RuntimeError:
            failures.append(container_id)

    if failures:
        return {"status": "error", "project_id": project_id, "removed_count": len(removed),
                "target_count": len(targets), "already_absent": already_absent,
                "failed_containers": failures,
                "message": f"Removed {len(removed)} of {len(targets)} approved local containers for {project_id}; "
                           f"{len(failures)} could not be safely removed. Refresh and retry."}
    return {"status": "complete", "project_id": project_id, "removed_count": len(removed),
            "target_count": len(targets), "already_absent": already_absent,
            "failed_containers": [],
            "message": f"Removed {len(removed)} local containers for {project_id}; "
                       f"{already_absent} were already absent. Runtime history and remote containers were preserved."}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def yaml_module():
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("Local setup needs PyYAML in the Python 3 environment. Install it with: python3 -m pip install PyYAML") from exc
    return yaml


def yaml_services(text: str) -> dict:
    yaml = yaml_module()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Compose YAML cannot be safely edited: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        raise RuntimeError("Compose file has no top-level services mapping")
    return data["services"]


def yaml_node(text: str):
    yaml = yaml_module()
    try:
        return yaml.compose(text)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Compose YAML cannot be safely edited: {exc}") from exc


def map_pair(node, key: str):
    yaml = yaml_module()
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            return key_node, value_node
    return None


def line_offset(text: str, line: int) -> int:
    lines = text.splitlines(keepends=True)
    return sum(len(value) for value in lines[:min(line, len(lines))])


def line_indent(node, fallback: int) -> int:
    yaml = yaml_module()
    if isinstance(node, yaml.MappingNode):
        columns = [key.start_mark.column for key, _ in node.value if isinstance(key, yaml.ScalarNode)]
        if columns:
            return min(columns)
    if isinstance(node, yaml.SequenceNode):
        # Scalar marks in block sequences point after the dash and its space.
        columns = [max(0, item.start_mark.column - 2) for item in node.value]
        if columns:
            return min(columns)
    return fallback


def child_indent_step(node, parent_column: int) -> int:
    yaml = yaml_module()
    if isinstance(node, yaml.MappingNode):
        columns = [key.start_mark.column for key, _ in node.value if isinstance(key, yaml.ScalarNode)]
        if columns and min(columns) > parent_column:
            return min(columns) - parent_column
    return 2


def add_service_labels(text: str, service: str, values: dict[str, str]) -> str:
    """Patch only YAML scalar spans, preserving comments and unrelated formatting."""
    yaml = yaml_module()
    root = yaml_node(text)
    services_pair = map_pair(root, "services")
    service_pair = map_pair(services_pair[1], service) if services_pair else None
    if not service_pair:
        return text
    service_node = service_pair[1]
    if not isinstance(service_node, yaml.MappingNode) or service_node.flow_style:
        raise RuntimeError(f"Service {service} is not a block mapping; refusing an unsafe edit")
    service_indent = line_indent(service_node, service_pair[0].start_mark.column + 2)
    service_step = max(1, service_indent - service_pair[0].start_mark.column)
    labels_pair = map_pair(service_node, "labels")
    edits: list[tuple[int, int, str]] = []
    missing = dict(values)

    def append_block_lines(line: int, rows: list[str]) -> None:
        offset = line_offset(text, line)
        prefix = ""
        if offset == len(text) and text and not text.endswith(("\n", "\r")):
            prefix = "\n"
        edits.append((offset, offset, prefix + "".join(rows)))

    if labels_pair:
        labels_node = labels_pair[1]
        if isinstance(labels_node, yaml.MappingNode):
            for key_node, value_node in labels_node.value:
                if not isinstance(key_node, yaml.ScalarNode) or key_node.value not in values:
                    continue
                if not isinstance(value_node, yaml.ScalarNode):
                    raise RuntimeError(f"Service {service} has a complex value for {key_node.value}; refusing an unsafe edit")
                edits.append((value_node.start_mark.index, value_node.end_mark.index,
                              json.dumps(values[key_node.value])))
                missing.pop(key_node.value, None)
            if missing and labels_node.flow_style:
                start = labels_node.end_mark.index - 1
                items = ", ".join(f"{json.dumps(k)}: {json.dumps(v)}" for k, v in missing.items())
                separator = ", " if labels_node.value else ""
                edits.append((start, start, separator + items))
            elif missing:
                step = child_indent_step(service_node, service_pair[0].start_mark.column)
                indent = line_indent(labels_node, labels_pair[0].start_mark.column + step)
                newline = "\r\n" if "\r\n" in text else "\n"
                append_block_lines(labels_node.end_mark.line,
                                   [" " * indent + f"{json.dumps(k)}: {json.dumps(v)}" + newline
                                    for k, v in missing.items()])
        elif isinstance(labels_node, yaml.SequenceNode):
            for item in labels_node.value:
                if not isinstance(item, yaml.ScalarNode):
                    raise RuntimeError(f"Service {service} has a complex labels list; refusing an unsafe edit")
                key = item.value.split("=", 1)[0]
                if key not in values:
                    continue
                edits.append((item.start_mark.index, item.end_mark.index,
                              json.dumps(f"{key}={values[key]}")))
                missing.pop(key, None)
            if missing and labels_node.flow_style:
                start = labels_node.end_mark.index - 1
                items = ", ".join(json.dumps(f"{k}={v}") for k, v in missing.items())
                separator = ", " if labels_node.value else ""
                edits.append((start, start, separator + items))
            elif missing:
                step = child_indent_step(service_node, service_pair[0].start_mark.column)
                indent = line_indent(labels_node, labels_pair[0].start_mark.column + step)
                newline = "\r\n" if "\r\n" in text else "\n"
                append_block_lines(labels_node.end_mark.line,
                                   [" " * indent + "- " + json.dumps(f"{k}={v}") + newline
                                    for k, v in missing.items()])
        else:
            raise RuntimeError(f"Service {service} uses an unsupported labels format; refusing an unsafe edit")
    elif values:
        indent = service_indent
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = [" " * indent + "labels:" + newline]
        lines.extend(" " * (indent + service_step) + f"{json.dumps(k)}: {json.dumps(v)}" + newline
                     for k, v in values.items())
        append_block_lines(service_node.end_mark.line, lines)

    updated = text
    for start, end, replacement in sorted(edits, key=lambda edit: edit[0], reverse=True):
        updated = updated[:start] + replacement + updated[end:]
    try:
        yaml.safe_load(updated)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Proposed labels for service {service} would make Compose YAML invalid: {exc}") from exc
    return updated


def compose_args(project: dict) -> list[str]:
    args = ["docker", "compose", "--project-directory", project["directory"], "-p", project["name"]]
    for path in project["files"]:
        args.extend(["-f", path])
    return args


def discover_projects() -> list[dict]:
    result = run(["docker", "compose", "ls", "--all", "--format", "json"], capture=True)
    try:
        rows = json.loads(result.stdout or "[]")
    except ValueError as exc:
        raise RuntimeError("Docker Compose returned an unreadable project list") from exc
    projects = []
    for row in rows:
        config_files = row.get("ConfigFiles") or row.get("configFiles") or ""
        if isinstance(config_files, list):
            files = [str(part).strip() for part in config_files if str(part).strip()]
        else:
            files = [part.strip() for part in str(config_files).split(",") if part.strip()]
        if files:
            projects.append({"name": row.get("Name") or row.get("name"),
                             "status": row.get("Status") or row.get("status") or "unknown",
                             "files": [str(Path(path).expanduser().resolve()) for path in files],
                             "directory": str(Path(files[0]).expanduser().resolve().parent)})
    return projects


def choose_project(projects: list[dict]) -> dict:
    if not projects:
        raise RuntimeError("Docker Compose did not report any local projects with config files")
    choices = [f"{item['name']} ({item['status']}) — {', '.join(item['files'])}" for item in projects]
    selected = gum_choose(choices, title="Choose the Compose project whose services should report")
    if not selected:
        raise ApprovalCancelled("Runtime project selection was cancelled in Gum.")
    return projects[choices.index(selected[0])]


def update_env_scope(fingerprint: str, project_id: str, environment: str,
                     *, remove: bool = False) -> tuple[str, str]:
    before = ENV_FILE.read_text() if ENV_FILE.exists() else ""
    lines = before.splitlines(True)
    found = None
    for i, line in enumerate(lines):
        if line.startswith("TELEMETRY_HOST_SCOPES="):
            found = i
            break
    raw = lines[found].split("=", 1)[1].strip() if found is not None else "{}"
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]
    try:
        scopes = json.loads(raw or "{}")
    except ValueError as exc:
        raise RuntimeError("api/.env has invalid TELEMETRY_HOST_SCOPES JSON; it was left unchanged") from exc
    if not isinstance(scopes, dict):
        raise RuntimeError("TELEMETRY_HOST_SCOPES must be a JSON object")
    old_scope = json.loads(json.dumps(scopes.get(fingerprint)))
    if remove:
        host = scopes.get(fingerprint)
        projects = host.get("projects", {}) if isinstance(host, dict) else {}
        envs = projects.get(project_id, []) if isinstance(projects, dict) else []
        if environment in envs:
            envs.remove(environment)
        if isinstance(projects, dict):
            if envs:
                projects[project_id] = envs
            else:
                projects.pop(project_id, None)
            if not projects:
                scopes.pop(fingerprint, None)
    else:
        host = scopes.setdefault(fingerprint, {"projects": {}})
        if not isinstance(host, dict) or not isinstance(host.get("projects", {}), dict):
            raise RuntimeError("Existing host scope has an unsupported structure; it was left unchanged")
        projects = host.setdefault("projects", {})
        envs = projects.setdefault(project_id, [])
        if not isinstance(envs, list) or not all(isinstance(value, str) for value in envs):
            raise RuntimeError("Existing project scope has an unsupported structure; it was left unchanged")
        if environment not in envs:
            envs.append(environment)
    value = json.dumps(scopes, separators=(",", ":"), sort_keys=True)
    line = f"TELEMETRY_HOST_SCOPES={value}\n"
    if found is None:
        after = before + ("" if not before or before.endswith("\n") else "\n") + line
    else:
        lines[found] = line
        after = "".join(lines)
    return before, after


def write_text_atomic(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.chmod(temp_name, mode if mode is not None else (path.stat().st_mode & 0o777 if path.exists() else 0o600))
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def cert_fingerprint() -> str:
    cert = CERT_DIR / "agent" / "host.crt"
    result = run(["openssl", "x509", "-in", str(cert), "-noout", "-fingerprint", "-sha256"], capture=True)
    value = result.stdout.strip().split("=", 1)[-1].replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RuntimeError("Could not read a valid local host certificate fingerprint")
    return value


def cert_directory_is_empty_scaffold() -> bool:
    if not CERT_DIR.exists():
        return True
    if CERT_DIR.is_symlink() or not CERT_DIR.is_dir():
        return False
    return all(path.is_dir() and not path.is_symlink() for path in CERT_DIR.rglob("*"))


def start_ingress_and_agent() -> tuple[dict, str]:
    created_certs = cert_directory_is_empty_scaffold()
    if created_certs:
        run([str(API / "telemetry-dev" / "host" / "bootstrap-dev-certs.sh"), str(CERT_DIR)])
    needed = [CERT_DIR / "ingress" / name for name in ("ca.pem", "server.crt", "server.key")]
    needed += [CERT_DIR / "agent" / name for name in ("ca.pem", "host.crt", "host.key")]
    if not all(path.is_file() for path in needed):
        raise RuntimeError(f"Local identity directory is incomplete: {CERT_DIR}")
    fingerprint = cert_fingerprint()
    CURRENT_SETUP.update({"fingerprint": fingerprint, "certificates_created": created_certs,
                          "scope_added": False, "scope_original": False})
    write_state(CURRENT_SETUP)
    before, after = update_env_scope(fingerprint, CURRENT_SETUP["project_id"], CURRENT_SETUP["environment"])
    if before != after:
        CURRENT_SETUP.update({"scope_added": True, "scope_original": True,
                              "scope_file_before": before, "scope_file_after_hash": digest(after)})
        write_state(CURRENT_SETUP)
        write_text_atomic(ENV_FILE, after)
    CURRENT_SETUP.update({"scope_added": before != after, "scope_original": before != after})
    write_state(CURRENT_SETUP)
    print("\nStarting local API ingress and host agent…")
    run(["docker", "compose", "--profile", "runtime-host", "up", "-d", "python-api", "telemetry-caddy"], cwd=API)
    socket_path = Path(os.environ.get("DOCKER_SOCK_PATH", "/var/run/docker.sock"))
    if not socket_path.exists():
        raise RuntimeError(f"Docker socket not found at {socket_path}; set DOCKER_SOCK_PATH and retry")
    env = os.environ.copy()
    env.update({"HOST_IDENTITY_DIR": str(CERT_DIR / "agent"),
                "BACKEND_URL": "https://telemetry-caddy:9443",
                "DOCKER_SOCK_PATH": str(socket_path),
                "DOCKER_SOCK_GID": docker_socket_group_id(socket_path)})
    run(["docker", "compose", "-f", "telemetry-agent/docker-compose.yml", "-f",
         "telemetry-agent/docker-compose.local.yml", "up", "-d", "--build"], cwd=API, env=env)
    return CURRENT_SETUP, fingerprint


CURRENT_SETUP: dict = {}


def setup(project_name: str | None = None) -> dict | None:
    require_terminal()
    saved = read_state()
    if saved:
        phase = saved.get("phase", "incomplete")
        project = saved.get("project") if isinstance(saved.get("project"), dict) else {}
        if phase == "active":
            message = (
                f"Runtime is already active for {saved.get('project_id')} · {saved.get('environment')} "
                f"(Compose project {project.get('name', 'unknown')}). No changes were made. "
                "Run just runtime-cleanup before setting up a different project."
            )
            print(message)
            return {"status": "complete", "message": message}
        raise RuntimeError(
            f"A local Runtime recovery record is in phase '{phase}' at {state_path()}. "
            "Run just runtime-status, then just runtime-cleanup before starting a new setup; "
            "the recovery record was preserved."
        )
    yaml_module()
    print("Runtime setup runs in this terminal. The browser cannot access your Docker daemon or project files.")
    print("The command requests permission before reading Docker metadata and the selected project files.")
    if not confirm("Allow this command to query Docker for local Compose project metadata?"):
        raise RuntimeError("Setup cancelled before Docker metadata was read.")
    projects = discover_projects()
    if project_name is None:
        project = choose_project(projects)
    else:
        project = next((item for item in projects if item["name"] == project_name), None)
        if project is None:
            raise RuntimeError("The selected Compose project is no longer available. Reload the project list and try again.")
    print(f"Selected project directory: {project['directory']}")
    base = compose_args(project)
    result = run(base + ["config", "--services"], cwd=Path(project["directory"]), capture=True)
    services = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if not services:
        raise RuntimeError("The selected Compose project has no services")
    while True:
        picked = gum_choose(
            services,
            title="Choose Runtime services · x toggles; select at least one, then press Enter",
            multi=True,
        )
        selected_set = set(picked)
        selected = [service for service in services if service in selected_set]
        if selected:
            break
    while True:
        project_id = prompt("Runtime project ID (letters, numbers, dot, dash, underscore)")
        if SLUG.fullmatch(project_id): break
        print("Project ID must be 1–128 letters, numbers, dots, dashes, or underscores.")
    while True:
        environment = prompt("Environment", "development")
        if SLUG.fullmatch(environment): break
        print("Environment must be 1–128 letters, numbers, dots, dashes, or underscores.")
    commit_result = run(["git", "rev-parse", "HEAD"], cwd=Path(project["directory"]), capture=True, check=False)
    proposed = commit_result.stdout.strip().lower() if commit_result.returncode == 0 else ""
    print(f"Detected Git commit: {proposed or 'not available'}")
    if proposed and COMMIT.fullmatch(proposed):
        commit = proposed
    else:
        while True:
            commit = prompt("Enter the full lowercase Git commit SHA-1 or SHA-256")
            if COMMIT.fullmatch(commit): break
            print("Commit must be a full lowercase 40-character SHA-1 or 64-character SHA-256.")

    files = [Path(path) for path in project["files"]]
    for path in files:
        if not path.is_file():
            raise RuntimeError(f"Compose file no longer exists: {path}")
    contents = {path: path.read_text() for path in files}
    services_by_file = {}
    for path in files:
        try:
            services_by_file[path] = yaml_services(contents[path])
        except RuntimeError as exc:
            raise RuntimeError(f"Could not safely read Compose file {path}: {exc}") from exc
    owners = {}
    for service in selected:
        for path in files:
            if service in services_by_file[path]:
                owners[service] = path
    if set(owners) != set(selected):
        raise RuntimeError("Could not locate all selected service definitions in the Compose files")
    CURRENT_SETUP.update({"version": 1, "project_id": project_id, "environment": environment,
                          "commit": commit, "project": project, "services": selected,
                          "compose_originals": {}, "compose_hashes": {}, "phase": "starting"})
    labels = {}
    proposed_texts = dict(contents)
    for service in selected:
        values = {LABELS["enabled"]: "true", LABELS["project"]: project_id,
                  LABELS["environment"]: environment, LABELS["commit"]: commit, LABELS["service"]: service}
        path = owners[service]
        try:
            proposed_texts[path] = add_service_labels(proposed_texts[path], service, values)
        except RuntimeError as exc:
            raise RuntimeError(f"Could not safely propose labels for {service} in {path}: {exc}") from exc
        labels[service] = values
    changed = [path for path in files if contents[path] != proposed_texts[path]]
    if not changed:
        raise RuntimeError("Selected services already have the requested telemetry labels")
    changed_paths = ", ".join(str(path) for path in changed)
    print(f"\nRuntime setup summary: Compose project {project['name']}; services {', '.join(selected)}; files {changed_paths}; selected services will be recreated.")
    required_approval("Approve Runtime setup", [
        f"Read the selected Compose files and Git commit ({', '.join(str(path) for path in files)})",
        f"Create local certificates under {CERT_DIR}" if cert_directory_is_empty_scaffold() else "Reuse the existing local certificates",
        "Update API host authorization scope",
        f"Apply telemetry labels to selected services: {', '.join(selected)}",
        f"Recreate selected services in Compose project {project['name']}",
        "Start or refresh the local API ingress and telemetry agent",
    ], summary=f"Compose project: {project['name']}\nServices: {', '.join(selected)}\nAffected files: {changed_paths}")
    CURRENT_SETUP["label_values"] = labels
    for path in changed:
        CURRENT_SETUP["compose_originals"][str(path)] = contents[path]
        CURRENT_SETUP["compose_hashes"][str(path)] = digest(proposed_texts[path])
    write_state(CURRENT_SETUP)
    try:
        for path in changed:
            if digest(path.read_text()) != digest(contents[path]):
                raise RuntimeError(f"Compose file changed after approval: {path}; no further edits were applied")
            write_text_atomic(path, proposed_texts[path])
    except Exception:
        print(f"\nSetup stopped. Recovery copies are recorded at {state_path()}.")
        raise
    CURRENT_SETUP["phase"] = "compose-labeled"
    write_state(CURRENT_SETUP)
    try:
        _, fingerprint = start_ingress_and_agent()
    except Exception:
        print(f"\nSetup stopped. Recovery copies and progress are recorded at {state_path()}.")
        print("Run just runtime-status for the current setup state.")
        raise

    recreate = base + ["up", "-d", "--no-deps", *selected]
    run(recreate, cwd=Path(project["directory"]))
    CURRENT_SETUP["phase"] = "active"
    write_state(CURRENT_SETUP)
    print("\nSetup complete.")
    print(f"Runtime project: {project_id} · {environment} · {commit}")
    print("Open Runtime and select the project/commit. Container reporting needs no source snapshot.")
    print("Optional: add source snapshots for code matching, and spans for routes and database activity.")
    print("Check setup: just runtime-status")
    print("Remove setup: just runtime-cleanup")


def status() -> None:
    require_terminal()
    state = read_state()
    if not state:
        print("No local Runtime tracking setup is recorded.")
        return
    print(f"Runtime setup: {state.get('phase', 'incomplete')}")
    print(f"Project: {state.get('project_id')} · {state.get('environment')} · {state.get('commit')}")
    print(f"Compose project: {state.get('project', {}).get('name')}")
    print(f"Services: {', '.join(state.get('services', []))}")
    print(f"Host certificate: {state.get('fingerprint', 'not provisioned')}")
    print(f"Recovery state: {state_path()}")
    if confirm("Allow Docker status inspection for the local API ingress and host agent?"):
        run(["docker", "compose", "--profile", "runtime-host", "ps", "python-api", "telemetry-caddy"], cwd=API, check=False)
        env = os.environ.copy()
        env.update({"HOST_IDENTITY_DIR": str(CERT_DIR / "agent"), "BACKEND_URL": "https://telemetry-caddy:9443"})
        run(["docker", "compose", "-f", "telemetry-agent/docker-compose.yml", "-f",
             "telemetry-agent/docker-compose.local.yml", "ps"], cwd=API, env=env, check=False)


def managed_agent_environment(*, require_active: bool = True) -> tuple[dict, dict] | tuple[None, None]:
    state = read_state()
    if not state:
        return None, None
    if require_active and state.get("phase") not in {"active", "labels-applied-services-not-recreated"}:
        print(f"Local Runtime setup is {state.get('phase', 'incomplete')}; recover it with runtime status/cleanup before starting services.")
        return None, None
    required = [CERT_DIR / "agent" / name for name in ("ca.pem", "host.crt", "host.key")]
    if not all(path.is_file() for path in required):
        print(f"Local host identity is incomplete at {CERT_DIR}; the telemetry agent was not started.")
        return None, None
    socket_path = Path(os.environ.get("DOCKER_SOCK_PATH", "/var/run/docker.sock"))
    if not socket_path.exists():
        print(f"Docker socket not found at {socket_path}; the telemetry agent was not started.")
        return None, None
    env = os.environ.copy()
    env.update({"HOST_IDENTITY_DIR": str(CERT_DIR / "agent"),
                "BACKEND_URL": "https://telemetry-caddy:9443",
                "DOCKER_SOCK_PATH": str(socket_path),
                "DOCKER_SOCK_GID": docker_socket_group_id(socket_path)})
    args = ["docker", "compose", "-f", "telemetry-agent/docker-compose.yml", "-f",
            "telemetry-agent/docker-compose.local.yml"]
    return {"env": env, "cwd": API}, {"args": args}


def ensure_host_services() -> None:
    command, compose = managed_agent_environment()
    if command is None:
        # Clean up a Caddy container left behind by older default-profile startup.
        run(["docker", "compose", "--profile", "runtime-host", "stop", "telemetry-caddy"], cwd=API, check=False)
        if not read_state():
            print("No configured local Runtime agent to start.")
        return
    print("==> Starting local Runtime ingress and host agent")
    run(["docker", "compose", "--profile", "runtime-host", "up", "-d", "python-api", "telemetry-caddy"],
        cwd=API)
    run(compose["args"] + ["up", "-d"], cwd=command["cwd"], env=command["env"])


def stop_host_services() -> None:
    run(["docker", "compose", "--profile", "runtime-host", "stop", "telemetry-caddy"], cwd=API, check=False)
    command, compose = managed_agent_environment(require_active=False)
    if command is None:
        if not read_state():
            print("No configured local Runtime agent to stop.")
        return
    print("==> Stopping local Runtime host agent")
    run(compose["args"] + ["down"], cwd=command["cwd"], env=command["env"])


def restore_compose(state: dict) -> bool:
    safe = True
    for raw_path, original in state.get("compose_originals", {}).items():
        path = Path(raw_path)
        if not path.exists():
            print(f"Compose file is missing; recovery copy remains in state: {path}")
            safe = False
            continue
        write_text_atomic(path, original)
    return safe


def cleanup() -> dict:
    require_terminal()
    state = read_state()
    if not state:
        print("No local Runtime tracking setup is recorded.")
        return {"status": "complete", "message": "No local Runtime setup record was present."}
    compose_files = list(state.get("compose_originals", {}).keys())
    certs_applicable = bool(state.get("certificates_created") and CERT_DIR.exists())
    actions = [
        "Stop the telemetry agent and Runtime ingress",
        f"Restore every saved Compose file ({', '.join(compose_files) or 'none'})",
        f"Recreate selected Compose services without Runtime labels ({', '.join(state.get('services', [])) or 'none'})",
        "Remove the host/project/environment authorization",
        "Refresh python-api when required",
    ]
    if certs_applicable:
        actions.append(f"Permanently remove generated development certificates under {CERT_DIR}")
    print(f"\nRuntime cleanup summary: files {', '.join(compose_files) or 'none'}; project {state.get('project_id')}; services {', '.join(state.get('services', [])) or 'none'}.")
    required_approval("Approve Runtime cleanup", actions,
                      summary=f"Project: {state.get('project_id')} · {state.get('environment')}\nAffected files: {', '.join(compose_files) or 'none'}")
    try:
        project = state.get("project") if isinstance(state.get("project"), dict) else None
        services = [item for item in state.get("services", []) if isinstance(item, str) and SLUG.fullmatch(item)]
        if not project or not services:
            raise RuntimeError("Saved Runtime project metadata is incomplete; selected services could not be recreated safely")
        compose_ok = restore_compose(state)
        if not compose_ok:
            raise RuntimeError("One or more saved Compose files could not be restored")
        run(compose_args(project) + ["up", "-d", "--no-deps", *services], cwd=Path(project["directory"]))
        env = os.environ.copy()
        env.update({"HOST_IDENTITY_DIR": str(CERT_DIR / "agent"), "BACKEND_URL": "https://telemetry-caddy:9443"})
        run(["docker", "compose", "-f", "telemetry-agent/docker-compose.yml", "-f",
             "telemetry-agent/docker-compose.local.yml", "down"], cwd=API, env=env, check=False)
        run(["docker", "compose", "--profile", "runtime-host", "stop", "telemetry-caddy"], cwd=API, check=False)
        fingerprint = state.get("fingerprint")
        if fingerprint and state.get("scope_original"):
            before, after = update_env_scope(fingerprint, state["project_id"], state["environment"], remove=True)
            if before != after:
                write_text_atomic(ENV_FILE, after)
                run(["docker", "compose", "up", "-d", "python-api"], cwd=API, check=False)
        if certs_applicable:
            shutil.rmtree(CERT_DIR)
    except Exception as exc:
        state["phase"] = "cleanup-incomplete"
        state["cleanup_error"] = str(exc)
        write_state(state)
        return {"status": "error", "message": f"Cleanup is incomplete; recovery state was retained: {exc}"}
    write_state(None)
    return {"status": "complete", "message": "Local Runtime setup cleaned up."}


def main() -> int:
    commands = {"setup": setup, "status": status, "cleanup": cleanup,
                "ensure-host-services": ensure_host_services, "stop-host-services": stop_host_services,
                "helper": serve_helper}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        print("Usage: just {runtime-setup|runtime-status|runtime-cleanup}; internal helper commands are managed by the root justfile", file=sys.stderr)
        return 2
    try:
        commands[sys.argv[1]]()
        return 0
    except (RuntimeError, OSError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("\nInterrupted. Any completed changes remain recorded for recovery.", file=sys.stderr)
            return 130
        print(f"Runtime setup error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
