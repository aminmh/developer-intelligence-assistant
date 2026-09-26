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
`awaiting_source` when no matching source has arrived. Auth is project scoped.

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

Response states: `awaiting_source → storing → indexing → ready`, or `failed`.
Uploads currently execute indexing synchronously in the backend thread pool;
configure proxy/CI request timeouts for the project size. GET can observe state
while indexing. HTTP 503 reports failed indexing without source/error-message
contents. Retry the exact bytes to resume after failure or backend interruption.
Per-snapshot process locks prevent concurrent retries from double-indexing.

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
collection; it never clears legacy data or another commit's data. Retention or
deletion of snapshots needs a separate reviewed lifecycle policy.

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
