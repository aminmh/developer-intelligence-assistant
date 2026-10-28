from __future__ import annotations

import json

from telemetry import runtime


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({"ok": 1}).encode()


def test_unscoped_clickhouse_query_uses_basic_auth(monkeypatch):
    client = runtime.ClickHouseRuntime.__new__(runtime.ClickHouseRuntime)
    client.url = "http://clickhouse:8123"
    client.user = "user"
    client.password = "pass"
    client.database = "runtime"
    client.ssl_context = None
    captured = {}

    def fake_urlopen(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(runtime, "urlopen", fake_urlopen)

    assert client.unscoped_rows("SELECT 1") == [{"ok": 1}]
    assert captured == {"authorization": "Basic dXNlcjpwYXNz", "timeout": 10}
