"""Replay probes against developer-run targets and merge the comparison into the report.

Contract: @.archcore/regression/regression-check.spec.md clauses 13-14, 17-18 and failure clauses
3-6. Decisions: @.archcore/regression/developer-runs-targets-app-only-sends-http.adr.md (the app
only sends HTTP; targets are saved benchmark targets with the same origin checks and DNS pinning)
and @.archcore/regression/replay-writes-only-on-disposable-targets.adr.md (non-GET probes only on a
disposable target, after an optional reset request).

A baseline run sends every probe twice; fields that differ between the two passes are noise. A
candidate run sends the same probes once, in the same order, and is compared with the newest
completed baseline. Every request carries ``X-DevAssistant-Benchmark: <run id>`` so its spans can
be found again (clause 18).
"""

from __future__ import annotations

import http.client
import logging
import re
import socket
import ssl
import threading
import time
from hashlib import sha256
from typing import Any, Callable
from urllib.parse import urlsplit

from regression.diff import compare, noise_between
from regression.model import NOT_COVERED, SUSPECTED_REGRESSION, TIER_REPLAY, TIER_TELEMETRY, UNCHANGED
from regression.probes import SAFE_METHODS
from regression.recordings import MAX_BODY_BYTES, Recordings

REQUEST_TIMEOUT_S = 15
MAX_READ_BYTES = 20 * 1024 * 1024
USER_AGENT = "DevAssistant-Regression/1"
P95_REGRESSION = 0.10  # same threshold as benchmark comparison
logger = logging.getLogger(__name__)


class ReplayError(RuntimeError):
    """A replay cannot start or had to stop; the message is safe to show."""


# --- transport ------------------------------------------------------------------------------------------

class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to a pinned address with SNI and certificate checks for the original host name."""

    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def send(origin: str, pinned: str | None, method: str, path: str, headers: dict[str, str], body: str | None,
         timeout: float = REQUEST_TIMEOUT_S) -> dict[str, Any]:
    """One request, no redirects followed; the body is hashed in full and kept up to ``MAX_BODY_BYTES``."""
    parts = urlsplit(origin)
    host, port = parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)
    if parts.scheme == "https":
        conn: http.client.HTTPConnection = _PinnedHTTPS(host, port, pinned or host, timeout)
    else:
        conn = http.client.HTTPConnection(pinned or host, port, timeout=timeout)
    started = time.monotonic()
    try:
        conn.request(method, path, body=body.encode("utf-8") if body is not None else None,
                     headers={"Host": parts.netloc, **headers})
        response = conn.getresponse()
        digest, kept, size = sha256(), bytearray(), 0
        while size < MAX_READ_BYTES:
            chunk = response.read(65536)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
            if len(kept) <= MAX_BODY_BYTES:
                kept.extend(chunk)
        truncated = size > MAX_BODY_BYTES
        return {"status": response.status, "headers": {k.lower(): v for k, v in response.getheaders()},
                "body": None if truncated else bytes(kept), "body_sha256": digest.hexdigest() if size else None,
                "truncated": truncated, "size": size, "error": None,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}
    except (OSError, http.client.HTTPException) as exc:
        return {"status": None, "headers": {}, "body": None, "body_sha256": None, "truncated": False, "size": 0,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}", "elapsed_ms": None}
    finally:
        conn.close()


# --- runs -----------------------------------------------------------------------------------------------

def prepare_target(target: dict[str, Any]) -> tuple[str, str | None]:
    """Failure clause 3: origin validation, HTTPS for credentials, and the pinned address."""
    from code_quality.performance import PerformanceError, _validate_resolved_target, is_local_origin, validate_origin

    if target.get("auth_mode") == "http_login":
        raise ReplayError("Replay does not support HTTP-login targets yet. Use a public or Bearer-token target.")
    try:
        origin = validate_origin(target.get("origin"))
        if target.get("auth_mode") == "bearer" and origin.startswith("http://") and not is_local_origin(origin):
            raise ReplayError("Use an HTTPS origin when the target needs a token.")
        return origin, _validate_resolved_target(origin)
    except PerformanceError as exc:
        raise ReplayError(str(exc)) from exc


def plan_probes(probes: list[dict[str, Any]], disposable: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Failure clause 4: write probes run only on a disposable target; the rest are listed as skipped."""
    ordered = sorted(probes, key=lambda probe: probe.get("order", 0))
    sent = [p for p in ordered if p["method"] in SAFE_METHODS or disposable]
    skipped = [{"probe_id": p["probe_id"], "method": p["method"], "path": p["path"],
                "reason": "write request on a target not marked disposable"}
               for p in ordered if p not in sent]
    return sent, skipped


