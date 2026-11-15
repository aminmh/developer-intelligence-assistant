import pytest

import telemetry.profiles as profiles
from telemetry.profiles import store_profiles

PROJECT = "pizza"
REVISION = "rev_" + "a" * 32
COMMIT = "b" * 40


def _profile(index: int = 1) -> dict:
    return {
        "project_id": PROJECT,
        "revision_id": REVISION,
        "trace_id": f"{index:032x}",
        "span_id": f"{index:016x}",
        "profile_id": f"prof_{index:024x}",
        "event_type": "EXCIMER_REAL",
        "duration_ms": 12.5,
        "sample_period_ms": 5.0,
        "samples_collapsed": "App\\Http\\Controller::index;App\\Service::run 3",
        "environment": "development",
        "service_name": "pizza-builder",
        "top_frames": ["App\\Service::run"],
        "resource_attributes": {
            "indexer.project_id": PROJECT,
            "indexer.revision_id": REVISION,
            "deployment.environment.name": "development",
            "indexer.schema.version": "indexer-telemetry-v2",
            "indexer.code_dirty": False,
        },
    }


class _ClickHouse:
    configured = True
    database = "telemetry"
    url = "http://clickhouse:8123"

    def __init__(self, fail_inserts: int = 0):
        self.calls: list[tuple[str, dict | None]] = []
        self.fail_inserts = fail_inserts

    def execute(self, sql, body=None, extra_parameters=None, timeout=15):
        self.calls.append((sql, extra_parameters))
        if sql.startswith("INSERT") and self.fail_inserts:
            self.fail_inserts -= 1
            raise RuntimeError("UNKNOWN_TABLE")

    def statements(self) -> list[str]:
        return [sql.split(" ", 2)[0] for sql, _ in self.calls]


@pytest.fixture(autouse=True)
def _fresh_table_cache(monkeypatch):
    monkeypatch.setattr(profiles, "_ensured_tables", set())
    monkeypatch.setattr(profiles, "runtime_sampling_policy", lambda: {"aggregation_retention_hours": 72})


def test_profiles_use_a_fire_and_forget_async_insert():
    client = _ClickHouse()

    assert store_profiles(client, PROJECT, REVISION, COMMIT, [_profile()]) == 1

    insert_sql, settings = client.calls[-1]
    assert insert_sql.startswith("INSERT INTO `telemetry`.`otel_profiles`")
    assert settings["async_insert"] == "1"
    assert settings["wait_for_async_insert"] == "0"
    assert settings["async_insert_busy_timeout_max_ms"] == "5000"


def test_profile_table_is_created_once_per_process():
    client = _ClickHouse()

    store_profiles(client, PROJECT, REVISION, COMMIT, [_profile(1)])
    store_profiles(client, PROJECT, REVISION, COMMIT, [_profile(2)])

    assert client.statements() == ["CREATE", "INSERT", "INSERT"]


def test_profile_table_is_recreated_when_an_insert_fails_after_caching():
    client = _ClickHouse()
    store_profiles(client, PROJECT, REVISION, COMMIT, [_profile(1)])
    client.fail_inserts = 1

    assert store_profiles(client, PROJECT, REVISION, COMMIT, [_profile(2)]) == 1

    assert client.statements() == ["CREATE", "INSERT", "INSERT", "CREATE", "INSERT"]


def test_invalid_profile_is_rejected_before_any_clickhouse_call():
    client = _ClickHouse()
    broken = _profile()
    broken["sample_period_ms"] = 10.0

    with pytest.raises(ValueError):
        store_profiles(client, PROJECT, REVISION, COMMIT, [broken])

    assert client.calls == []
