"""Bounded live HTTP benchmarks, run in the background, with revision-aware history.

Each run benchmarks explicitly selected GET paths of one project-scoped target with the pinned k6
runner. Every route gets its own k6 scenario so a slow route does not throttle the others. Runs
execute in a background thread (at most one per user and two in total); the browser polls their
progress and can cancel them. Secrets (Bearer tokens, login credentials) live only in the k6
script on tmpfs for the duration of a run and never reach the saved report.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request as UrlRequest, urlopen
from uuid import UUID, uuid4

from code_quality.service import _store_path

K6_VERSION = "0.54.0"
K6_SCRIPT_TMPFS_DIR = Path("/dev/shm")
K6_SCRIPT_PREFIX = "code-quality-k6-"
K6_API_PORTS = (6565, 6566)
MAX_ROUTES = 20
MAX_VUS = 50
MAX_TOTAL_VUS = 100
MAX_DURATION_SECONDS = 300
MIN_DURATION_SECONDS = 5
MAX_STAGES = 10
MAX_RATE_PER_SECOND = 500
MAX_THINK_TIME_MS = 2000
MAX_WARMUP_SECONDS = 60
MAX_ACTIVE_RUNS_PER_USER = 1
MAX_ACTIVE_RUNS = 2
GRACEFUL_STOP_SECONDS = 30
REGRESSION_P95_RATIO = 1.10
EXECUTORS = ("constant_vus", "ramping_vus", "constant_arrival_rate")
CSRF_MODES = ("none", "header", "field")
LOCAL_DOCKER_HOST = "host.docker.internal"
# Ports of this application's own services on the Docker host: the credential helper, analyzer
# worker / telemetry ingress, MinIO, Qdrant, Neo4j, the collector, Keycloak and Outline.
DEFAULT_BLOCKED_HOST_PORTS = frozenset({47831, 9443, 1080, 9000, 9001, 6333, 6334, 7474, 7687,
                                        24318, 8180, 18081, 3020, 9002, 8123})
_ROUTE_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\??\}")
_FIELD_NAME = re.compile(r"^[A-Za-z0-9_.\[\]-]{1,128}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
logger = logging.getLogger(__name__)


class PerformanceError(ValueError):
    """Invalid or unavailable benchmark setup with a safe message."""


class PerformanceBusy(PerformanceError):
    """Another benchmark is already running for this user or the service."""

    def __init__(self, message: str, active_run_id: str | None = None):
        super().__init__(message)
        self.active_run_id = active_run_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _deployment_mode() -> str:
    from code_quality.external_analyzers import deployment_mode
    return deployment_mode()


def _blocked_host_ports() -> frozenset[int]:
    extra = os.environ.get("PERFORMANCE_BLOCKED_HOST_PORTS", "")
    ports = {int(value) for value in re.findall(r"\d{1,5}", extra)}
    return DEFAULT_BLOCKED_HOST_PORTS | ports


# ----- validation ---------------------------------------------------------------------------------

def validate_origin(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise PerformanceError("Enter a valid HTTP or HTTPS target origin.")
    parsed = urlsplit(value.strip())
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise PerformanceError("Target must be an HTTP or HTTPS origin without credentials, path, query, or fragment.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise PerformanceError("Target port is invalid.") from exc
    host = parsed.hostname.lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        host = LOCAL_DOCKER_HOST
    if host == LOCAL_DOCKER_HOST:
        if _deployment_mode() != "developer":
            raise PerformanceError("Local Docker targets are only available in Developer Mode. Use a public HTTPS target.")
        effective_port = port or (443 if parsed.scheme == "https" else 80)
        if effective_port in _blocked_host_ports():
            raise PerformanceError(f"Port {effective_port} belongs to a Developer Assistant service. Use the port your application publishes.")
    elif host.endswith((".localhost", ".local", ".internal")):
        raise PerformanceError("Use a public target host or host.docker.internal for a local Docker target.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise PerformanceError("Private and link-local IP targets are not allowed; use host.docker.internal for local Docker.")
    return f"{parsed.scheme.lower()}://{host}{':' + str(port) if port else ''}"


def is_local_origin(origin: str) -> bool:
    return urlsplit(origin).hostname == LOCAL_DOCKER_HOST


def _validate_resolved_target(origin: str) -> str | None:
    host = urlsplit(origin).hostname or ""
    if host == LOCAL_DOCKER_HOST:
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
    if any(ch in value for ch in "\r\n\\") or any(ord(ch) < 32 for ch in value) or re.search(r"(?i)(?:^|/)\.\.(?:/|$)", value):
        raise PerformanceError("Selected routes cannot contain control characters or parent path segments.")
    if "{" in value or "}" in value:
        raise PerformanceError(f"Fill in the route parameters of {value[:120]} before running it.")
    return value


def route_parameters(template: str) -> list[str]:
    return list(dict.fromkeys(_ROUTE_PARAM.findall(template)))


def fill_route(template: str, values: dict[str, Any]) -> str:
    """Replace {param} segments of a route template with URL-safe values."""
    def substitute(match: re.Match) -> str:
        value = values.get(match.group(1))
        if not isinstance(value, (str, int)) or str(value) == "":
            if match.group(0).endswith("?}"):
                return ""
            raise PerformanceError(f"Enter a value for {{{match.group(1)}}} in {template[:120]}.")
        text = str(value)
        if len(text) > 200 or not re.fullmatch(r"[A-Za-z0-9._~@:!$&'()*+,;=%-]+", text):
            raise PerformanceError(f"The value for {{{match.group(1)}}} must be one URL path segment.")
        return text
    filled = _ROUTE_PARAM.sub(substitute, template)
    filled = re.sub(r"/{2,}", "/", filled)
    if len(filled) > 1:
        filled = filled.rstrip("/") or "/"
    return validate_route(filled)


def _whole(value: Any, name: str, low: int, high: int, default: int) -> int:
    try:
        number = int(default if value is None or value == "" else value)
    except (TypeError, ValueError) as exc:
        raise PerformanceError(f"{name} must be a whole number.") from exc
    if not low <= number <= high:
        raise PerformanceError(f"{name} must be between {low} and {high}.")
    return number


def validate_workload(value: dict) -> dict:
    executor = value.get("executor") or "constant_vus"
    if executor not in EXECUTORS:
        raise PerformanceError("Choose constant virtual users, ramping virtual users or constant arrival rate.")
    workload = {"executor": executor,
                "virtual_users": _whole(value.get("virtual_users"), "Virtual users", 1, MAX_VUS, 1),
                "duration_seconds": _whole(value.get("duration_seconds"), "Duration (seconds)",
                                           MIN_DURATION_SECONDS, MAX_DURATION_SECONDS, 30),
                "think_time_ms": _whole(value.get("think_time_ms"), "Think time (ms)", 0, MAX_THINK_TIME_MS, 100),
                "warmup_seconds": _whole(value.get("warmup_seconds"), "Warm-up (seconds)", 0, MAX_WARMUP_SECONDS, 0)}
    if executor == "constant_arrival_rate":
        workload["rate_per_second"] = _whole(value.get("rate_per_second"), "Requests per second", 1, MAX_RATE_PER_SECOND, 10)
        workload["max_virtual_users"] = _whole(value.get("max_virtual_users"), "Maximum virtual users", 1, MAX_VUS, 20)
    if executor == "ramping_vus":
        stages = value.get("stages")
        if not isinstance(stages, list) or not 1 <= len(stages) <= MAX_STAGES:
            raise PerformanceError(f"Ramping needs 1 to {MAX_STAGES} stages.")
        clean = []
        for stage in stages:
            if not isinstance(stage, dict):
                raise PerformanceError("Each ramping stage needs a duration and a target.")
            clean.append({"duration_seconds": _whole(stage.get("duration_seconds"), "Stage duration (seconds)", 1, MAX_DURATION_SECONDS, 10),
                          "target": _whole(stage.get("target"), "Stage target virtual users", 0, MAX_VUS, 1)})
        total = sum(stage["duration_seconds"] for stage in clean)
        if not MIN_DURATION_SECONDS <= total <= MAX_DURATION_SECONDS:
            raise PerformanceError(f"Ramping stages must add up to {MIN_DURATION_SECONDS}-{MAX_DURATION_SECONDS} seconds.")
        workload["stages"] = clean
        workload["duration_seconds"] = total
        workload["virtual_users"] = max(1, max(stage["target"] for stage in clean))
    return workload


def peak_virtual_users(workload: dict) -> int:
    if workload["executor"] == "constant_arrival_rate":
        return workload["max_virtual_users"]
    return workload["virtual_users"]


def validate_thresholds(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    if value.get("p95_ms") not in (None, ""):
        result["p95_ms"] = _whole(value.get("p95_ms"), "p95 budget (ms)", 1, 600_000, 1)
    if value.get("error_rate") not in (None, ""):
        try:
            rate = float(value["error_rate"])
        except (TypeError, ValueError) as exc:
            raise PerformanceError("Error-rate budget must be a number between 0 and 1.") from exc
        if not 0 <= rate <= 1:
            raise PerformanceError("Error-rate budget must be a number between 0 and 1.")
        result["error_rate"] = rate
    return result


def _validate_login(value: Any) -> dict:
    login = value if isinstance(value, dict) else {}
    for key in ("login_path", "username_field", "password_field"):
        item = login.get(key)
        if not isinstance(item, str) or not item or len(item) > 512:
            raise PerformanceError("HTTP login requires a login path and username/password field names.")
    for key in ("username_field", "password_field"):
        if not _FIELD_NAME.fullmatch(login[key]):
            raise PerformanceError("Login field names may only use letters, digits, '_', '-', '.', and brackets.")
    csrf_mode = login.get("csrf_mode")
    if csrf_mode is None:  # settings saved before CSRF modes existed
        csrf_mode = "field" if login.get("csrf_cookie_name") and login.get("csrf_field_name") else "none"
    if csrf_mode not in CSRF_MODES:
        raise PerformanceError("Choose a CSRF mode: none, header, or form field.")
    clean = {"login_path": validate_route(login["login_path"]), "username_field": login["username_field"],
             "password_field": login["password_field"], "csrf_mode": csrf_mode,
             "csrf_path": validate_route(login["csrf_path"]) if login.get("csrf_path") else None,
             "csrf_cookie_name": "", "csrf_field_name": "", "csrf_header_name": "",
             "check_path": validate_route(login["check_path"]) if login.get("check_path") else None}
    if csrf_mode != "none":
        cookie = str(login.get("csrf_cookie_name") or "")
        if not _FIELD_NAME.fullmatch(cookie):
            raise PerformanceError("Enter the CSRF cookie name, for example XSRF-TOKEN.")
        clean["csrf_cookie_name"] = cookie
    if csrf_mode == "field":
        field = str(login.get("csrf_field_name") or "")
        if not _FIELD_NAME.fullmatch(field):
            raise PerformanceError("Enter the CSRF form field name, for example _token.")
        clean["csrf_field_name"] = field
    if csrf_mode == "header":
        header = str(login.get("csrf_header_name") or "X-XSRF-TOKEN")
        if not _HEADER_NAME.fullmatch(header):
            raise PerformanceError("The CSRF header name may only use letters, digits, and '-'.")
        clean["csrf_header_name"] = header
    return clean


def validate_target(value: Any) -> dict:
    if not isinstance(value, dict):
        raise PerformanceError("Target settings are invalid.")
    origin = validate_origin(value.get("origin"))
    name = str(value.get("name") or "").strip()[:80] or origin
    auth_mode = value.get("auth_mode", "public")
    if auth_mode not in {"public", "bearer", "http_login"}:
        raise PerformanceError("Choose public, Bearer, or HTTP login authentication.")
    if auth_mode != "public" and origin.startswith("http://") and not is_local_origin(origin):
        raise PerformanceError("Use an HTTPS origin when the target needs a token or login; credentials must not travel in clear text.")
    login = _validate_login(value.get("login")) if auth_mode == "http_login" else {}
    params = value.get("route_params") if isinstance(value.get("route_params"), dict) else {}
    route_params: dict[str, dict[str, str]] = {}
    for template, values in list(params.items())[:100]:
        if isinstance(template, str) and template.startswith("/") and len(template) <= 2048 and isinstance(values, dict):
            route_params[template] = {str(k)[:64]: str(v)[:200] for k, v in list(values.items())[:20]
                                      if isinstance(v, (str, int)) and str(v) != ""}
    return {"name": name, "origin": origin, **validate_workload(value), "auth_mode": auth_mode, "login": login,
            "thresholds": validate_thresholds(value.get("thresholds")), "route_params": route_params}


# ----- store --------------------------------------------------------------------------------------

class PerformanceStore:
    """Project-scoped target profiles, background run state, and immutable run reports."""

    def __init__(self, path: Path | None = None):
        self.path = path or _store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            table = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='performance_targets'").fetchone()
            if table and "project_id" not in table["sql"]:
                # The first version allowed one target per user and origin with no project scope.
                db.execute("ALTER TABLE performance_targets RENAME TO performance_targets_v1")
            db.execute("""CREATE TABLE IF NOT EXISTS performance_targets (
                target_id TEXT PRIMARY KEY, user_key TEXT NOT NULL, project_id TEXT NOT NULL, name TEXT NOT NULL,
                origin TEXT NOT NULL, config_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(user_key, project_id, name))""")
            if table and "project_id" not in table["sql"]:
                db.execute("""INSERT OR IGNORE INTO performance_targets
                    (target_id, user_key, project_id, name, origin, config_json, updated_at)
                    SELECT target_id, user_key, '', origin, origin, config_json, updated_at FROM performance_targets_v1""")
                db.execute("DROP TABLE performance_targets_v1")
            db.execute("CREATE INDEX IF NOT EXISTS performance_targets_scope ON performance_targets(user_key, project_id)")
            db.execute("""CREATE TABLE IF NOT EXISTS performance_runs (
                run_id TEXT PRIMARY KEY, user_key TEXT NOT NULL, project_id TEXT NOT NULL,
                target_id TEXT NOT NULL, created_at TEXT NOT NULL, report_json TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS performance_runs_project ON performance_runs(user_key, project_id, created_at DESC)")
            db.execute("""CREATE TABLE IF NOT EXISTS performance_jobs (
                run_id TEXT PRIMARY KEY, user_key TEXT NOT NULL, project_id TEXT NOT NULL, target_id TEXT NOT NULL,
                state TEXT NOT NULL, port INTEGER, started_at TEXT NOT NULL, expected_seconds INTEGER NOT NULL,
                finished_at TEXT, error TEXT, summary_json TEXT NOT NULL)""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _user_key(user_id: str) -> str:
        return sha256(user_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _target(row: sqlite3.Row) -> dict:
        config = json.loads(row["config_json"])
        config.setdefault("executor", "constant_vus")
        config.setdefault("think_time_ms", 100)
        config.setdefault("warmup_seconds", 0)
        config.setdefault("thresholds", {})
        config.setdefault("route_params", {})
        return {**config, "target_id": row["target_id"], "project_id": row["project_id"], "name": row["name"],
                "origin": row["origin"], "updated_at": row["updated_at"], "unassigned": row["project_id"] == ""}

    # targets
    def targets(self, user_id: str, project_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM performance_targets WHERE user_key=? AND project_id IN (?, '') "
                              "ORDER BY project_id='' , name COLLATE NOCASE", (self._user_key(user_id), project_id)).fetchall()
        return [self._target(row) for row in rows]

    def get_target(self, user_id: str, project_id: str, target_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM performance_targets WHERE user_key=? AND target_id=? AND project_id IN (?, '')",
                             (self._user_key(user_id), target_id, project_id)).fetchone()
        return self._target(row) if row else None

    def save_target(self, user_id: str, project_id: str, payload: Any) -> dict:
        config = validate_target(payload)
        user_key = self._user_key(user_id)
        target_id = payload.get("target_id") if isinstance(payload, dict) else None
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if target_id:
                existing = db.execute("SELECT * FROM performance_targets WHERE user_key=? AND target_id=? AND project_id IN (?, '')",
                                      (user_key, str(target_id), project_id)).fetchone()
                if existing is None:
                    raise PerformanceError("The performance target was not found. Reload and retry.")
            clash = db.execute("SELECT target_id FROM performance_targets WHERE user_key=? AND project_id=? AND name=?",
                               (user_key, project_id, config["name"])).fetchone()
            if clash and clash["target_id"] != target_id:
                raise PerformanceError(f"A target named {config['name']!r} already exists in this project. Choose another name.")
            target_id = str(target_id or uuid4())
            stored = {key: value for key, value in config.items() if key not in {"name", "origin"}}
            db.execute("""INSERT INTO performance_targets (target_id,user_key,project_id,name,origin,config_json,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(target_id) DO UPDATE SET project_id=excluded.project_id, name=excluded.name,
                origin=excluded.origin, config_json=excluded.config_json, updated_at=excluded.updated_at""",
                       (target_id, user_key, project_id, config["name"], config["origin"],
                        json.dumps(stored, separators=(",", ":")), now))
            row = db.execute("SELECT * FROM performance_targets WHERE target_id=?", (target_id,)).fetchone()
        return self._target(row)

    def delete_target(self, user_id: str, project_id: str, target_id: str) -> bool:
        with self._connect() as db:
            result = db.execute("DELETE FROM performance_targets WHERE user_key=? AND target_id=? AND project_id IN (?, '')",
                                (self._user_key(user_id), target_id, project_id))
        return result.rowcount > 0

    # background runs
    def claim_run(self, user_id: str, project_id: str, target_id: str, expected_seconds: int, summary: dict) -> tuple[str, int]:
        """Register a running benchmark, enforcing the per-user and global limits."""
        user_key = self._user_key(user_id)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT run_id, user_key, port FROM performance_jobs WHERE state='running'").fetchall()
            mine = [row for row in active if row["user_key"] == user_key]
            if len(mine) >= MAX_ACTIVE_RUNS_PER_USER:
                raise PerformanceBusy("You already have a benchmark running. Wait for it or cancel it first.", mine[0]["run_id"])
            if len(active) >= MAX_ACTIVE_RUNS:
                raise PerformanceBusy("The maximum number of concurrent benchmarks is running. Retry when one finishes.")
            used = {row["port"] for row in active}
            port = next(port for port in K6_API_PORTS if port not in used)
            run_id = str(uuid4())
            db.execute("""INSERT INTO performance_jobs (run_id,user_key,project_id,target_id,state,port,started_at,expected_seconds,summary_json)
                VALUES(?,?,?,?,?,?,?,?,?)""", (run_id, user_key, project_id, target_id, "running", port, _now(),
                                               expected_seconds, json.dumps(summary, separators=(",", ":"))))
        return run_id, port

    def finish_job(self, run_id: str, state: str, error: str | None = None) -> None:
        with self._connect() as db:
            db.execute("UPDATE performance_jobs SET state=?, finished_at=?, error=? WHERE run_id=?",
                       (state, _now(), (error or None) and error[:500], run_id))

    def job(self, user_id: str, project_id: str, run_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM performance_jobs WHERE user_key=? AND project_id=? AND run_id=?",
                             (self._user_key(user_id), project_id, run_id)).fetchone()
        return self._job(row) if row else None

    def jobs(self, user_id: str, project_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM performance_jobs WHERE user_key=? AND project_id=? AND state IN ('running','failed') "
                              "AND run_id NOT IN (SELECT run_id FROM performance_runs) ORDER BY started_at DESC LIMIT 20",
                              (self._user_key(user_id), project_id)).fetchall()
        return [self._job(row) for row in rows]

    @staticmethod
    def _job(row: sqlite3.Row) -> dict:
        return {"run_id": row["run_id"], "project_id": row["project_id"], "target_id": row["target_id"],
                "state": row["state"], "port": row["port"], "started_at": row["started_at"],
                "expected_seconds": row["expected_seconds"], "finished_at": row["finished_at"], "error": row["error"],
                **json.loads(row["summary_json"])}

    def recover_interrupted(self) -> int:
        with self._connect() as db:
            result = db.execute("UPDATE performance_jobs SET state='failed', finished_at=?, "
                                "error='The API restarted during this benchmark. Start it again.' WHERE state='running'", (_now(),))
        return result.rowcount

    # reports
    def save_run(self, user_id: str, project_id: str, report: dict) -> dict:
        with self._connect() as db:
            db.execute("INSERT INTO performance_runs(run_id,user_key,project_id,target_id,created_at,report_json) VALUES(?,?,?,?,?,?)",
                       (report["run_id"], self._user_key(user_id), project_id, report["target_id"], report["created_at"],
                        json.dumps(report, separators=(",", ":"))))
        return report

    def history(self, user_id: str, project_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT report_json FROM performance_runs WHERE user_key=? AND project_id=? ORDER BY created_at DESC LIMIT 200",
                              (self._user_key(user_id), project_id)).fetchall()
        return [_with_defaults(json.loads(row["report_json"])) for row in rows]

    def get_run(self, user_id: str, project_id: str, run_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT report_json FROM performance_runs WHERE user_key=? AND project_id=? AND run_id=?",
                             (self._user_key(user_id), project_id, run_id)).fetchone()
        return _with_defaults(json.loads(row["report_json"])) if row else None

    def delete_run(self, user_id: str, project_id: str, run_id: str) -> bool:
        with self._connect() as db:
            result = db.execute("DELETE FROM performance_runs WHERE user_key=? AND project_id=? AND run_id=?",
                                (self._user_key(user_id), project_id, run_id))
            db.execute("DELETE FROM performance_jobs WHERE user_key=? AND project_id=? AND run_id=? AND state!='running'",
                       (self._user_key(user_id), project_id, run_id))
        return result.rowcount > 0


def _with_defaults(report: dict) -> dict:
    report.setdefault("state", "completed")
    report.setdefault("revision_id", None)
    report.setdefault("commit_sha", None)
    report.setdefault("target_name", report.get("target_origin"))
    report.setdefault("cancelled", False)
    report.setdefault("partial_reasons", [] if report.get("complete", True) else ["Some requests failed at the network level."])
    report.setdefault("thresholds", {})
    report.setdefault("baseline", False)
    return report


# ----- k6 script ----------------------------------------------------------------------------------

def _scenario(index: int, workload: dict, exec_name: str, start_seconds: int) -> dict:
    base = {"exec": exec_name, "startTime": f"{start_seconds}s", "gracefulStop": f"{GRACEFUL_STOP_SECONDS}s"}
    if workload["executor"] == "constant_arrival_rate":
        return {**base, "executor": "constant-arrival-rate", "rate": workload["rate_per_second"], "timeUnit": "1s",
                "duration": f"{workload['duration_seconds']}s",
                "preAllocatedVUs": min(workload["max_virtual_users"], workload["rate_per_second"]),
                "maxVUs": workload["max_virtual_users"]}
    if workload["executor"] == "ramping_vus":
        return {**base, "executor": "ramping-vus", "startVUs": 0,
                "stages": [{"duration": f"{stage['duration_seconds']}s", "target": stage["target"]} for stage in workload["stages"]]}
    return {**base, "executor": "constant-vus", "vus": workload["virtual_users"], "duration": f"{workload['duration_seconds']}s"}


def build_script(origin: str, routes: list[str], workload: dict, auth: dict, *, thresholds: dict | None = None,
                 pinned_address: str | None = None, run_id: str = "", export: bool = False) -> str:
    """Generate the k6 script: one recorded scenario per route, an optional unrecorded warm-up first.

    With export=True secrets become __ENV placeholders and DNS pinning is dropped, so the developer can
    re-run the same benchmark in their own environment.
    """
    host = urlsplit(origin).hostname or ""
    thresholds = thresholds or {}
    scenarios: dict[str, dict] = {}
    warm = workload.get("warmup_seconds", 0)
    for index in range(len(routes)):
        if warm:
            scenarios[f"warmup_{index}"] = {"executor": "constant-vus", "vus": 1 if workload["executor"] == "constant_arrival_rate"
                                            else workload["virtual_users"], "duration": f"{warm}s", "exec": f"warm_{index}",
                                            "gracefulStop": "5s"}
        scenarios[f"route_{index}"] = _scenario(index, workload, f"route_{index}", warm)
    k6_thresholds: dict[str, list[str]] = {}
    for index in range(len(routes)):
        if "p95_ms" in thresholds:
            k6_thresholds[f"route_{index}_duration"] = [f"p(95)<{thresholds['p95_ms']}"]
        if "error_rate" in thresholds:
            k6_thresholds[f"route_{index}_failure_rate"] = [f"rate<={thresholds['error_rate']}"]
    script_auth = dict(auth)
    if export:
        if script_auth.get("mode") == "bearer":
            script_auth["token"] = "__ENV_TOKEN__"
        if script_auth.get("mode") == "http_login":
            script_auth["username"] = "__ENV_USERNAME__"
            script_auth["password"] = "__ENV_PASSWORD__"
    config = {"origin": origin, "routes": [{"path": route, "metric": f"route_{index}"} for index, route in enumerate(routes)],
              "auth": script_auth, "think": workload.get("think_time_ms", 0) / 1000, "run_id": run_id,
              "hosts": {host: pinned_address} if pinned_address and not export else {}}
    options = {"scenarios": scenarios, "thresholds": k6_thresholds, "discardResponseBodies": True, "hosts": config["hosts"],
               "summaryTrendStats": ["avg", "min", "med", "max", "p(90)", "p(95)", "p(99)"]}
    cfg_json = json.dumps(config, separators=(",", ":"))
    if export:
        cfg_json = (cfg_json.replace('"__ENV_TOKEN__"', "__ENV.BENCHMARK_TOKEN")
                    .replace('"__ENV_USERNAME__"', "__ENV.BENCHMARK_USERNAME")
                    .replace('"__ENV_PASSWORD__"', "__ENV.BENCHMARK_PASSWORD"))
    route_functions = "\n".join(
        f"export function route_{index}() {{ hit({index}, true); }}\nexport function warm_{index}() {{ hit({index}, false); }}"
        for index in range(len(routes)))
    return f'''import http from "k6/http";
import {{ sleep }} from "k6";
import {{ Counter, Rate, Trend }} from "k6/metrics";
// Generated by Developer Assistant (k6 {K6_VERSION}).{" Set BENCHMARK_TOKEN / BENCHMARK_USERNAME / BENCHMARK_PASSWORD as needed." if export else ""}
const cfg = {cfg_json};
export const options = {json.dumps(options, separators=(",", ":"))};
const m = cfg.routes.map((r) => ({{
  duration: new Trend(r.metric + "_duration", true), requests: new Counter(r.metric + "_requests"),
  failure: new Rate(r.metric + "_failure_rate"), network: new Counter(r.metric + "_network_errors"),
  s2xx: new Counter(r.metric + "_2xx"), s3xx: new Counter(r.metric + "_3xx"),
  s4xx: new Counter(r.metric + "_4xx"), s5xx: new Counter(r.metric + "_5xx"), auth: new Counter(r.metric + "_auth_rejected"),
}}));
const loginFailures = new Counter("benchmark_login_failures");
let authenticated = false;
function baseHeaders() {{
  const headers = {{}};
  if (cfg.run_id) headers["X-DevAssistant-Benchmark"] = cfg.run_id;
  if (cfg.auth.mode === "bearer") headers["Authorization"] = "Bearer " + cfg.auth.token;
  return headers;
}}
function pathOf(location) {{
  const path = location.replace(/^[a-z][a-z0-9+.-]*:\\/\\/[^/]+/i, "").split("?")[0].split("#")[0];
  return path.length > 1 ? path.replace(/\\/+$/, "") : (path || "/");
}}
function cookieValue(name) {{
  const values = http.cookieJar().cookiesForURL(cfg.origin)[name];
  return values && values.length ? decodeURIComponent(values[0]) : "";
}}
function login() {{
  if (authenticated || cfg.auth.mode !== "http_login") return true;
  const a = cfg.auth;
  if (a.csrf_path) http.get(cfg.origin + a.csrf_path, {{ headers: baseHeaders(), redirects: 0, tags: {{ name: "csrf-bootstrap" }} }});
  const payload = {{}};
  payload[a.username_field] = a.username;
  payload[a.password_field] = a.password;
  // A failed form login redirects back to the referring page; naming the login page makes that detectable.
  const headers = Object.assign(baseHeaders(), {{ Referer: cfg.origin + a.login_path, Accept: "text/html,application/json" }});
  if (a.csrf_mode === "field") {{ const token = cookieValue(a.csrf_cookie_name); if (token) payload[a.csrf_field_name] = token; }}
  if (a.csrf_mode === "header") {{ const token = cookieValue(a.csrf_cookie_name); if (token) headers[a.csrf_header_name] = token; }}
  const response = http.post(cfg.origin + a.login_path, payload, {{ headers, redirects: 0, tags: {{ name: "auth-login" }} }});
  let ok = response.status >= 200 && response.status < 300;
  if (response.status >= 300 && response.status < 400) {{
    const location = response.headers["Location"] || response.headers["location"] || "";
    ok = location !== "" && pathOf(location) !== pathOf(a.login_path);
  }}
  if (ok && a.check_path) {{
    const check = http.get(cfg.origin + a.check_path, {{ headers: baseHeaders(), redirects: 0, tags: {{ name: "auth-check" }} }});
    ok = check.status >= 200 && check.status < 300;
  }}
  authenticated = ok;
  return ok;
}}
function hit(i, record) {{
  if (!login()) {{ if (record) loginFailures.add(1); sleep(1); return; }}
  const response = http.get(cfg.origin + cfg.routes[i].path, {{ headers: baseHeaders(), redirects: 0, tags: {{ name: cfg.routes[i].metric }} }});
  if (record) {{
    const s = response.status, r = m[i];
    r.requests.add(1); r.duration.add(response.timings.duration);
    r.failure.add(!(s >= 200 && s < 300));
    if (s === 0) r.network.add(1); else if (s < 300) r.s2xx.add(1); else if (s < 400) r.s3xx.add(1); else if (s < 500) r.s4xx.add(1); else r.s5xx.add(1);
    if (s === 401 || s === 403) r.auth.add(1);
  }}
  if (cfg.think > 0) sleep(cfg.think);
}}
{route_functions}
export function handleSummary(data) {{
  const value = (name) => (data.metrics[name] && data.metrics[name].values) || {{}};
  const passed = (name) => {{
    const t = data.metrics[name] && data.metrics[name].thresholds;
    return t ? Object.values(t).every((item) => item.ok) : null;
  }};
  const routes = cfg.routes.map((route) => {{
    const p = route.metric;
    return {{ path: route.path, count: value(p + "_requests").count || 0, throughput_rps: value(p + "_requests").rate || 0,
      errors: Math.round((value(p + "_failure_rate").passes || 0)), error_rate: value(p + "_failure_rate").rate || 0,
      network_errors: value(p + "_network_errors").count || 0, auth_rejected: value(p + "_auth_rejected").count || 0,
      statuses: {{ "2xx": value(p + "_2xx").count || 0, "3xx": value(p + "_3xx").count || 0, "4xx": value(p + "_4xx").count || 0, "5xx": value(p + "_5xx").count || 0 }},
      latency_ms: value(p + "_duration"), thresholds: {{ latency: passed(p + "_duration"), errors: passed(p + "_failure_rate") }} }};
  }});
  return {{ stdout: JSON.stringify({{ runner_version: "{K6_VERSION}", routes, login_failures: value("benchmark_login_failures").count || 0 }}) }};
}}
'''


# ----- running ------------------------------------------------------------------------------------

def _auth_config(target: dict, bearer_token: str | None, login_credentials: dict | None) -> dict:
    auth_mode = target.get("auth_mode", "public")
    auth: dict = {"mode": auth_mode}
    if auth_mode == "bearer":
        if not isinstance(bearer_token, str) or not bearer_token.strip() or len(bearer_token) > 8192:
            raise PerformanceError("The saved Bearer token is unavailable. Save it again and retry.")
        auth["token"] = bearer_token
    elif auth_mode == "http_login":
        if (not isinstance(login_credentials, dict) or not isinstance(login_credentials.get("username"), str)
                or not isinstance(login_credentials.get("password"), str)
                or not login_credentials["username"] or not login_credentials["password"]):
            raise PerformanceError("Enter the HTTP login credentials for this benchmark run.")
        if len(login_credentials["username"]) > 512 or len(login_credentials["password"]) > 4096:
            raise PerformanceError("HTTP login credentials exceed the supported size.")
        auth.update(_validate_login(target.get("login")))
        auth.update(username=login_credentials["username"], password=login_credentials["password"])
    return auth


def prepare_run(target: dict, selections: Any) -> tuple[list[dict], dict]:
    """Resolve route selections ({template, params} or a path) into concrete, validated paths."""
    if not isinstance(selections, list) or not 1 <= len(selections) <= MAX_ROUTES:
        raise PerformanceError(f"Select between 1 and {MAX_ROUTES} routes.")
    routes = []
    for item in selections:
        if isinstance(item, str):
            item = {"template": item, "params": {}}
        if not isinstance(item, dict) or not isinstance(item.get("template"), str):
            raise PerformanceError("Each selected route needs a path.")
        template = item["template"].strip()
        if not template.startswith("/") or len(template) > 2048:
            raise PerformanceError("Each selected route must be a relative path beginning with one slash.")
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        routes.append({"template": template, "path": fill_route(template, params)})
    paths = [route["path"] for route in routes]
    if len(set(paths)) != len(paths):
        raise PerformanceError("Selected routes must be unique after their parameters are filled in.")
    workload = {key: target[key] for key in ("executor", "virtual_users", "duration_seconds", "think_time_ms", "warmup_seconds",
                                             "rate_per_second", "max_virtual_users", "stages") if key in target}
    workload = validate_workload(workload)
    if peak_virtual_users(workload) * len(routes) > MAX_TOTAL_VUS:
        raise PerformanceError(f"Virtual users × routes may not exceed {MAX_TOTAL_VUS}. Lower the virtual users or select fewer routes.")
    return routes, workload


def expected_seconds(workload: dict) -> int:
    return workload.get("warmup_seconds", 0) + workload["duration_seconds"] + GRACEFUL_STOP_SECONDS


def estimated_requests(workload: dict, route_count: int) -> int | None:
    if workload["executor"] == "constant_arrival_rate":
        return workload["rate_per_second"] * workload["duration_seconds"] * route_count
    return None


def _k6_api(port: int, path: str, payload: dict | None = None, method: str = "GET") -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = UrlRequest(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                         headers={"Content-Type": "application/json"} if data else {})
    with urlopen(request, timeout=2) as response:
        return json.loads(response.read() or b"{}")


def live_progress(job: dict) -> dict:
    started = datetime.fromisoformat(job["started_at"])
    elapsed = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
    progress = {"elapsed_seconds": round(elapsed), "expected_seconds": job["expected_seconds"],
                "percent": min(99, round(100 * elapsed / max(1, job["expected_seconds"] - GRACEFUL_STOP_SECONDS)))}
    if job.get("state") == "running" and job.get("port"):
        try:
            sample = _k6_api(job["port"], "/v1/metrics/http_reqs")["data"]["attributes"]["sample"]
            progress.update(requests=int(sample.get("count", 0)), requests_per_second=round(float(sample.get("rate", 0)), 1))
        except (URLError, OSError, KeyError, TypeError, ValueError):
            pass
    return progress


def cancel_job(job: dict) -> bool:
    """Ask k6 to stop; it still writes the summary, which is saved as a cancelled partial run."""
    if job.get("state") != "running" or not job.get("port"):
        return False
    try:
        _k6_api(job["port"], "/v1/status", {"data": {"type": "status", "id": "default", "attributes": {"stopped": True}}}, "PATCH")
    except (URLError, OSError, ValueError):
        _CANCELLED.add(job["run_id"])
        return True
    _CANCELLED.add(job["run_id"])
    return True


_CANCELLED: set[str] = set()


def interpret_output(output: dict, routes: list[dict], *, cancelled: bool = False,
                     measured_seconds: int | None = None) -> tuple[list[dict], bool, list[str]]:
    measured = output.get("routes")
    if not isinstance(measured, list) or len(measured) != len(routes):
        raise PerformanceError("The benchmark runner returned invalid summary data. Retry the run.")
    if output.get("login_failures", 0):
        raise PerformanceError("HTTP login failed: the login request was rejected or redirected back to the login page. "
                               "Check the login path, field names, credentials, CSRF settings, and the logged-in check route.")
    if not cancelled and not any(isinstance(item, dict) and item.get("count", 0) for item in measured):
        raise PerformanceError("The selected routes produced no measurements. Check target reachability and retry.")
    if not cancelled and all(isinstance(item, dict) and int(item.get("count", 0) or 0) <= int(item.get("network_errors", 0) or 0)
                             for item in measured):
        raise PerformanceError("The configured target could not be reached from the API service. Check the origin and target network, then retry.")
    reasons = []
    results = []
    for route, item in zip(routes, measured):
        item = dict(item) if isinstance(item, dict) else {}
        item["path"] = route["path"]
        item["template"] = route["template"]
        statuses = item.get("statuses") or {}
        if item.get("network_errors"):
            reasons.append(f"{route['path']}: {item['network_errors']} requests failed at the network level.")
        if item.get("auth_rejected"):
            reasons.append(f"{route['path']}: {item['auth_rejected']} requests were rejected (401/403).")
        if statuses.get("3xx"):
            reasons.append(f"{route['path']}: {statuses['3xx']} responses were redirects, which are not measured as successes.")
        if statuses.get("4xx", 0) > (item.get("auth_rejected") or 0):
            reasons.append(f"{route['path']}: {statuses['4xx'] - (item.get('auth_rejected') or 0)} responses were client errors (4xx); "
                           "check that the path and its parameters exist.")
        if statuses.get("5xx"):
            reasons.append(f"{route['path']}: {statuses['5xx']} responses were server errors (5xx).")
        # k6 divides by the whole test time (warm-up included); report throughput over the measured window.
        if measured_seconds and not cancelled and item.get("count"):
            item["throughput_rps"] = round(item["count"] / measured_seconds, 2)
        if not item.get("count"):
            reasons.append(f"{route['path']}: no requests completed.")
        results.append(item)
    if cancelled:
        reasons.insert(0, "The run was cancelled before it finished.")
    return results, not reasons, reasons


def _run_k6(script: str, port: int, timeout: int) -> subprocess.CompletedProcess:
    script_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".js", prefix=K6_SCRIPT_PREFIX,
                                         dir=K6_SCRIPT_TMPFS_DIR, delete=False) as script_file:
            script_file.write(script)
            script_path = Path(script_file.name)
        os.chmod(script_path, 0o600)
    except OSError as exc:
        if script_path is not None:
            script_path.unlink(missing_ok=True)
        raise PerformanceError("The API service could not prepare an in-memory benchmark script. Check its temporary filesystem and retry.") from exc
    try:
        return subprocess.run(["k6", "run", "--quiet", "--address", f"127.0.0.1:{port}", str(script_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise PerformanceError("The benchmark runner timed out. Reduce the workload or check the API service and target, then retry.") from exc
    except FileNotFoundError as exc:
        raise PerformanceError("The pinned k6 runner is unavailable in the API service. Rebuild python-api and retry.") from exc
    except OSError as exc:
        raise PerformanceError("The k6 runner could not start. Rebuild python-api or check the target network, then retry.") from exc
    finally:
        if script_path is not None:
            script_path.unlink(missing_ok=True)


def execute_run(run_id: str, port: int, target: dict, routes: list[dict], workload: dict, auth: dict,
                pinned_address: str | None, runner: Callable[[str, int, int], Any] = _run_k6) -> dict:
    """Run k6 synchronously and turn its summary into report fields (no persistence)."""
    script = build_script(target["origin"], [route["path"] for route in routes], workload, auth,
                          thresholds=target.get("thresholds"), pinned_address=pinned_address, run_id=run_id)
    completed = runner(script, port, expected_seconds(workload) + 60)
    cancelled = run_id in _CANCELLED
    # k6 exits 99 when a threshold (budget) fails; the summary is still complete and is reported.
    try:
        output = json.loads(completed.stdout.decode("utf-8").strip() or "{}")
    except (UnicodeError, ValueError) as exc:
        raise PerformanceError("The benchmark runner returned invalid summary data. Retry the run.") from exc
    if completed.returncode not in (0, 99) and not (cancelled and output.get("routes")):
        raise PerformanceError("The benchmark could not reach the target or the k6 runner failed. Check the target origin and authentication, then retry.")
    results, complete, reasons = interpret_output(output, routes, cancelled=cancelled,
                                                  measured_seconds=workload["duration_seconds"])
    return {"runner_version": output.get("runner_version", K6_VERSION), "route_results": results,
            "complete": complete and not cancelled, "partial_reasons": reasons, "cancelled": cancelled,
            "thresholds_failed": completed.returncode == 99}


def sanitized_target(target: dict) -> dict:
    """The target settings a report keeps: never tokens, credentials, cookies or CSRF values."""
    login = {key: value for key, value in (target.get("login") or {}).items()
             if key in {"login_path", "username_field", "password_field", "csrf_mode", "csrf_path",
                        "csrf_cookie_name", "csrf_field_name", "csrf_header_name", "check_path"}}
    return {"name": target.get("name"), "origin": target["origin"], "auth_mode": target.get("auth_mode", "public"),
            "login": login, "thresholds": target.get("thresholds") or {}}


def start_run(store: PerformanceStore, user_id: str, project_id: str, target: dict, selections: Any, revision: dict | None,
              bearer_token: str | None = None, login_credentials: dict | None = None,
              runner: Callable[[str, int, int], Any] = _run_k6, background: bool = True) -> dict:
    """Validate, register and start a background benchmark; returns the running job."""
    routes, workload = prepare_run(target, selections)
    origin = validate_origin(target.get("origin"))
    if target.get("auth_mode", "public") != "public" and origin.startswith("http://") and not is_local_origin(origin):
        raise PerformanceError("Use an HTTPS origin when the target needs a token or login; credentials must not travel in clear text.")
    auth = _auth_config(target, bearer_token, login_credentials)
    pinned = _validate_resolved_target(origin)
    summary = {"target_name": target.get("name"), "target_origin": origin, "routes": [route["path"] for route in routes],
               "workload": workload, "revision_id": (revision or {}).get("revision_id")}
    run_id, port = store.claim_run(user_id, project_id, target["target_id"], expected_seconds(workload), summary)
    created_at = _now()

    def work() -> None:
        try:
            measured = execute_run(run_id, port, {**target, "origin": origin}, routes, workload, auth, pinned, runner)
            store.save_run(user_id, project_id, {
                "run_id": run_id, "project_id": project_id, "state": "completed", "created_at": created_at,
                "finished_at": _now(), "target_id": target["target_id"], "target_name": target.get("name") or origin,
                "target_origin": origin, "revision_id": (revision or {}).get("revision_id"),
                "commit_sha": (revision or {}).get("commit_sha"), "source_ref": (revision or {}).get("source_ref"),
                "routes": [route["path"] for route in routes], "route_templates": [route["template"] for route in routes],
                "auth_mode": target.get("auth_mode", "public"), "workload": workload,
                "thresholds": target.get("thresholds") or {}, "target_config": sanitized_target({**target, "origin": origin}),
                "baseline": False, **measured})
            store.finish_job(run_id, "completed")
        except PerformanceError as exc:
            store.finish_job(run_id, "failed", str(exc))
        except Exception as exc:  # never leak details; keep the job from staying "running"
            logger.error("code_quality_performance_failed project=%s error_type=%s", project_id, type(exc).__name__)
            store.finish_job(run_id, "failed", "The benchmark failed unexpectedly. Check the API logs and retry.")
        finally:
            _CANCELLED.discard(run_id)

    if background:
        threading.Thread(target=work, name=f"benchmark-{run_id[:8]}", daemon=True).start()
    else:
        work()
    return store.job(user_id, project_id, run_id) or {"run_id": run_id, "state": "running"}


def cleanup_stale_scripts(directory: Path | None = None) -> int:
    """Remove k6 scripts (which may hold secrets) left behind by an API process that was killed."""
    removed = 0
    try:
        for path in (directory or K6_SCRIPT_TMPFS_DIR).glob(K6_SCRIPT_PREFIX + "*.js"):
            path.unlink(missing_ok=True)
            removed += 1
    except OSError:
        pass
    return removed


# ----- comparison and export ----------------------------------------------------------------------

def _pct(before: float | None, after: float | None) -> float | None:
    if not isinstance(before, (int, float)) or not isinstance(after, (int, float)) or before == 0:
        return None
    return round(100 * (after - before) / before, 1)


def compare_runs(left: dict, right: dict) -> dict:
    keys = ("target_origin", "routes", "auth_mode", "workload", "runner_version")
    differences = [key for key in keys if left.get(key) != right.get(key)]
    equivalent = not differences
    by_path = lambda run: {item.get("path"): item for item in run.get("route_results", [])}
    before, after = by_path(left), by_path(right)
    rows = []
    for path in list(dict.fromkeys([*before, *after])):
        a, b = before.get(path) or {}, after.get(path) or {}
        la, lb = a.get("latency_ms") or {}, b.get("latency_ms") or {}
        row = {"path": path}
        for label, key in (("p50", "med"), ("p95", "p(95)"), ("p99", "p(99)")):
            row[label] = {"before": la.get(key), "after": lb.get(key), "change_pct": _pct(la.get(key), lb.get(key))}
        row["throughput_rps"] = {"before": a.get("throughput_rps"), "after": b.get("throughput_rps"),
                                 "change_pct": _pct(a.get("throughput_rps"), b.get("throughput_rps"))}
        row["error_rate"] = {"before": a.get("error_rate"), "after": b.get("error_rate")}
        p95a, p95b = la.get("p(95)"), lb.get("p(95)")
        worse_latency = isinstance(p95a, (int, float)) and isinstance(p95b, (int, float)) and p95b > p95a * REGRESSION_P95_RATIO
        worse_errors = (b.get("error_rate") or 0) > (a.get("error_rate") or 0) + 0.001
        row["regression"] = bool(equivalent and a and b and (worse_latency or worse_errors))
        row["improvement"] = bool(equivalent and a and b and isinstance(p95a, (int, float)) and isinstance(p95b, (int, float))
                                  and p95b < p95a / REGRESSION_P95_RATIO and not worse_errors)
        rows.append(row)
    return {"equivalent": equivalent, "differences": differences,
            "revisions": {"before": left.get("revision_id"), "after": right.get("revision_id"),
                          "changed": left.get("revision_id") != right.get("revision_id")},
            "routes": rows, "regressions": sum(1 for row in rows if row["regression"])}


def export_script(report: dict) -> str:
    target = report.get("target_config") or {"origin": report["target_origin"], "auth_mode": report.get("auth_mode", "public"), "login": {}}
    auth: dict = {"mode": target.get("auth_mode", "public")}
    if auth["mode"] == "http_login":
        auth.update(target.get("login") or {})
    if auth["mode"] == "bearer":
        auth["token"] = ""
    workload = validate_workload(report.get("workload") or {})
    return build_script(target["origin"], list(report.get("routes") or []), workload, auth,
                        thresholds=report.get("thresholds") or target.get("thresholds"), export=True)


def benchmark_traces(project_id: str, revision_id: str | None, run_id: str, limit: int = 15) -> dict:
    """Slowest Runtime-traced requests the benchmark generated (tagged with its run id)."""
    if not revision_id:
        return {"available": False, "reason": "This run is not linked to an indexed revision.", "requests": [], "routes": []}
    try:
        UUID(run_id)
    except ValueError:
        return {"available": False, "reason": "Invalid run.", "requests": [], "routes": []}
    from telemetry.runtime import ClickHouseRuntime
    runtime = ClickHouseRuntime()
    if not runtime.configured:
        return {"available": False, "reason": "ClickHouse is not configured.", "requests": [], "routes": []}
    table = f"`{runtime.database}`.`{runtime.table}`"
    from telemetry.span_schema import SPAN_SCOPE
    where = SPAN_SCOPE + " AND SpanKind = 'Server' AND SpanAttributes['indexer.benchmark_run_id'] = {run_id:String}"
    try:
        slowest = runtime.rows(
            f"SELECT TraceId AS trace_id, SpanAttributes['http.route'] AS route, SpanAttributes['http.request.method'] AS method, "
            f"toInt32OrNull(SpanAttributes['http.response.status_code']) AS status_code, toFloat64(Duration) / 1e6 AS duration_ms, "
            f"toString(Timestamp) AS timestamp FROM {table} WHERE {where} ORDER BY Duration DESC LIMIT {int(limit)}",
            project_id, revision_id, {"param_run_id": run_id})
        routes = runtime.rows(
            f"SELECT SpanAttributes['http.route'] AS route, count() AS traced, quantile(0.95)(toFloat64(Duration)) / 1e6 AS p95_ms, "
            f"countIf(StatusCode = 'Error') AS failures FROM {table} WHERE {where} GROUP BY route ORDER BY traced DESC LIMIT 50",
            project_id, revision_id, {"param_run_id": run_id})
    except Exception as exc:
        logger.warning("benchmark_trace_lookup_failed project=%s error_type=%s", project_id, type(exc).__name__)
        return {"available": False, "reason": "Runtime telemetry is unavailable.", "requests": [], "routes": []}
    reason = None if slowest else ("No traced requests carry this run's id. The application needs the Indexr OpenTelemetry "
                                   "library with benchmark tagging, and sampled requests must reach Runtime.")
    return {"available": bool(slowest), "reason": reason, "requests": slowest, "routes": routes}
