"""What the running back end answered, from runtime telemetry.

Plan: @.archcore/frontend-assist/frontend-developer-assistance.plan.md (Phase 4).

Server request spans carry the route template, the method, and the response
status code, and nothing of the response body (the collector's privacy
transform keeps a fixed attribute list). So this reads counts per
``(method, route, status code)`` for one indexed revision.
"""

from __future__ import annotations

from typing import Any

MAX_ROWS = 5000


class RuntimeUnavailable(LookupError):
    """Runtime data cannot be read right now; the message is safe to show."""


def observed_responses(project_id: str, revision_id: str, *, runtime: Any = None) -> list[dict[str, Any]]:
    """``[{"method", "route", "status_code", "requests"}]`` for the revision, most frequent first."""
    from telemetry.runtime import _SCOPE, ClickHouseRuntime

    runtime = runtime or ClickHouseRuntime()
    if not runtime.configured:
        raise RuntimeUnavailable("Runtime telemetry is not configured, so live traffic cannot be compared.")
    table = f"`{runtime.database}`.`{runtime.table}`"
    window = f" AND Timestamp >= now() - INTERVAL {int(runtime.window_hours)} HOUR"
    sql = (
        "SELECT upper(SpanAttributes['http.request.method']) AS method, HttpRoute AS route, "
        "toInt32OrNull(SpanAttributes['http.response.status_code']) AS status_code, count() AS requests "
        f"FROM {table} WHERE {_SCOPE}{window} AND SpanKind = 'Server' AND HttpRoute != '' "
        f"GROUP BY method, route, status_code ORDER BY requests DESC LIMIT {MAX_ROWS}"
    )
    try:
        rows = runtime.rows(sql, project_id, revision_id)
    except Exception as exc:  # noqa: BLE001 — network, auth, and query errors all mean "not available now"
        raise RuntimeUnavailable("Runtime telemetry could not be read. Check the Runtime page and retry.") from exc
    return [{"method": str(row.get("method") or ""), "route": str(row.get("route") or ""),
             "status_code": int(row["status_code"]) if row.get("status_code") is not None else None,
             "requests": int(row.get("requests") or 0)} for row in rows]
