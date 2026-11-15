#!/usr/bin/env python3
"""Send synthetic secrets, then inspect real storage and the isolated topology.

Python 3 standard library only. Run after the standalone compose stack is up.
Never point this canary at production or application databases.
"""

import base64
import copy
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.telemetry.yml"
CH_PASSWORD = os.environ.get("TELEMETRY_CH_PASSWORD", "telemetry-dev-only")
GRAPH_PASSWORD = os.environ.get("TELEMETRY_NEO4J_PASSWORD", "telemetry-dev-only")
IDENTITY = {"indexer.project_id": "telemetry-fixture",
            "vcs.ref.head.revision": "a" * 40,
            "indexer.symbol_id": r"App\Http\Controllers\OrderController::store",
            "indexer.dirty": False, "indexer.schema.version": "indexer-telemetry-v1"}


def request(url, body, user=None, password=None, service="clickhouse"):
    headers = {"Content-Type": "application/json"}
    if user:
        credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {credentials}"
    # Internal-only Docker bridges may suppress published ports (Engine 29 does).
    # Use each existing container's wget; no extra network or Docker socket mount.
    command = ["docker", "compose", "-f", str(COMPOSE), "exec", "-T", service,
               "wget", "-qO-", "--timeout=5", "--post-data=" + body.decode()]
    command.extend("--header=" + key + ": " + value for key, value in headers.items())
    result = subprocess.run([*command, url], capture_output=True, timeout=15)
    if result.returncode:
        raise ConnectionError(f"{service} HTTP request failed (exit {result.returncode})")
    return result.stdout.decode()


def clickhouse(sql):
    return request("http://127.0.0.1:8123/", sql.encode(), "telemetry", CH_PASSWORD)


def graph_counts():
    body = {"statements": [{"statement": "MATCH (n) RETURN count(n) AS nodes"},
                           {"statement": "MATCH ()-[r]->() RETURN count(r) AS relationships"}]}
    result = json.loads(request("http://127.0.0.1:7474/db/neo4j/tx/commit",
                                json.dumps(body).encode(), "neo4j", GRAPH_PASSWORD, "neo4j"))
    if result.get("errors"):
        raise RuntimeError("Neo4j count query failed")
    return [entry["data"][0]["row"][0] for entry in result["results"]]


