"""Hourly per-revision rollups behind the Runtime summary.

`summary()` used to run seven queries over the whole aggregation window on every call, two of
them self-joins of the span table, so its cost grew with the window. Closed hours are now
rolled up once into `runtime_span_rollups`; a summary reads those rows plus the raw spans of
hours that are not rolled up yet (the current hour, or hours the last cycle has not reached).

Each row is one (revision, hour, kind, key) with a call count, failures, the newest span time
and a mergeable p95 state. Kinds:

- ``all``: every span in the hour; k1 = the head-sampling ratio it was recorded at ('' = unknown)
- ``symbol``: k1 = symbol id
- ``route``: k1 = method, k2 = route, k3 = route id
- ``table``: k1 = table id, k2 = operation
- ``symbol_table``: k1 = symbol id, k2 = table id
- ``parent``: k1 = child symbol id, k2 = parent symbol id
- ``symbol_route``: k1 = symbol id, k2 = route id
- ``hour``: marker written last; an hour with a marker is rolled up

Joins (``parent``, ``symbol_route``) count the child span's hour and look for its parent or
server span from one hour earlier, so a request that crosses an hour boundary still joins.
Rows are ReplacingMergeTree, so a retried hour replaces its rows. Deleting requests
invalidates the affected hours, which are then rolled up again.
"""
from __future__ import annotations

from telemetry.span_schema import SPAN_SCOPE

ROLLUP_TABLE = "runtime_span_rollups"
ROLLUP_MARKER = "devassistant-runtime-rollups:v1"
# Spans reach ClickHouse within the 5 s batch window plus up to 30 s of exporter retries.
# An hour is rolled up only once it has been closed this long.
SETTLE_MINUTES = 5
MAX_HOURS_PER_CYCLE = 48
KINDS = ("all", "symbol", "route", "table", "symbol_table", "parent", "symbol_route")

_ROLLUP_SCOPE = "project_id = {project_id:String} AND revision_id = {revision_id:String}"
_ROUTE_ID = (
    "if(SpanAttributes['indexer.route_id'] != '', SpanAttributes['indexer.route_id'], "
    "concat('route:', upper(SpanAttributes['http.request.method']), ' ', HttpRoute))"
)
_TABLE_IDS = (
    "arrayJoin(if(length(JSONExtractArrayRaw(SpanAttributes['indexer.table_ids'])) > 0, "
    "arrayMap(value -> replaceAll(value, '\"', ''), JSONExtractArrayRaw(SpanAttributes['indexer.table_ids'])), "
    "[if(SpanAttributes['indexer.db.table'] != '', SpanAttributes['indexer.db.table'], SpanAttributes['db.collection.name'])]))"
)
_NO_P95 = "quantileStateIf(0.95)(toFloat64(Duration), 0)"
_P95 = "quantileState(0.95)(toFloat64(Duration))"
_COLUMNS = "hour, kind, k1, k2, k3, calls, failures, last_seen, duration"


def rollup_ddl(database: str, retention_hours: int) -> str:
    return (
        f"CREATE TABLE IF NOT EXISTS `{database}`.`{ROLLUP_TABLE}` (\n"
        "    project_id LowCardinality(String), revision_id LowCardinality(String),\n"
        "    hour DateTime, kind LowCardinality(String), k1 String, k2 String, k3 String,\n"
        "    calls UInt64, failures UInt64, last_seen DateTime64(9),\n"
        "    duration AggregateFunction(quantile(0.95), Float64), rolled_at DateTime64(3)\n"
        ") ENGINE = ReplacingMergeTree(rolled_at)\n"
        "PARTITION BY toDate(hour)\n"
        "ORDER BY (project_id, revision_id, hour, kind, k1, k2, k3)\n"
        f"TTL hour + INTERVAL {int(retention_hours) + 1} HOUR\n"
        f"COMMENT '{ROLLUP_MARKER}'"
    )


