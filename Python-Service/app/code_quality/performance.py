"""Bounded live HTTP benchmarks and their independent history."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from code_quality.service import _store_path

K6_VERSION = "0.54.0"
K6_SCRIPT_TMPFS_DIR = Path("/dev/shm")
MAX_ROUTES = 20
MAX_VUS = 50
MAX_DURATION_SECONDS = 300
MIN_DURATION_SECONDS = 5
MAX_LOGIN_FIELDS = 12


class PerformanceError(ValueError):
    """Invalid or unavailable benchmark setup with a safe message."""


class PerformanceStore:
    """Persist user target profiles and immutable benchmark results."""

    def __init__(self, path: Path | None = None):
        self.path = path or _store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS performance_targets (
                target_id TEXT PRIMARY KEY, user_key TEXT NOT NULL, origin TEXT NOT NULL,
                config_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(user_key, origin))""")
            db.execute("CREATE INDEX IF NOT EXISTS performance_targets_user ON performance_targets(user_key)")
            db.execute("""CREATE TABLE IF NOT EXISTS performance_runs (
                run_id TEXT PRIMARY KEY, user_key TEXT NOT NULL, project_id TEXT NOT NULL,
                target_id TEXT NOT NULL, created_at TEXT NOT NULL, report_json TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS performance_runs_project ON performance_runs(user_key, project_id, created_at DESC)")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _user_key(user_id: str) -> str:
        return sha256(user_id.encode("utf-8")).hexdigest()

    def targets(self, user_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT target_id,origin,config_json,updated_at FROM performance_targets WHERE user_key=? ORDER BY updated_at DESC", (self._user_key(user_id),)).fetchall()
        return [{"target_id": row["target_id"], "origin": row["origin"], **json.loads(row["config_json"]), "updated_at": row["updated_at"]} for row in rows]

    def save_target(self, user_id: str, payload: Any) -> dict:
        config = validate_target(payload)
        user_key = self._user_key(user_id)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            row = db.execute("SELECT target_id FROM performance_targets WHERE user_key=? AND origin=?", (user_key, config["origin"])).fetchone()
            target_id = row["target_id"] if row else str(uuid4())
            db.execute("INSERT INTO performance_targets(target_id,user_key,origin,config_json,updated_at) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(user_key,origin) DO UPDATE SET config_json=excluded.config_json,updated_at=excluded.updated_at",
                       (target_id, user_key, config["origin"], json.dumps(config, separators=(",", ":")), now))
        return {"target_id": target_id, **config, "updated_at": now}

    def delete_target(self, user_id: str, target_id: str) -> bool:
        with self._connect() as db:
            result = db.execute("DELETE FROM performance_targets WHERE user_key=? AND target_id=?", (self._user_key(user_id), target_id))
        return result.rowcount > 0

    def get_target(self, user_id: str, target_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT target_id,origin,config_json,updated_at FROM performance_targets WHERE user_key=? AND target_id=?", (self._user_key(user_id), target_id)).fetchone()
        return {"target_id": row["target_id"], "origin": row["origin"], **json.loads(row["config_json"]), "updated_at": row["updated_at"]} if row else None

    def save_run(self, user_id: str, project_id: str, target: dict, report: dict) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        result = {"run_id": str(uuid4()), "project_id": project_id, "target_id": target["target_id"],
                  "target_origin": target["origin"], "created_at": now, "routes": report["routes"],
                  "auth_mode": report["auth_mode"], "workload": report["workload"],
                  "runner_version": report["runner_version"], "complete": report["complete"]}
        with self._connect() as db:
            db.execute("INSERT INTO performance_runs(run_id,user_key,project_id,target_id,created_at,report_json) VALUES(?,?,?,?,?,?)",
                       (result["run_id"], self._user_key(user_id), project_id, target["target_id"], now,
                        json.dumps({**result, "route_results": report["route_results"]}, separators=(",", ":"))))
        return result | {"route_results": report["route_results"]}

    def history(self, user_id: str, project_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT report_json FROM performance_runs WHERE user_key=? AND project_id=? ORDER BY created_at DESC LIMIT 200", (self._user_key(user_id), project_id)).fetchall()
        return [json.loads(row["report_json"]) for row in rows]

    def get_run(self, user_id: str, project_id: str, run_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT report_json FROM performance_runs WHERE user_key=? AND project_id=? AND run_id=?", (self._user_key(user_id), project_id, run_id)).fetchone()
        return json.loads(row["report_json"]) if row else None


def validate_origin(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise PerformanceError("Enter a valid HTTP or HTTPS target origin.")
    parsed = urlsplit(value.strip())
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise PerformanceError("Target must be an HTTP or HTTPS origin without credentials, path, query, or fragment.")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise PerformanceError("Target port is invalid.") from exc
    host = parsed.hostname.lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        host = "host.docker.internal"
    if host.endswith((".localhost", ".local", ".internal")) and host != "host.docker.internal":
        raise PerformanceError("Use a public target host or host.docker.internal for a local Docker target.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise PerformanceError("Private and link-local IP targets are not allowed; use host.docker.internal for local Docker.")
    return f"{parsed.scheme.lower()}://{host}{':' + str(parsed.port) if parsed.port else ''}"


def _validate_resolved_target(origin: str) -> str | None:
    host = urlsplit(origin).hostname or ""
    if host == "host.docker.internal":
        return None
    try:
        addresses = socket.getaddrinfo(host, urlsplit(origin).port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise PerformanceError("The target host could not be resolved. Check the origin and retry.") from exc
    if not addresses:
        raise PerformanceError("The target host did not resolve to an address.")
    resolved: list[str] = []
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address[4][0])
        except ValueError as exc:
            raise PerformanceError("The target resolved to an invalid network address.") from exc
        if not parsed_address.is_global:
            raise PerformanceError("The target resolves to a private or link-local address. Use host.docker.internal for local Docker.")
        resolved.append(str(parsed_address))
    return resolved[0]


def validate_route(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2048 or not value.startswith("/") or value.startswith("//"):
        raise PerformanceError("Each selected route must be a relative path beginning with one slash.")
    if any(ch in value for ch in "\r\n\\") or re.search(r"(?i)(?:^|/)\.\.(?:/|$)", value):
        raise PerformanceError("Selected routes cannot contain control characters or parent path segments.")
    return value


def validate_target(value: Any) -> dict:
    if not isinstance(value, dict):
        raise PerformanceError("Target settings are invalid.")
    origin = validate_origin(value.get("origin"))
    try:
        vus = int(value.get("virtual_users", 1))
        duration = int(value.get("duration_seconds", 30))
    except (TypeError, ValueError) as exc:
        raise PerformanceError("Workload values must be whole numbers.") from exc
    if not 1 <= vus <= MAX_VUS:
        raise PerformanceError(f"Virtual users must be between 1 and {MAX_VUS}.")
    if not MIN_DURATION_SECONDS <= duration <= MAX_DURATION_SECONDS:
        raise PerformanceError(f"Duration must be between {MIN_DURATION_SECONDS} and {MAX_DURATION_SECONDS} seconds.")
    auth_mode = value.get("auth_mode", "public")
    if auth_mode not in {"public", "bearer", "http_login"}:
        raise PerformanceError("Choose public, Bearer, or HTTP login authentication.")
    login = value.get("login", {}) if isinstance(value.get("login", {}), dict) else {}
    if auth_mode == "http_login":
        for key in ("login_path", "username_field", "password_field"):
            item = login.get(key)
            if not isinstance(item, str) or not item or len(item) > 512:
                raise PerformanceError("HTTP login requires a login path and username/password field names.")
        login_path = validate_route(login["login_path"])
        csrf_path = validate_route(login["csrf_path"]) if login.get("csrf_path") else None
        login = {"login_path": login_path, "username_field": login["username_field"],
                 "password_field": login["password_field"], "csrf_path": csrf_path,
                 "csrf_cookie_name": str(login.get("csrf_cookie_name", ""))[:128],
                 "csrf_field_name": str(login.get("csrf_field_name", ""))[:128]}
    else:
        login = {}
    return {"origin": origin, "virtual_users": vus, "duration_seconds": duration,
            "auth_mode": auth_mode, "login": login}


def _script(origin: str, routes: list[str], workload: dict, auth: dict,
            pinned_address: str | None = None) -> str:
    route_configs = [{"path": route, "metric": f"route_{index}"} for index, route in enumerate(routes)]
    host = urlsplit(origin).hostname or ""
    config = {"origin": origin, "routes": route_configs, "auth": auth,
              "hosts": {host: pinned_address} if pinned_address else {}}
    return f'''import http from "k6/http";
import {{ check, sleep }} from "k6";
import {{ Counter, Rate, Trend }} from "k6/metrics";
const cfg = {json.dumps(config, separators=(",", ":"))};
const durations = cfg.routes.map((r) => new Trend(r.metric + "_duration", true));
const failures = cfg.routes.map((r) => new Counter(r.metric + "_errors"));
const counts = cfg.routes.map((r) => new Counter(r.metric + "_requests"));
const networkErrors = cfg.routes.map((r) => new Counter(r.metric + "_network_errors"));
const failureRate = cfg.routes.map((r) => new Rate(r.metric + "_failure_rate"));
const loginFailures = new Counter("benchmark_login_failures");
const authFailures = new Counter("benchmark_auth_failures");
let authenticated = false;
export const options = {{ vus: {workload['virtual_users']}, duration: "{workload['duration_seconds']}s",
  discardResponseBodies: true, hosts: cfg.hosts }};
function login() {{
  if (authenticated || cfg.auth.mode === "public") return true;
  const jar = http.cookieJar();
  if (cfg.auth.csrf_path) http.get(cfg.origin + cfg.auth.csrf_path, {{ tags: {{ name: "csrf-bootstrap" }}, redirects: 0 }});
  const payload = {{}};
  payload[cfg.auth.username_field] = cfg.auth.username;
  payload[cfg.auth.password_field] = cfg.auth.password;
  if (cfg.auth.csrf_cookie_name && cfg.auth.csrf_field_name) {{ const cookies = jar.cookiesForURL(cfg.origin); const token = cookies[cfg.auth.csrf_cookie_name]; if (token) payload[cfg.auth.csrf_field_name] = decodeURIComponent(token[0]); }}
  const response = http.post(cfg.origin + cfg.auth.login_path, payload, {{ tags: {{ name: "auth-login" }}, redirects: 0 }});
  authenticated = response.status >= 200 && response.status < 400;
  return authenticated;
}}
export default function () {{
  if (!login()) {{ loginFailures.add(1); return; }}
  for (let i = 0; i < cfg.routes.length; i++) {{
    const route = cfg.routes[i];
    const headers = cfg.auth.mode === "bearer" ? {{ Authorization: "Bearer " + cfg.auth.token }} : undefined;
    const response = http.get(cfg.origin + route.path, {{ headers, tags: {{ name: route.metric }}, redirects: 0 }});
    durations[i].add(response.timings.duration);
    const failed = response.status < 200 || response.status >= 400;
    counts[i].add(1); failures[i].add(failed ? 1 : 0); failureRate[i].add(failed);
    networkErrors[i].add(response.status === 0 ? 1 : 0);
    authFailures.add(response.status === 401 || response.status === 403 ? 1 : 0);
    check(response, {{ ["route " + i + " responds successfully"]: (r) => r.status >= 200 && r.status < 400 }});
  }}
  sleep(0.1);
}}
export function handleSummary(data) {{
  const result = {{ runner_version: "{K6_VERSION}", routes: cfg.routes.map((route) => {{
    const prefix = route.metric;
    const metric = (suffix) => data.metrics[prefix + suffix]?.values || {{}};
    const count = metric("_requests").count || 0;
    return {{ path: route.path, count, throughput_rps: metric("_requests").rate || 0, errors: metric("_errors").count || 0,
      network_errors: metric("_network_errors").count || 0,
      error_rate: metric("_failure_rate").rate || 0, latency_ms: metric("_duration") }};
  }}), login_failures: data.metrics.benchmark_login_failures?.values?.count || 0,
    auth_failures: data.metrics.benchmark_auth_failures?.values?.count || 0 }};
  return {{ stdout: JSON.stringify(result) }};
}}
'''


def run_benchmark(target: dict, routes: Any, bearer_token: str | None = None,
                  login_credentials: dict | None = None) -> dict:
    if not isinstance(routes, list) or not 1 <= len(routes) <= MAX_ROUTES:
        raise PerformanceError(f"Select between 1 and {MAX_ROUTES} routes.")
    selected = list(dict.fromkeys(validate_route(route) for route in routes))
    if len(selected) != len(routes):
        raise PerformanceError("Selected routes must be unique.")
    origin = validate_origin(target.get("origin"))
    pinned_address = _validate_resolved_target(origin)
    auth_mode = target.get("auth_mode", "public")
    auth: dict = {"mode": auth_mode}
    if auth_mode == "bearer":
        if not isinstance(bearer_token, str) or not bearer_token.strip() or len(bearer_token) > 8192:
            raise PerformanceError("The saved Bearer token is unavailable. Save it again and retry.")
        auth["token"] = bearer_token
    elif auth_mode == "http_login":
        login = target.get("login", {})
        if not isinstance(login_credentials, dict) or not isinstance(login_credentials.get("username"), str) or not isinstance(login_credentials.get("password"), str):
            raise PerformanceError("Enter the HTTP login credentials for this benchmark run.")
        if len(login_credentials["username"]) > 512 or len(login_credentials["password"]) > 4096:
            raise PerformanceError("HTTP login credentials exceed the supported size.")
        auth.update(login)
        auth.update(username=login_credentials["username"], password=login_credentials["password"])
    workload = {"virtual_users": int(target["virtual_users"]), "duration_seconds": int(target["duration_seconds"])}
    command = ["k6", "run", "--quiet"]
    script_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".js", prefix="code-quality-k6-",
                                         dir=K6_SCRIPT_TMPFS_DIR, delete=False) as script_file:
            script_file.write(_script(origin, selected, workload, auth, pinned_address))
            script_path = Path(script_file.name)
        os.chmod(script_path, 0o600)
    except OSError as exc:
        if script_path is not None:
            script_path.unlink(missing_ok=True)
        raise PerformanceError("The API service could not prepare an in-memory benchmark script. Check its temporary filesystem and retry.") from exc
    try:
        completed = subprocess.run([*command, str(script_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=workload["duration_seconds"] + 60, check=False)
    except subprocess.TimeoutExpired as exc:
        raise PerformanceError("The benchmark runner timed out. Reduce the workload or check the API service and target, then retry.") from exc
    except FileNotFoundError as exc:
        raise PerformanceError("The pinned k6 runner is unavailable in the API service. Rebuild python-api and retry.") from exc
    except OSError as exc:
        raise PerformanceError("The k6 runner could not start. Rebuild python-api or check the target network, then retry.") from exc
    finally:
        script_path.unlink(missing_ok=True)
    if completed.returncode:
        raise PerformanceError("The benchmark could not reach the target or the k6 runner failed. Check the target origin and authentication, then retry.")
    try:
        output = json.loads(completed.stdout.decode("utf-8").strip())
        measured = output.get("routes")
        if output.get("login_failures", 0):
            raise PerformanceError("HTTP login or CSRF bootstrap failed for a virtual user. Check the login path, field names, credentials, and CSRF settings.")
        if output.get("auth_failures", 0):
            raise PerformanceError("The target rejected one or more selected route requests. Check the authentication mode, saved token, or route access.")
        if not isinstance(measured, list) or len(measured) != len(selected):
            raise ValueError
    except PerformanceError:
        raise
    except (UnicodeError, ValueError, TypeError, AttributeError) as exc:
        raise PerformanceError("The benchmark runner returned invalid summary data. Retry the run.") from exc
    if not any(isinstance(item, dict) and item.get("count", 0) for item in measured):
        raise PerformanceError("The selected routes produced no measurements. Check target reachability and retry.")
    all_network_failed = all(
        isinstance(item, dict) and int(item.get("count", 0) or 0) <= int(item.get("network_errors", 0) or 0)
        for item in measured
    )
    if all_network_failed:
        raise PerformanceError("The configured target could not be reached from the API service. Check the origin and target network, then retry.")
    complete = all(
        isinstance(item, dict)
        and int(item.get("count", 0) or 0) > 0
        and int(item.get("network_errors", 0) or 0) == 0
        for item in measured
    )
    return {"routes": selected, "auth_mode": auth_mode, "workload": workload,
            "runner_version": output.get("runner_version", K6_VERSION),
            "complete": complete, "route_results": measured}


def compare_runs(left: dict, right: dict) -> dict:
    keys = ("target_origin", "routes", "auth_mode", "workload", "runner_version", "complete")
    differences = [key for key in keys if left.get(key) != right.get(key)]
    return {"equivalent": not differences, "differences": differences}
