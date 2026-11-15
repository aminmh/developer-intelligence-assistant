"""Browser screen views recorded by the snippet, and the back-end requests each one sent.

Design: @.archcore/frontend-assist/browser-telemetry-through-a-same-origin-relay.rfc.md.

A screen view is one `browser.page` span in a trace of its own, with child spans
for slow interactions and JavaScript errors. A back-end request is never part of
that trace: it stays the root of its own trace (which sampling and the Runtime
views rely on) and carries the view's IDs in `indexer.browser.trace_id` and
`indexer.browser.span_id`. Reading joins the two by those IDs.

Views are read for a project across its revisions, because a screen view is
stamped with the back-end revision that relayed it, not with the revision the
user happens to have selected.
"""

from __future__ import annotations

import re
from typing import Any

MAX_VIEWS = 200
MAX_REQUESTS = 200
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")
_PROJECT = "ProjectId = {project_id:String} AND Eligible"


def _unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    return {"available": False, "reason": reason, **extra}


def _percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(fraction * len(ordered) + 0.5) - 1))]


def _quoted(ids: list[str]) -> str:
    # IDs are checked against a hex pattern before they reach SQL.
    return ", ".join(f"'{value}'" for value in ids)


def _runtime(runtime: Any) -> Any:
    if runtime is not None:
        return runtime
    from telemetry.runtime import ClickHouseRuntime

    return ClickHouseRuntime()


def screen_views(project_id: str, *, runtime: Any = None) -> dict[str, Any]:
    """Recent screen views of a project, newest first, with a per-screen summary."""
    runtime = _runtime(runtime)
    if not runtime.configured:
        return _unavailable("Runtime telemetry is not configured.", views=[], screens=[])
    table = f"`{runtime.database}`.`{runtime.table}`"
    window = f" AND Timestamp >= now() - INTERVAL {int(runtime.window_hours)} HOUR"
    try:
        rows = runtime.rows(
            "SELECT TraceId AS trace_id, SpanId AS span_id, Timestamp AS timestamp, RevisionId AS revision_id, "
            "SpanAttributes['browser.route'] AS route, toFloat64(Duration) / 1000000 AS duration_ms, "
            "toInt32OrNull(SpanAttributes['browser.vital.lcp_ms']) AS lcp_ms, "
            "toInt32OrNull(SpanAttributes['browser.vital.inp_ms']) AS inp_ms, "
            "toFloat64OrNull(SpanAttributes['browser.vital.cls']) AS cls, "
            "SpanAttributes['browser.frontend.project_id'] AS frontend_project_id "
            f"FROM {table} WHERE {_PROJECT}{window} AND SpanName = 'browser.page' "
            f"ORDER BY Timestamp DESC LIMIT {MAX_VIEWS}", project_id, "")
        views = [row for row in rows if _TRACE_ID.match(str(row.get("trace_id", ""))) and _SPAN_ID.match(str(row.get("span_id", "")))]
        children: dict[str, dict[str, int]] = {}
        requests: dict[str, dict[str, Any]] = {}
        if views:
            traces = _quoted([row["trace_id"] for row in views])
            for row in runtime.rows(
                    "SELECT TraceId AS trace_id, countIf(SpanName = 'browser.error') AS errors, "
                    "countIf(SpanName = 'browser.interaction') AS slow_interactions "
                    f"FROM {table} WHERE {_PROJECT}{window} AND SpanName IN ('browser.error', 'browser.interaction') "
                    f"AND TraceId IN ({traces}) GROUP BY TraceId", project_id, ""):
                children[row["trace_id"]] = {"errors": int(row["errors"]), "slow_interactions": int(row["slow_interactions"])}
            for row in runtime.rows(
                    "SELECT SpanAttributes['indexer.browser.trace_id'] AS view, count() AS requests, "
                    "sum(toFloat64(Duration)) / 1000000 AS request_ms, "
                    "countIf(toInt32OrNull(SpanAttributes['http.response.status_code']) >= 400) AS failed "
                    f"FROM {table} WHERE {_PROJECT}{window} AND SpanKind = 'Server' "
                    f"AND SpanAttributes['indexer.browser.trace_id'] IN ({traces}) GROUP BY view", project_id, ""):
                requests[row["view"]] = row
    except Exception:  # noqa: BLE001 — network, auth, and query errors all mean "not available now"
        return _unavailable("Screen views could not be read. Check the Runtime page and retry.", views=[], screens=[])

    clean = []
    for row in views:
        linked = requests.get(row["trace_id"]) or {}
        clean.append({
            "trace_id": row["trace_id"], "span_id": row["span_id"], "timestamp": row.get("timestamp"),
            "revision_id": row.get("revision_id") or None, "route": str(row.get("route") or "(unknown)"),
            "duration_ms": row.get("duration_ms"), "lcp_ms": row.get("lcp_ms"), "inp_ms": row.get("inp_ms"),
            "cls": row.get("cls"), "frontend_project_id": row.get("frontend_project_id") or None,
            **(children.get(row["trace_id"]) or {"errors": 0, "slow_interactions": 0}),
            "requests": int(linked.get("requests") or 0), "failed_requests": int(linked.get("failed") or 0),
            "request_ms": round(float(linked.get("request_ms") or 0), 1),
        })
    by_route: dict[str, list[dict[str, Any]]] = {}
    for view in clean:
        by_route.setdefault(view["route"], []).append(view)
    screens = []
    for route, items in by_route.items():
        screens.append({
            "route": route, "views": len(items),
            # The 75th percentile is the usual reporting point for Web Vitals.
            "lcp_p75_ms": _percentile([float(v["lcp_ms"]) for v in items if v["lcp_ms"] is not None], 0.75),
            "inp_p75_ms": _percentile([float(v["inp_ms"]) for v in items if v["inp_ms"] is not None], 0.75),
            "cls_p75": _percentile([float(v["cls"]) for v in items if v["cls"] is not None], 0.75),
            "errors": sum(v["errors"] for v in items), "slow_interactions": sum(v["slow_interactions"] for v in items),
            "requests_per_view": round(sum(v["requests"] for v in items) / len(items), 1),
        })
    screens.sort(key=lambda screen: (-screen["views"], screen["route"]))
    return {"available": True, "reason": None, "window_hours": runtime.window_hours, "views": clean, "screens": screens,
            "truncated": len(views) >= MAX_VIEWS}


