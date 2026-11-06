import pytest

from telemetry import span_schema
from telemetry.span_schema import SCHEMA_MARKER, ensure_span_schema, span_table_ddl


class _ClickHouse:
    configured = True
    url = "http://clickhouse:8123"
    database = "telemetry"
    table = "otel_spans_full"

    def __init__(self, table_info=None):
        self.table_info = table_info
        self.statements: list[str] = []

    def execute(self, sql, body=None, extra_parameters=None, timeout=15):
        self.statements.append(sql)

    def unscoped_rows(self, sql):
        if "system.tables" in sql:
            return [self.table_info] if self.table_info else []
        if "max(Timestamp)" in sql:
            return [{"newest": "2026-11-06 10:00:00.000000000"}]
        return []


@pytest.fixture(autouse=True)
def _fresh_cache():
    span_schema.reset_cache()
    yield
    span_schema.reset_cache()


def test_ddl_sorts_by_scope_columns_and_uses_the_retention_hours():
    ddl = span_table_ddl("telemetry", "otel_spans_full", 72)

    assert "ORDER BY (ProjectId, RevisionId, SpanKind, Timestamp)" in ddl
    assert "TTL toDateTime(Timestamp) + INTERVAL 72 HOUR" in ddl
    assert "ProjectId LowCardinality(String) MATERIALIZED ResourceAttributes['indexer.project_id']" in ddl
    assert "Eligible Bool MATERIALIZED" in ddl
    assert f"COMMENT '{SCHEMA_MARKER}'" in ddl


def test_ddl_rejects_unsafe_identifiers():
    with pytest.raises(ValueError):
        span_table_ddl("telemetry", "spans; DROP TABLE x", 72)


def test_missing_table_is_created_once_per_target():
    client = _ClickHouse()

    assert ensure_span_schema(client, 72) is True
    assert ensure_span_schema(client, 72) is True

    creates = [sql for sql in client.statements if sql.startswith("CREATE TABLE")]
    assert len(creates) == 1
    assert "`telemetry`.`otel_spans_full`" in creates[0]


def test_exporter_created_table_is_copied_and_swapped():
    client = _ClickHouse({"comment": "", "engine_full": "MergeTree ORDER BY (ServiceName, SpanName)"})

    ensure_span_schema(client, 72)

    kinds = [" ".join(sql.split()[:2]) for sql in client.statements]
    assert kinds == [
        "CREATE DATABASE",
        "DROP VIEW",
        "DROP TABLE",
        "DROP TABLE",
        "CREATE TABLE",
        "INSERT INTO",
        "EXCHANGE TABLES",
        "INSERT INTO",
        "DROP TABLE",
    ]
    copy = client.statements[5]
    assert copy.startswith("INSERT INTO `telemetry`.`otel_spans_full_v2_staging` (Timestamp, TraceId")
    assert "WHERE Timestamp >= now() - INTERVAL 72 HOUR" in copy
    assert "ProjectId" not in copy  # materialized columns are derived, never copied
    assert client.statements[-1] == "DROP TABLE `telemetry`.`otel_spans_full_v2_staging`"


def test_current_table_with_a_new_retention_only_changes_the_ttl():
    client = _ClickHouse({"comment": SCHEMA_MARKER, "engine_full": "MergeTree TTL toDateTime(Timestamp) + toIntervalHour(72)"})

    ensure_span_schema(client, 48)

    assert client.statements[-1].startswith("ALTER TABLE `telemetry`.`otel_spans_full` MODIFY TTL")
    assert "INTERVAL 48 HOUR" in client.statements[-1]


def test_current_table_needs_no_change():
    client = _ClickHouse({"comment": SCHEMA_MARKER, "engine_full": "MergeTree TTL toDateTime(Timestamp) + toIntervalHour(72)"})

    ensure_span_schema(client, 72)

    assert client.statements == ["CREATE DATABASE IF NOT EXISTS `telemetry`"]


def test_unconfigured_storage_is_not_ready():
    client = _ClickHouse()
    client.configured = False

    assert ensure_span_schema(client, 72) is False
    assert client.statements == []