def _headers(probe: dict[str, Any], run_id: str, token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json, text/html;q=0.9, */*;q=0.5", "User-Agent": USER_AGENT,
               "X-DevAssistant-Benchmark": run_id, **probe.get("headers", {})}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def execute(run: dict[str, Any], target: dict[str, Any], probes: list[dict[str, Any]], *, settings: dict[str, Any],
            token: str | None, recordings: Recordings, project_id: str, sender: Callable[..., dict[str, Any]] = send,
            baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send the probes for one run and return its summary; raises ReplayError to stop (failure clause 5)."""
    origin, pinned = prepare_target(target)
    sent, skipped = plan_probes(probes, bool(settings.get("disposable")))
    if not sent:
        raise ReplayError("No probe can be sent: add GET probes or mark the target disposable.")
    passes = 2 if run["kind"] == "baseline" else 1
    check_id, run_id = run["check_id"], run["run_id"]
    reset = settings.get("reset_path") if settings.get("disposable") else None
    for pass_no in range(1, passes + 1):
        if reset:
            response = sender(origin, pinned, "POST", reset, _headers({}, run_id, token), None)
            if response.get("error") or not (200 <= int(response.get("status") or 0) < 400):
                raise ReplayError(f"The reset request to {reset} failed ({response.get('error') or response.get('status')}).")
        for index, probe in enumerate(sent):
            response = sender(origin, pinned, probe["method"], probe["path"], _headers(probe, run_id, token), probe.get("body"))
            if index == 0 and pass_no == 1 and response.get("error"):
                who = "baseline" if run["kind"] == "baseline" else "candidate"
                raise ReplayError(f"The {who} target {origin} is unreachable: {response['error']}. Start it and retry.")
            recordings.save(project_id, check_id, run_id, probe["probe_id"], pass_no, response)
    summary: dict[str, Any] = {"target_origin": origin, "sent": len(sent), "skipped": skipped, "passes": passes}
    if run["kind"] == "baseline":
        summary["noise"] = {probe["probe_id"]: noise_between(
            recordings.load(project_id, check_id, run_id, probe["probe_id"], 1) or {},
            recordings.load(project_id, check_id, run_id, probe["probe_id"], 2) or {}) for probe in sent}
        return summary
    summary["baseline_run_id"] = baseline["run_id"] if baseline else None
    run = {**run, "target_origin": origin}
    summary["results"] = compare_with_baseline(project_id, run, sent, baseline, settings, recordings)
    return summary


def compare_with_baseline(project_id: str, run: dict[str, Any], probes: list[dict[str, Any]],
                          baseline: dict[str, Any] | None, settings: dict[str, Any],
                          recordings: Recordings) -> list[dict[str, Any]]:
    """Clauses 14-16: each candidate response against the baseline's first pass, minus noise and rules."""
    noise = ((baseline or {}).get("summary") or {}).get("noise") or {}
    results = []
    for probe in probes:
        row = {"probe_id": probe["probe_id"], "method": probe["method"], "path": probe["path"],
               "template": probe.get("template") or probe["path"]}
        before = recordings.load(project_id, run["check_id"], baseline["run_id"], probe["probe_id"], 1) if baseline else None
        after = recordings.load(project_id, run["check_id"], run["run_id"], probe["probe_id"], 1)
        if before is None or after is None:
            results.append({**row, "equal": None, "reason": "no baseline recording for this probe"})
            continue
        origins = (((baseline or {}).get("summary") or {}).get("target_origin") or "", run.get("target_origin") or "")
        outcome = compare(before, after, noise=noise.get(probe["probe_id"]), ignore=settings.get("ignore"), origins=origins)
        results.append({**row, **outcome, "base_status": before.get("status"), "candidate_status": after.get("status")})
    return results


def start(store: Any, project_id: str, check_id: str, kind: str, target: dict[str, Any], *, token: str | None = None,
          recordings: Recordings | None = None, sender: Callable[..., dict[str, Any]] = send,
          background: bool = True, on_done: Callable[[str, str], Any] | None = None) -> dict[str, Any]:
    """Claim and run a replay; raises ReplayError before claiming when it cannot start."""
    probes = store.get_probes(project_id, check_id)
    if not probes:
        raise ReplayError("Add probes before recording or replaying.")
    settings = store.get_settings(project_id, check_id)
    prepare_target(target)  # refuse a bad target before claiming a run
    baseline = None
    if kind == "candidate":
        baseline = store.latest_run(project_id, check_id, "baseline")
        if baseline is None:
            raise ReplayError("Record a baseline on the old version first.")
    try:
        run = store.claim_run(project_id, check_id, kind, target["target_id"])
    except RuntimeError as exc:
        raise ReplayError(f"Replay run {exc} is still running for this project.") from exc
    recordings = recordings or Recordings()

    def work() -> None:
        try:
            summary = execute(run, target, probes, settings=settings, token=token, recordings=recordings,
                              project_id=project_id, sender=sender, baseline=baseline)
            store.finish_run(project_id, run["run_id"], "completed", summary)
            if on_done:
                on_done(project_id, check_id)
        except ReplayError as exc:
            store.finish_run(project_id, run["run_id"], "failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 — never leave a run "running"
            logger.error("regression_replay_failed project=%s error_type=%s", project_id, type(exc).__name__)
            store.finish_run(project_id, run["run_id"], "failed", error="The replay failed unexpectedly. Check the API logs.")

    if background:
        threading.Thread(target=work, name=f"regression-replay-{run['run_id'][:8]}", daemon=True).start()
    else:
        work()
    return store.get_run(project_id, run["run_id"])


# --- clause 18: spans of both runs ----------------------------------------------------------------------

def span_summary(project_id: str, revision_id: str, run_id: str) -> dict[str, dict[str, Any]] | None:
    """Per route: requests, p95 ms, database spans per request and error spans, from spans tagged with the run."""
    from telemetry.runtime import ClickHouseRuntime
    from telemetry.span_schema import SPAN_SCOPE

    runtime = ClickHouseRuntime()
    if not runtime.configured:
        return None
    table = f"`{runtime.database}`.`{runtime.table}`"
    tagged = (f"SELECT TraceId FROM {table} WHERE {SPAN_SCOPE} AND SpanKind = 'Server' "
              f"AND SpanAttributes['indexer.benchmark_run_id'] = {{run_id:String}}")
    try:
        rows = runtime.rows(
            f"SELECT TraceId AS trace_id, anyIf(SpanAttributes['http.route'], SpanKind = 'Server') AS route, "
            f"maxIf(toFloat64(Duration), SpanKind = 'Server') / 1e6 AS duration_ms, "
            f"countIf(SpanAttributes['db.system.name'] != '') AS db_spans, countIf(StatusCode = 'Error') AS errors "
            f"FROM {table} WHERE {SPAN_SCOPE} AND TraceId IN ({tagged}) GROUP BY TraceId LIMIT 5000",
            project_id, revision_id, {"param_run_id": run_id})
    except Exception as exc:  # noqa: BLE001 — span deltas enrich the report; it stands without them
        logger.warning("regression_span_lookup_failed error_type=%s", type(exc).__name__)
        return None
    return summarize_spans(rows)


def summarize_spans(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_route: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("route"):
            by_route.setdefault(row["route"], []).append(row)
    out = {}
    for route, traces in by_route.items():
        durations = sorted(float(t.get("duration_ms") or 0) for t in traces)
        p95 = durations[min(len(durations) - 1, int(round(0.95 * (len(durations) - 1))))]
        out[route] = {"requests": len(traces), "p95_ms": round(p95, 1),
                      "db_spans_per_request": round(sum(int(t.get("db_spans") or 0) for t in traces) / len(traces), 2),
                      "errors": sum(int(t.get("errors") or 0) for t in traces)}
    return out


def span_deltas(before: dict[str, dict[str, Any]] | None, after: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Clause 18: query-count, error and p95 changes per route template, flagged like benchmark comparison."""
    if not before or not after:
        return {}
    out = {}
    for route in sorted(set(before) & set(after)):
        a, b = before[route], after[route]
        p95_change = (b["p95_ms"] - a["p95_ms"]) / a["p95_ms"] if a["p95_ms"] else None
        flags = []
        if b["db_spans_per_request"] != a["db_spans_per_request"]:
            flags.append("query_count")
        if b["errors"] > a["errors"]:
            flags.append("new_errors")
        if p95_change is not None and p95_change > P95_REGRESSION:
            flags.append("slower")
        out[route] = {"before": a, "after": b, "p95_change_pct": round(p95_change * 100, 1) if p95_change is not None else None,
                      "flags": flags}
    return out


# --- clause 17: merge into the report -------------------------------------------------------------------

def _span_delta(deltas: dict[str, dict[str, Any]] | None, template: str) -> dict[str, Any] | None:
    """Spans name routes as the framework does, with or without the leading slash."""
    wanted = template.strip("/")
    return next((delta for route, delta in (deltas or {}).items() if route.strip("/") == wanted), None)


def _template_pattern(template: str) -> re.Pattern:
    """A route template as a regex over concrete paths: each {param} matches one segment."""
    parts = re.split(r"(\{[A-Za-z_][A-Za-z0-9_]*\??\})", template)
    return re.compile("^" + "".join("[^/]+" if part.startswith("{") else re.escape(part) for part in parts) + "$")


def _result_key(row: dict[str, Any], templates: list[tuple[str, str]]) -> str:
    """The report route a replayed request exercises: its own template, else the route whose template matches its path.

    Hand-typed or imported probes carry only a concrete path, such as /api/orders/1 for /api/orders/{id}.
    """
    key = _route_key(row["method"], row["template"])
    if "{" in row["template"]:
        return key
    path = row["path"].split("?", 1)[0]
    for method, template in templates:
        if _route_key(method, "") == _route_key(row["method"], "") and _template_pattern(template).match(path):
            return _route_key(method, template)
    return key


def _route_key(method: str, template: str) -> str:
    from regression.probes import _method

    return f"{_method(method)} {template}"


def merge(report: dict[str, Any], candidate: dict[str, Any] | None, deltas: dict[str, dict[str, Any]] | None = None,
          *, tests: list[Any] | None = None) -> dict[str, Any]:
    """Route verdicts and coverage from replay results; requests outside the report's routes are listed apart."""
    from regression.coverage import apply_coverage

    results = ((candidate or {}).get("summary") or {}).get("results") or []
    templates = [(route.get("method", ""), route.get("path", "")) for route in report.get("routes", [])]
    by_route: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        by_route.setdefault(_result_key(row, templates), []).append(row)
    known = set()
    for route in report.get("routes", []):
        key = _route_key(route.get("method", ""), route.get("path", ""))
        known.add(key)
        rows = [row for row in by_route.get(key, []) if row.get("equal") is not None]
        route.pop("replay", None)
        route.pop("spans", None)
        # The static verdict is kept apart so a later replay starts from it, never from an earlier replay.
        static = route.setdefault("static_verdict", route.get("verdict"))
        route["verdict"] = static
        if rows:
            differing = [row for row in rows if not row["equal"]]
            route["replay"] = {"probes": len(rows), "differing": len(differing),
                               "differences": [{"path": row["path"], "differences": row["differences"][:10]}
                                               for row in differing[:5]]}
            if differing:
                route["verdict"] = SUSPECTED_REGRESSION
            elif static in (None, NOT_COVERED, UNCHANGED):
                route["verdict"] = UNCHANGED  # a judgment of change stays until a person reviews it
        delta = _span_delta(deltas, route.get("path", ""))
        if delta:
            route["spans"] = delta
    report["replay_other"] = [
        {"method": row["method"], "path": row["path"], "equal": row.get("equal"),
         "verdict": SUSPECTED_REGRESSION if row.get("equal") is False else (UNCHANGED if row.get("equal") else NOT_COVERED),
         "differences": (row.get("differences") or [])[:10]}
        for key, rows in by_route.items() if key not in known for row in rows]
    replayed = {key for key, rows in by_route.items() if any(row.get("equal") is not None for row in rows)}
    apply_coverage(report, tests=tests, replayed=replayed)
    if results:
        report["tier"] = TIER_TELEMETRY if deltas else TIER_REPLAY
        report["replay"] = {"candidate_run_id": candidate["run_id"],
                            "baseline_run_id": candidate["summary"].get("baseline_run_id"),
                            "probes": len(results), "differing": sum(1 for r in results if r.get("equal") is False),
                            "skipped": candidate["summary"].get("skipped", [])}
    return report