def screen_view(project_id: str, trace_id: str, *, runtime: Any = None) -> dict[str, Any]:
    """One screen view: its vitals, its slow interactions and errors, and the requests it sent."""
    trace_id = trace_id.lower()
    if not _TRACE_ID.match(trace_id):
        raise ValueError("Invalid trace ID")
    runtime = _runtime(runtime)
    if not runtime.configured:
        return _unavailable("Runtime telemetry is not configured.")
    table = f"`{runtime.database}`.`{runtime.table}`"
    window = f" AND Timestamp >= now() - INTERVAL {int(runtime.window_hours)} HOUR"
    try:
        spans = runtime.rows(
            "SELECT SpanName AS name, SpanId AS span_id, Timestamp AS timestamp, RevisionId AS revision_id, "
            "toFloat64(Duration) / 1000000 AS duration_ms, SpanAttributes['browser.route'] AS route, "
            "toInt32OrNull(SpanAttributes['browser.vital.lcp_ms']) AS lcp_ms, "
            "toInt32OrNull(SpanAttributes['browser.vital.inp_ms']) AS inp_ms, "
            "toFloat64OrNull(SpanAttributes['browser.vital.cls']) AS cls, SpanAttributes['error.type'] AS error_type "
            f"FROM {table} WHERE {_PROJECT}{window} AND TraceId = '{trace_id}' "
            "AND SpanName IN ('browser.page', 'browser.error', 'browser.interaction') ORDER BY Timestamp LIMIT 100",
            project_id, "")
        requests = runtime.rows(
            "SELECT TraceId AS trace_id, Timestamp AS timestamp, RevisionId AS revision_id, "
            "upper(SpanAttributes['http.request.method']) AS method, HttpRoute AS route, "
            "toInt32OrNull(SpanAttributes['http.response.status_code']) AS status_code, "
            "toFloat64(Duration) / 1000000 AS duration_ms, StatusCode AS span_status "
            f"FROM {table} WHERE {_PROJECT}{window} AND SpanKind = 'Server' "
            f"AND SpanAttributes['indexer.browser.trace_id'] = '{trace_id}' ORDER BY Timestamp LIMIT {MAX_REQUESTS}",
            project_id, "")
    except Exception:  # noqa: BLE001
        return _unavailable("The screen view could not be read. Check the Runtime page and retry.")
    page = next((span for span in spans if span.get("name") == "browser.page"), None)
    if page is None:
        return _unavailable("This screen view was not found. It may be older than the runtime window.")
    return {
        "available": True, "reason": None,
        "view": {"trace_id": trace_id, "span_id": page.get("span_id"), "timestamp": page.get("timestamp"),
                 "revision_id": page.get("revision_id") or None, "route": str(page.get("route") or "(unknown)"),
                 "duration_ms": page.get("duration_ms"), "lcp_ms": page.get("lcp_ms"), "inp_ms": page.get("inp_ms"),
                 "cls": page.get("cls")},
        "errors": [{"timestamp": span.get("timestamp"), "error_type": str(span.get("error_type") or "Error")}
                   for span in spans if span.get("name") == "browser.error"],
        "slow_interactions": [{"timestamp": span.get("timestamp"), "duration_ms": span.get("duration_ms")}
                              for span in spans if span.get("name") == "browser.interaction"],
        "requests": [{"trace_id": str(row.get("trace_id", "")).lower(), "timestamp": row.get("timestamp"),
                      "revision_id": row.get("revision_id") or None, "method": str(row.get("method") or ""),
                      "route": str(row.get("route") or ""), "status_code": row.get("status_code"),
                      "duration_ms": row.get("duration_ms"), "failed": row.get("span_status") == "Error"
                      or (isinstance(row.get("status_code"), int) and row["status_code"] >= 500)}
                     for row in requests if _TRACE_ID.match(str(row.get("trace_id", "")).lower())],
    }