def wait_for(check, description, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result is not None:
                return result
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(1)
    raise RuntimeError(f"Timed out waiting for {description}")


def verify_topology():
    result = subprocess.run(["docker", "compose", "-f", str(COMPOSE), "config", "--format", "json"],
                            check=True, capture_output=True, text=True)
    config = json.loads(result.stdout)
    assert set(config["services"]) == {"collector", "clickhouse", "neo4j"}
    assert all(network["internal"] for network in config["networks"].values())
    ingest = set(config["services"]["collector"]["networks"])
    graph = set(config["services"]["neo4j"]["networks"])
    assert ingest.isdisjoint(graph), "Collector must not join the graph network"
    for service in config["services"].values():
        assert all(port["host_ip"] == "127.0.0.1" for port in service.get("ports", []))
    collector = (ROOT / "telemetry-dev/collector.yaml").read_text()
    assert "exporters: [clickhouse]" in collector
    assert "qdrant" not in collector.lower() and "neo4j" not in collector.lower()
    # Inspect the running containers too: extra attachments cannot hide in source config.
    ids = subprocess.run(["docker", "compose", "-f", str(COMPOSE), "ps", "-q"],
                         check=True, capture_output=True, text=True).stdout.split()
    assert len(ids) == 3, "Start exactly the three isolated services first"
    running = json.loads(subprocess.run(["docker", "inspect", *ids], check=True,
                                       capture_output=True, text=True).stdout)
    expected_networks = {network["name"] for network in config["networks"].values()}
    for container in running:
        actual = set(container["NetworkSettings"]["Networks"])
        assert actual <= expected_networks, "Unexpected external network attachment"
        service = container["Config"]["Labels"]["com.docker.compose.service"]
        required = {config["networks"][name]["name"] for name in config["services"][service]["networks"]}
        assert actual == required, "Running network membership differs from compose"
        for bindings in container["NetworkSettings"]["Ports"].values():
            assert all(binding["HostIp"] == "127.0.0.1" for binding in bindings or [])
    return config


def fixture(canary, trace_id, span_id):
    def attribute(key, value=canary):
        return {"key": key, "value": {"stringValue": value}}

    now = time.time_ns()
    span = {
        "traceId": trace_id, "spanId": span_id, "name": canary, "kind": 2,
        "traceState": "vendor=" + canary,
        "startTimeUnixNano": str(now), "endTimeUnixNano": str(now + 1000000),
        "status": {"code": 2, "message": canary},
        "attributes": [attribute(key) for key in [
            "db.statement", "db.query.text", "exception.message", "exception.stacktrace",
            "http.request.body", "http.response.body", "http.request.header.authorization",
            "http.request.header.cookie", "cache.key", "url.full", "unknown.secret",
            "unknown.schema.version", "unknown.test.case"]],
        "events": [{"name": canary, "timeUnixNano": str(now),
                    "attributes": [attribute("exception.message"), attribute("unknown.secret")]}],
    }
    span["attributes"].extend({"key": key, "value": {"boolValue" if isinstance(value, bool) else "stringValue": value}}
                              for key, value in IDENTITY.items())
    linked = copy.deepcopy(span)
    linked["spanId"] = secrets.token_hex(8)
    linked["links"] = [{"traceId": secrets.token_hex(16), "spanId": secrets.token_hex(8),
                        "traceState": "vendor=" + canary,
                        "attributes": [attribute("unknown.secret")]}]
    payload = {"resourceSpans": [{
        "schemaUrl": "https://invalid.example/" + canary,
        "resource": {"attributes": [attribute("service.name", "telemetry-canary"),
                                     attribute("service.namespace", "devassistant"),
                                     attribute("deployment.environment.name", "development"),
                                     attribute("unknown.secret")]},
        "scopeSpans": [{"schemaUrl": "https://invalid.example/" + canary,
                        "scope": {"name": canary, "version": canary,
                                  "attributes": [attribute("unknown.secret")]},
                        "spans": [span, linked]}]}]}
    return payload


def main():
    verify_topology()
    wait_for(lambda: clickhouse("SELECT 1"), "ClickHouse")
    before = wait_for(graph_counts, "Neo4j")
    assert before == [0, 0], "Phase0 isolated graph must start empty"
    canary = "TELEMETRY_SECRET_" + secrets.token_hex(16)
    trace_id, span_id = secrets.token_hex(16), secrets.token_hex(8)
    response = json.loads(request("http://collector:4318/v1/traces",
                                  json.dumps(fixture(canary, trace_id, span_id)).encode()) or "{}")
    assert not response.get("partialSuccess", {}).get("rejectedSpans", 0)

    def read_rows():
        # Only random hexadecimal IDs enter SQL; secret canary never enters a query log.
        body = clickhouse(f"SELECT * FROM telemetry.otel_traces WHERE TraceId = '{trace_id}' FORMAT JSONEachRow")
        return [json.loads(line) for line in body.splitlines()] or None

    rows = wait_for(read_rows, "sanitized stored span")
    # Wait beyond the batch timeout to also detect accidental linked-span persistence.
    time.sleep(2)
    rows = read_rows()
    assert len(rows) == 1, "The span with a link must be rejected"
    row = rows[0]
    assert row["SpanId"] == span_id and row["SpanName"] == "telemetry.dev.span"
    assert row["StatusMessage"] == "" and row["TraceState"] == ""
    assert row["ServiceName"] == "telemetry-canary"
    assert row["ScopeName"] == "devassistant.telemetry.phase0" and row["ScopeVersion"] == "1"
    assert row["ResourceAttributes"] == {"service.name": "telemetry-canary",
                                          "service.namespace": "devassistant",
                                          "deployment.environment.name": "development"}
    assert row["SpanAttributes"] == {key: "false" if value is False else value for key, value in IDENTITY.items()}
    assert all(value == [] for key, value in row.items() if key.startswith(("Events.", "Links.")))
    assert canary not in json.dumps(rows), "Canary survived in persisted payload"
    # Scan all retained trace rows, including every event/link/resource/scope field.
    all_rows = clickhouse("SELECT * FROM telemetry.otel_traces FORMAT JSONEachRow")
    assert canary not in all_rows, "Canary appeared in another persisted span"
    tables = [json.loads(line) for line in clickhouse(
        "SELECT name, engine, create_table_query FROM system.tables WHERE database = 'telemetry' FORMAT JSONEachRow").splitlines()]
    trace_tables = [table for table in tables if table["name"].startswith("otel_traces") and "MergeTree" in table["engine"]]
    assert len(trace_tables) >= 2, "Expected raw spans and trace-ID lookup tables"
    for table in trace_tables:
        ddl = table["create_table_query"]
        assert "TTL" in ddl and ("toIntervalSecond(604800)" in ddl or "toIntervalDay(7)" in ddl), table["name"]
    assert graph_counts() == before == [0, 0], "Phase0 must not write graph facts"
    print(json.dumps({"result": "PASS", "stored_sanitized_spans": len(rows),
                      "linked_span_rejected": True, "canary_absent_all_trace_columns": True,
                      "retention_seconds": 604800, "ttl_tables": [t["name"] for t in trace_tables],
                      "neo4j_nodes": 0, "neo4j_relationships": 0,
                      "networks_internal": True, "ports_loopback_only": True,
                      "qdrant_exporter_or_network": False}, indent=2))


if __name__ == "__main__":
    main()