def _kind_selects(spans: str, child: str, parent: str) -> list[str]:
    """SELECTs producing rollup columns for spans matching ``child``.

    ``parent`` is the time filter for the join side (parent and server spans).
    """
    # Explicit columns: `SELECT *` leaves out MATERIALIZED columns such as SymbolId.
    src = (
        "SELECT Timestamp, TraceId, SpanId, ParentSpanId, Duration, StatusCode, SpanAttributes, SymbolId, HttpRoute, "
        "ResourceAttributes['indexer.sampling.ratio'] AS SamplingRatio "
        f"FROM {spans} WHERE {SPAN_SCOPE} AND {child}"
    )
    joined = f"SELECT TraceId, SpanId, SymbolId, HttpRoute, SpanAttributes FROM {spans} WHERE {SPAN_SCOPE} AND {parent}"
    hour = "toStartOfHour(Timestamp)"
    return [
        f"SELECT {hour} AS hour, 'all' AS kind, SamplingRatio AS k1, '' AS k2, '' AS k3, count() AS calls, "
        f"countIf(StatusCode = 'Error') AS failures, max(Timestamp) AS last_seen, {_NO_P95} AS duration "
        f"FROM ({src}) GROUP BY hour, k1",

        f"SELECT {hour} AS hour, 'symbol' AS kind, SymbolId AS k1, '' AS k2, '' AS k3, count() AS calls, "
        f"countIf(StatusCode = 'Error') AS failures, max(Timestamp) AS last_seen, {_P95} AS duration "
        f"FROM ({src}) WHERE SymbolId != '' GROUP BY hour, k1",

        f"SELECT {hour} AS hour, 'route' AS kind, SpanAttributes['http.request.method'] AS k1, HttpRoute AS k2, "
        f"{_ROUTE_ID} AS k3, count() AS calls, countIf(StatusCode = 'Error') AS failures, "
        f"max(Timestamp) AS last_seen, {_P95} AS duration "
        f"FROM ({src}) WHERE HttpRoute != '' GROUP BY hour, k1, k2, k3",

        f"SELECT hour, 'table' AS kind, table_id AS k1, operation AS k2, '' AS k3, count() AS calls, "
        f"toUInt64(0) AS failures, max(Timestamp) AS last_seen, {_NO_P95} AS duration FROM ("
        f"SELECT {hour} AS hour, {_TABLE_IDS} AS table_id, upper(SpanAttributes['db.operation.name']) AS operation, "
        f"Timestamp, Duration FROM ({src})) WHERE table_id != '' GROUP BY hour, k1, k2",

        f"SELECT hour, 'symbol_table' AS kind, SymbolId AS k1, table_id AS k2, '' AS k3, count() AS calls, "
        f"toUInt64(0) AS failures, max(Timestamp) AS last_seen, {_NO_P95} AS duration FROM ("
        f"SELECT {hour} AS hour, SymbolId, {_TABLE_IDS} AS table_id, Timestamp, Duration FROM ({src}) "
        "WHERE SymbolId != '') WHERE table_id != '' GROUP BY hour, k1, k2",

        f"SELECT toStartOfHour(child.Timestamp) AS hour, 'parent' AS kind, child.SymbolId AS k1, "
        "parent.SymbolId AS k2, '' AS k3, count() AS calls, toUInt64(0) AS failures, "
        f"max(child.Timestamp) AS last_seen, quantileStateIf(0.95)(toFloat64(child.Duration), 0) AS duration "
        f"FROM ({src}) AS child INNER JOIN ({joined}) AS parent "
        "ON child.TraceId = parent.TraceId AND child.ParentSpanId = parent.SpanId "
        "WHERE child.SymbolId != '' AND parent.SymbolId != '' GROUP BY hour, k1, k2",

        f"SELECT toStartOfHour(spans.Timestamp) AS hour, 'symbol_route' AS kind, spans.SymbolId AS k1, "
        "route AS k2, '' AS k3, count() AS calls, toUInt64(0) AS failures, max(spans.Timestamp) AS last_seen, "
        "quantileStateIf(0.95)(toFloat64(spans.Duration), 0) AS duration "
        f"FROM ({src}) AS spans INNER JOIN (SELECT TraceId, groupUniqArrayIf({_ROUTE_ID}, HttpRoute != '') AS routes "
        f"FROM ({joined}) GROUP BY TraceId) AS traces USING (TraceId) ARRAY JOIN traces.routes AS route "
        "WHERE spans.SymbolId != '' GROUP BY hour, k1, k2",
    ]


