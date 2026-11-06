"""Owned ClickHouse schema for the full-span table the Runtime views read.

The Collector's ClickHouse exporter writes into this table but no longer creates it
(`create_schema: false` on `clickhouse/full`). The exporter's own DDL sorts by
`ServiceName, SpanName` — the privacy transform renames every span to `runtime.span`,
so that key carries nothing — and every Runtime query had to look up project and
revision in the `ResourceAttributes` map on every row of the window.

Version 2 keeps the exporter's columns (so its inserts are unchanged) and adds
materialized scope columns and a sort key that matches how the table is read.
An older exporter-created table is migrated in place: the rows still inside the
retention window are copied into the new shape, and the tables are swapped atomically.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
SCHEMA_MARKER = f"devassistant-runtime-spans:v{SCHEMA_VERSION}"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# Columns the OpenTelemetry ClickHouse exporter (v0.136) writes. Order and types match its DDL.
EXPORTER_COLUMNS = (
    ("Timestamp", "DateTime64(9) CODEC(Delta(8), ZSTD(1))"),
    ("TraceId", "String CODEC(ZSTD(1))"),
    ("SpanId", "String CODEC(ZSTD(1))"),
    ("ParentSpanId", "String CODEC(ZSTD(1))"),
    ("TraceState", "String CODEC(ZSTD(1))"),
    ("SpanName", "LowCardinality(String) CODEC(ZSTD(1))"),
    ("SpanKind", "LowCardinality(String) CODEC(ZSTD(1))"),
    ("ServiceName", "LowCardinality(String) CODEC(ZSTD(1))"),
    ("ResourceAttributes", "Map(LowCardinality(String), String) CODEC(ZSTD(1))"),
    ("ScopeName", "String CODEC(ZSTD(1))"),
    ("ScopeVersion", "String CODEC(ZSTD(1))"),
    ("SpanAttributes", "Map(LowCardinality(String), String) CODEC(ZSTD(1))"),
    ("Duration", "UInt64 CODEC(ZSTD(1))"),
    ("StatusCode", "LowCardinality(String) CODEC(ZSTD(1))"),
    ("StatusMessage", "String CODEC(ZSTD(1))"),
    ("`Events.Timestamp`", "Array(DateTime64(9)) CODEC(ZSTD(1))"),
    ("`Events.Name`", "Array(LowCardinality(String)) CODEC(ZSTD(1))"),
    ("`Events.Attributes`", "Array(Map(LowCardinality(String), String)) CODEC(ZSTD(1))"),
    ("`Links.TraceId`", "Array(String) CODEC(ZSTD(1))"),
    ("`Links.SpanId`", "Array(String) CODEC(ZSTD(1))"),
    ("`Links.TraceState`", "Array(String) CODEC(ZSTD(1))"),
    ("`Links.Attributes`", "Array(Map(LowCardinality(String), String)) CODEC(ZSTD(1))"),
)

# Derived on insert from the maps, so queries filter and sort on plain columns.
MATERIALIZED_COLUMNS = (
    ("ProjectId", "LowCardinality(String) MATERIALIZED ResourceAttributes['indexer.project_id']"),
    ("RevisionId", "LowCardinality(String) MATERIALIZED ResourceAttributes['indexer.revision_id']"),
    ("Eligible", "Bool MATERIALIZED ResourceAttributes['indexer.code_dirty'] = 'false' "
                 "AND ResourceAttributes['indexer.schema.version'] = 'indexer-telemetry-v2'"),
    ("SymbolId", "String MATERIALIZED SpanAttributes['indexer.symbol_id']"),
    ("HttpRoute", "String MATERIALIZED SpanAttributes['http.route']"),
)

# Scope filter for one indexed revision; uses the sort-key prefix.
SPAN_SCOPE = "ProjectId = {project_id:String} AND RevisionId = {revision_id:String} AND Eligible"

_ready: set[tuple[str, str, str, int]] = set()


def span_table_ddl(database: str, table: str, retention_hours: int) -> str:
    _check(database, table)
    columns = ",\n    ".join(f"{name} {kind}" for name, kind in EXPORTER_COLUMNS + MATERIALIZED_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS `{database}`.`{table}` (\n    {columns},\n"
        "    INDEX idx_trace_id TraceId TYPE bloom_filter(0.001) GRANULARITY 1,\n"
        "    INDEX idx_duration Duration TYPE minmax GRANULARITY 1\n"
        ") ENGINE = MergeTree\n"
        "PARTITION BY toDate(Timestamp)\n"
        "ORDER BY (ProjectId, RevisionId, SpanKind, Timestamp)\n"
        f"TTL toDateTime(Timestamp) + INTERVAL {int(retention_hours)} HOUR\n"
        "SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1\n"
        f"COMMENT '{SCHEMA_MARKER}'"
    )


def ensure_span_schema(client, retention_hours: int) -> bool:
    """Create or migrate the full-span table once per ClickHouse target; True when it is ready.

    Safe to call often: after the first success for a target it returns without a query.
    """
    if not client.configured:
        return False
    key = (client.url, client.database, client.table, int(retention_hours))
    if key in _ready:
        return True
    database, table = client.database, client.table
    _check(database, table)
    client.execute(f"CREATE DATABASE IF NOT EXISTS `{database}`")
    current = _table_info(client, database, table)
    if current is None:
        client.execute(span_table_ddl(database, table, retention_hours))
    elif current["comment"] != SCHEMA_MARKER:
        _migrate(client, database, table, retention_hours)
    elif f"toIntervalHour({int(retention_hours)})" not in current["engine_full"]:
        # Retention changed; new TTL applies to new parts and to old ones as they merge.
        client.execute(
            f"ALTER TABLE `{database}`.`{table}` MODIFY TTL toDateTime(Timestamp) + INTERVAL {int(retention_hours)} HOUR",
            extra_parameters={"materialize_ttl_after_modify": "0"},
        )
    _ensure_rollups(client, database, retention_hours)
    _ready.add(key)
    return True


def _ensure_rollups(client, database: str, retention_hours: int) -> None:
    from telemetry.rollups import ROLLUP_TABLE, rollup_ddl

    current = _table_info(client, database, ROLLUP_TABLE)
    if current is None:
        client.execute(rollup_ddl(database, retention_hours))
    elif f"toIntervalHour({int(retention_hours) + 1})" not in current["engine_full"]:
        client.execute(
            f"ALTER TABLE `{database}`.`{ROLLUP_TABLE}` MODIFY TTL hour + INTERVAL {int(retention_hours) + 1} HOUR",
            extra_parameters={"materialize_ttl_after_modify": "0"},
        )


def _migrate(client, database: str, table: str, retention_hours: int) -> None:
    staging = f"{table}_v{SCHEMA_VERSION}_staging"
    _check(database, staging)
    source, target = f"`{database}`.`{table}`", f"`{database}`.`{staging}`"
    columns = ", ".join(name for name, _ in EXPORTER_COLUMNS)
    window = f"Timestamp >= now() - INTERVAL {int(retention_hours)} HOUR"
    logger.warning("Migrating %s.%s to Runtime span schema v%s", database, table, SCHEMA_VERSION)
    # The exporter's trace-id lookup view also fires on every insert, and nothing reads its table.
    client.execute(f"DROP VIEW IF EXISTS `{database}`.`{table}_trace_id_ts_mv`")
    client.execute(f"DROP TABLE IF EXISTS `{database}`.`{table}_trace_id_ts`")
    client.execute(f"DROP TABLE IF EXISTS {target}")
    client.execute(span_table_ddl(database, staging, retention_hours))
    client.execute(f"INSERT INTO {target} ({columns}) SELECT {columns} FROM {source} WHERE {window}", timeout=600)
    copied_until = client.unscoped_rows(f"SELECT toString(max(Timestamp)) AS newest FROM {target}")
    newest = (copied_until[0].get("newest") if copied_until else None) or "1970-01-01 00:00:00"
    client.execute(f"EXCHANGE TABLES {source} AND {target}")
    # Spans the Collector wrote while the copy ran landed in the old table; carry them over.
    client.execute(
        f"INSERT INTO {source} ({columns}) SELECT {columns} FROM {target} "
        "WHERE Timestamp > parseDateTime64BestEffort({newest:String}, 9)",
        extra_parameters={"param_newest": newest}, timeout=600,
    )
    client.execute(f"DROP TABLE {target}")


def _table_info(client, database: str, table: str) -> dict | None:
    rows = client.unscoped_rows(
        "SELECT comment, engine_full FROM system.tables "
        f"WHERE database = '{database}' AND name = '{table}'"
    )
    return rows[0] if rows else None


def _check(*names: str) -> None:
    if not all(_IDENTIFIER.fullmatch(name) for name in names):
        raise ValueError("Invalid runtime span table configuration")


def reset_cache() -> None:
    _ready.clear()


def ensure_active_span_schema() -> bool:
    """Ensure the span table on the active storage target (Local or Remote)."""
    from telemetry.runtime import ClickHouseRuntime, runtime_sampling_policy

    client = ClickHouseRuntime()
    return ensure_span_schema(client, runtime_sampling_policy()["aggregation_retention_hours"])
