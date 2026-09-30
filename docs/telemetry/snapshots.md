# Commit-scoped PHP source snapshots (Phase 0)

Snapshot ingestion is opt-in. Existing filename-based collections and saved
analyses remain readable without inferred project IDs or commits. Runtime
evidence must never join those legacy collections.

## Identity

Canonical strings are defined by `Python-Service/php/tests/telemetry/README.md`
and its shared golden fixtures. PHP `App\Telemetry\SymbolId` and Python
`telemetry.symbols` own normalization. `symbol_id` is readable; graph storage IDs
are SHA256 of compact UTF-8 JSON `[project_id, commit_sha, symbol_id]`. Qdrant
uses UUID5(URL namespace, graph storage ID). Neither concatenation nor filename
is a snapshot identity. Non-runtime class/property summaries use a separate
typed internal key and do not advertise a runtime `symbol_id`.

The project ID is a provisioned, case-sensitive ASCII slug (1–128 characters).
Commit SHA is full lowercase Git SHA1 (40) or SHA256 (64). Archive checksum is
SHA256 of the exact uploaded ZIP bytes. Snapshot metadata carries
`project_id`, `commit_sha`, `source_archive_sha256`, `dirty=false`, and
`telemetry_schema_version=indexer-telemetry-v1` in graph/vector properties and
the SQLite registry. AST schema version remains separate.

Two declarations with one canonical ID fail indexing; they are never merged.
Traits retain declaration IDs, and consuming-class IDs require explicit facts.
Two closures on one file/line are ambiguous. Unsupported/invalid identities are
reported. Static `CALLS` stays distinct from future `RUNTIME_CALLS`.

## CI upload API

Provision `TELEMETRY_CI_PROJECT_TOKENS` on the backend as a JSON object mapping
project IDs to separate high-entropy bearer tokens, each at least 32 characters.
There is no public token-minting endpoint, default token, or reuse of the legacy
upload JWT secret. Missing or malformed configuration fails closed. Protect the
API with HTTPS and use masked, protected CI variables; do not log auth headers.
Set `TELEMETRY_SNAPSHOT_ROOT` to a durable local POSIX filesystem directory
shared by backend worker processes (default sibling of workspace root).

`GET /telemetry/projects/{project_id}/snapshots/{commit_sha}` returns
`awaiting_source` when no matching source has arrived. A source archive without
a live matching deployment reports `awaiting_deployment`; once both exist it
reports `queued`, then `indexing`, and only publishes `ready` after strict
parser, vector, graph and inspection persistence checks succeed. Auth is
project scoped.

`PUT /telemetry/projects/{project_id}/snapshots/{commit_sha}/source` accepts raw
repository-root ZIP bytes with `Content-Type: application/zip` and
`Authorization: Bearer <project token>`. Root paths must match the deployed
repository: no enclosing checkout folder. Create once and reuse those exact
bytes on retries. CI attests the commit/source match; the server cannot prove
that an arbitrary archive came from Git. Archive generated sources must match
the deployed build; omit secrets, vendor and node_modules. No uploaded PHP is
executed, no Composer scripts run, and no application container is managed.

Example CI shell, using existing protected variables:

```sh
git archive --format=zip --output=source.zip "$CI_COMMIT_SHA"
curl --fail-with-body --request PUT \
  --header "Authorization: Bearer $INDEXER_CI_TOKEN" \
  --header 'Content-Type: application/zip' \
  --data-binary @source.zip \
  "$INDEXER_API/telemetry/projects/$INDEXER_PROJECT/snapshots/$CI_COMMIT_SHA/source"
```

Uploads stage exact bytes in MinIO and return the pending state without waiting
for indexing. A durable SQLite job is drained by the backend lifespan worker;
the shared snapshot directory and process lock let a restarted worker reclaim
an interrupted job without two backend processes indexing the same snapshot.
Failed indexing never returns `ready`; retry the exact bytes to resume after a
failure. GET can observe each pending state without exposing the archive key or
indexing diagnostics.

Ready retries perform no writes. Different bytes for the same clean
project/commit return 409, including after a failed attempt. The existing MinIO
storage client persists archives in `telemetry-sources/<scope>/<checksum>.zip`,
outside mutable legacy upload lists. Raw upload size is capped at 64 MiB,
expanded contents at 256 MiB and entries at 20,000; traversal, duplicate paths,
symlinks, encrypted entries and corrupt ZIPs are rejected.