def roll_up_closed_hours(client, project_id: str, revision_id: str) -> int:
    """Roll up closed hours of this revision that have spans and no rollup yet; returns how many."""
    spans = f"`{client.database}`.`{client.table}`"
    rollups = f"`{client.database}`.`{ROLLUP_TABLE}`"
    window = int(client.window_hours)
    pending = client.rows(
        f"SELECT DISTINCT toString(toStartOfHour(Timestamp)) AS hour FROM {spans} WHERE {SPAN_SCOPE} "
        f"AND Timestamp >= toStartOfHour(now() - INTERVAL {window} HOUR) "
        f"AND Timestamp < toStartOfHour(now() - INTERVAL {SETTLE_MINUTES} MINUTE) "
        f"AND toStartOfHour(Timestamp) NOT IN (SELECT hour FROM {rollups} FINAL WHERE {_ROLLUP_SCOPE} AND kind = 'hour') "
        f"ORDER BY hour LIMIT {MAX_HOURS_PER_CYCLE}",
        project_id, revision_id,
    )
    rolled = 0
    for row in pending:
        hour = str(row.get("hour", ""))
        if not hour:
            continue
        child = "Timestamp >= toDateTime({hour:String}) AND Timestamp < toDateTime({hour:String}) + INTERVAL 1 HOUR"
        parent = "Timestamp >= toDateTime({hour:String}) - INTERVAL 1 HOUR AND Timestamp < toDateTime({hour:String}) + INTERVAL 1 HOUR"
        marker = (
            "SELECT toDateTime({hour:String}) AS hour, 'hour' AS kind, '' AS k1, '' AS k2, '' AS k3, "
            "toUInt64(0) AS calls, toUInt64(0) AS failures, toDateTime64(toDateTime({hour:String}), 9) AS last_seen, "
            "quantileStateIf(0.95)(toFloat64(0), 0) AS duration"
        )
        union = " UNION ALL ".join(f"SELECT {_COLUMNS} FROM ({select})" for select in _kind_selects(spans, child, parent) + [marker])
        client.execute(
            f"INSERT INTO {rollups} (project_id, revision_id, {_COLUMNS}, rolled_at) "
            f"SELECT {{project_id:String}}, {{revision_id:String}}, {_COLUMNS}, now64(3) FROM ({union})",
            extra_parameters={"param_project_id": project_id, "param_revision_id": revision_id, "param_hour": hour},
            timeout=120,
        )
        rolled += 1
    return rolled


def read_summary(client, project_id: str, revision_id: str) -> dict[str, list[dict]]:
    """Merge rolled-up hours with raw spans of hours not rolled up yet; rows grouped by kind."""
    spans = f"`{client.database}`.`{client.table}`"
    rollups = f"`{client.database}`.`{ROLLUP_TABLE}`"
    window = int(client.window_hours)
    start = f"toStartOfHour(now() - INTERVAL {window} HOUR)"
    rolled_hours = f"(SELECT hour FROM {rollups} FINAL WHERE {_ROLLUP_SCOPE} AND kind = 'hour' AND hour >= {start})"
    child = f"Timestamp >= {start} AND toStartOfHour(Timestamp) NOT IN {rolled_hours}"
    parent = f"Timestamp >= {start} - INTERVAL 1 HOUR"
    live = " UNION ALL ".join(f"SELECT {_COLUMNS} FROM ({select})" for select in _kind_selects(spans, child, parent))
    rows = client.rows(
        "SELECT kind, k1, k2, k3, sum(calls) AS calls, sum(failures) AS failures, max(last_seen) AS last_seen, "
        "if(kind IN ('symbol', 'route'), quantileMerge(0.95)(duration) / 1000000, 0) AS p95_ms FROM ("
        f"SELECT {_COLUMNS} FROM {rollups} FINAL WHERE {_ROLLUP_SCOPE} AND hour >= {start} AND kind != 'hour' "
        f"UNION ALL {live}) GROUP BY kind, k1, k2, k3",
        project_id, revision_id,
    )
    grouped: dict[str, list[dict]] = {kind: [] for kind in KINDS}
    for row in rows:
        kind = row.get("kind")
        if kind in grouped:
            grouped[kind].append(row)
    return grouped


def hours_of_traces(client, project_id: str, revision_id: str, trace_ids: list[str], parameters: dict) -> list[str]:
    spans = f"`{client.database}`.`{client.table}`"
    placeholders = ", ".join(f"{{trace_id_{index}:String}}" for index in range(len(trace_ids)))
    rows = client.rows(
        f"SELECT DISTINCT toString(toStartOfHour(Timestamp)) AS hour FROM {spans} WHERE {SPAN_SCOPE} "
        f"AND TraceId IN ({placeholders})",
        project_id, revision_id, parameters,
    )
    return [str(row["hour"]) for row in rows if row.get("hour")]


def invalidate_hours(client, project_id: str, revision_id: str, hours: list[str]) -> None:
    """Drop the rollups of these hours so the next cycle rolls them up from the remaining spans."""
    if not hours:
        return
    placeholders = ", ".join(f"toDateTime({{hour_{index}:String}})" for index in range(len(hours)))
    client.execute(
        f"DELETE FROM `{client.database}`.`{ROLLUP_TABLE}` WHERE {_ROLLUP_SCOPE} AND hour IN ({placeholders})",
        extra_parameters={
            "param_project_id": project_id, "param_revision_id": revision_id, "lightweight_deletes_sync": "2",
            **{f"param_hour_{index}": hour for index, hour in enumerate(hours)},
        },
    )
