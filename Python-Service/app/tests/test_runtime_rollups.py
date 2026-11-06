from telemetry import rollups


class _ClickHouse:
    database = "telemetry"
    table = "otel_spans_full"
    window_hours = 24

    def __init__(self, pending=None):
        self.pending = pending or []
        self.queries: list[str] = []
        self.executed: list[tuple[str, dict]] = []

    def rows(self, sql, _project_id, _revision_id, _extra=None):
        self.queries.append(sql)
        if "SELECT DISTINCT toString(toStartOfHour(Timestamp)) AS hour" in sql:
            return [{"hour": hour} for hour in self.pending]
        return [
            {"kind": "symbol", "k1": "App\\A::run", "calls": 2},
            {"kind": "unknown", "k1": "x", "calls": 1},
        ]

    def execute(self, sql, body=None, extra_parameters=None, timeout=15):
        self.executed.append((sql, extra_parameters or {}))


def test_only_settled_hours_without_a_marker_are_rolled_up():
    client = _ClickHouse(pending=["2026-11-06 10:00:00", "2026-11-06 11:00:00"])

    assert rollups.roll_up_closed_hours(client, "pizza", "rev_a") == 2

    pending_query = client.queries[0]
    assert f"INTERVAL {rollups.SETTLE_MINUTES} MINUTE" in pending_query
    assert "kind = 'hour'" in pending_query
    sql, parameters = client.executed[0]
    assert sql.startswith("INSERT INTO `telemetry`.`runtime_span_rollups`")
    assert parameters == {"param_project_id": "pizza", "param_revision_id": "rev_a", "param_hour": "2026-11-06 10:00:00"}
    for kind in rollups.KINDS + ("hour",):
        assert f"'{kind}' AS kind" in sql
    # Parent and server spans are looked up from one hour earlier, so boundary-crossing requests still join.
    assert "toDateTime({hour:String}) - INTERVAL 1 HOUR" in sql


def test_rollup_source_names_materialized_columns_explicitly():
    selects = rollups._kind_selects("`telemetry`.`otel_spans_full`", "1", "1")

    assert all("SELECT *" not in select for select in selects)
    assert all("SymbolId, HttpRoute, " in select for select in selects)
    # The head-sampling ratio is carried into the 'all' rows so the summary can scale counts.
    assert "SamplingRatio AS k1" in selects[0]


def test_summary_rows_are_grouped_by_known_kind():
    client = _ClickHouse()

    grouped = rollups.read_summary(client, "pizza", "rev_a")

    assert set(grouped) == set(rollups.KINDS)
    assert grouped["symbol"] == [{"kind": "symbol", "k1": "App\\A::run", "calls": 2}]
    assert len(client.queries) == 1


def test_invalidating_hours_deletes_their_rollups_with_parameters():
    client = _ClickHouse()

    rollups.invalidate_hours(client, "pizza", "rev_a", ["2026-11-06 10:00:00"])
    rollups.invalidate_hours(client, "pizza", "rev_a", [])

    assert len(client.executed) == 1
    sql, parameters = client.executed[0]
    assert sql.startswith("DELETE FROM `telemetry`.`runtime_span_rollups` WHERE project_id = {project_id:String}")
    assert "hour IN (toDateTime({hour_0:String}))" in sql
    assert parameters["param_hour_0"] == "2026-11-06 10:00:00"
    assert parameters["lightweight_deletes_sync"] == "2"