Only successful complete parsing, graph/vector ingestion and inspection graph
ingestion publish `ready`. Failed snapshots may leave staged graph/vector rows;
they are excluded from legacy discovery and readiness-gated retrieval. Ready
collections are returned explicitly by status. Snapshot retry upserts its own
collection; it never clears legacy data or another commit's data. Unmatched,
unindexed archive bytes expire after 30 days; checksum and state metadata remain
so the exact archive can be reuploaded. Stopped deployment mappings remain for
seven days. Ready snapshots and saved analyses are retained by default; explicit
Runtime project removal is defined below.

## Host deployment discovery

See `../telemetry-agent/README.md` for the per-host read-only Docker API proxy,
opt-in labels, agent deployment, and backend host-scope configuration. The
backend accepts deployment reports at `POST /telemetry/host/deployments`; host
identity is taken only from `X-Verified-Client-Cert-SHA256`. Configure ingress
to require and verify client certificates, strip any incoming copy of that
header, and set it from the verified peer certificate fingerprint. Do not
expose the backend route directly to hosts or the public network. Production
discovery remains disabled until that ingress identity path is verified end
to end.

CI uploads accept clean snapshots only. Dirty development telemetry is marked
dirty and approximately attributed to the nearest clean commit in later
phases; it must not modify that snapshot or appear as exact production evidence.
No observed activity carries forward to a new commit.

## Phase boundary and verification

Tests: `test_telemetry_symbols.py`, `test_telemetry_source.py`,
`test_telemetry_persistence.py`; PHP scripts under `php/tests/telemetry`.
Routes/tables are explicit static fact fixtures in the Phase 0 golden gate;
framework introspection commands and runtime trait mappings belong to Phase 2.
The optional stack is `docker-compose.telemetry.yml`; see `telemetry-dev/README.md`.
Source indexing requires the existing MinIO, Neo4j, Qdrant and embedding backend;
the optional raw telemetry stack does not replace the app's data stores.

## Phase 1 runtime read API

The opt-in `runtime` profile in the main `docker-compose.yml` adds ClickHouse
and a privacy-filtering OTLP Collector while reusing the existing Neo4j service.
The Python API reads scoped aggregates from ClickHouse through
`GET /telemetry/runtime/projects/{project_id}/snapshots/{commit_sha}`; the
authenticated Express API exposes the same data under `/api/runtime`. The
internal routes require `RAG_CALLBACK_SECRET`, and browser access is scoped by
the persisted project membership role. `/api/runtime/settings` is admin-only.
Raw spans remain in ClickHouse. The read path only joins symbol IDs to the
immutable `sym_snapshot_<scope>` symbol collection; it does not write telemetry
edges into Neo4j or Qdrant.

For local development, configure the `TELEMETRY_*` variables from
`.env.example`, keep `TELEMETRY_RECORDING_RATIO` and
`TELEMETRY_RECORDING_PERCENT` aligned, then start `docker compose --profile
runtime up -d`. Configure `TELEMETRY_RECORDING_PERCENT` to set sampling.
Application exporters must send allow-listed identity and route
/ database fields to `http://localhost:24318/v1/traces` from the host, or
`http://host.docker.internal:24318/v1/traces` from a Docker container. No
application method instrumentation is bundled: each PHP application must emit
canonical symbol IDs and the current full commit SHA. The Runtime tab reports
no observations until those spans arrive. Do not expose this local receiver to
the network; production ingress and exporter credentials need separate
deployment configuration.

## Local Runtime project containers

The authenticated Runtime UI may provide a local-container removal action for
the selected project ID. The selected commit is only a view filter: the action
matches all local containers for that project across commits. Only a project
administrator may start it. A short-lived, origin-validated helper
authorization is scoped to the project.

One native grouped checklist approval covers stopping running and removing
running or stopped containers on the local Docker daemon that carry both exact
labels `com.devassistant.telemetry.enabled=true` and
`com.devassistant.telemetry.project=<project_id>`. The helper verifies that its
Docker context uses a local socket or named pipe, rechecks both labels before
each mutation, and reports partial failures. The dialog identifies the
project and local container count. Continue remains unavailable until every
required item is checked.

The action removes containers only. It leaves images, volumes, networks,
remote containers, Runtime snapshots and spans, source archives, indexes,
deployment history, memberships, CI credentials, Compose files, host/project
authorization, certificates, and the local helper setup recovery record
untouched. The project can therefore remain in the Runtime dropdown because
stored snapshots or remote deployment reports remain; a later local Compose
start can also recreate the local containers. The host agent's Docker proxy
remains read-only. Removing a container discards its writable layer; named
volumes and images remain. Use Clean up Runtime separately to restore Compose
files and remove the local host/project setup scope.

After the helper operation, the UI refreshes Runtime inventory and preserves
the project selection. A later inventory report closes removed-container
deployment records; Runtime data is retained by this action.
