# Isolated telemetry development stack

This optional Phase 0 stack is separate from the application Compose project.
It runs a pinned OpenTelemetry Collector, ClickHouse, and an empty isolated
Neo4j test database. Collector and ClickHouse share an internal ingest network;
Neo4j is on a separate internal network and cannot receive telemetry. The
Collector exports only to ClickHouse. Do not connect application or production
SDKs to this fixture: its receiver intentionally accepts only synthetic test
identity values.

Start from `api/`:

```sh
docker compose -f docker-compose.telemetry.yml up -d
docker compose -f docker-compose.telemetry.yml ps
python3 telemetry-dev/smoke.py
```

Use non-default local passwords through `TELEMETRY_CH_PASSWORD` and
`TELEMETRY_NEO4J_PASSWORD` in your shell. Do not use production credentials.
Published ports bind to loopback only. Some Docker Engine versions do not
publish host ports for internal-only networks; the smoke test then accesses
services from their existing containers without adding a network or exposing a
Docker socket.

`smoke.py` sends synthetic secrets and verifies the persisted ClickHouse row
contains only the allow-listed identity, linked spans and events are absent,
the canary is nowhere in retained trace columns, the raw trace TTL is seven
days, the Neo4j test database remains empty, and the configured network boundary
matches the running containers.

`verify_mtls.py` is an optional cross-host receiver overlay check. It creates
short-lived throwaway certificates in a temporary directory and expects an
active Docker daemon. `collector-mtls.example.yaml` is a configuration example,
not a production deployment: production certificates, secret distribution,
network routing, and rotation remain operator work.

Stop services while preserving the local test data with:

```sh
docker compose -f docker-compose.telemetry.yml down
```
