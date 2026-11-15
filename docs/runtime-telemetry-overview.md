# Runtime telemetry

### Runtime telemetry

The Runtime tab shows opted-in local containers and recent sampled requests for the selected indexed
revision. The Containers panel appears above and outside the Recent sampled requests panel. The
request list shows up to 50 recent samples, 10 per page. An **All / Errors / Slow** filter narrows the
list before the 50-row limit: Errors keeps failed requests and 5xx responses; Slow keeps requests at or
above their own route's p95 server duration in the window (routes with fewer than 20 requests use the
revision-wide p95, which the filter hint shows). When the PHP app records only a share of requests
(head sampling, 10% in production by default), the list says so and how much real traffic it stands
for; when the app does not report its ratio, the list says it may be a sample. The request's **Database** tab starts with
a summary (query count, database time and its share of the request, the slowest query) and groups queries by shape:
operation, tables, and the application method that issued them. A shape that runs 3 or more times in one request is
listed under **Repeated queries**, usually an N+1 query or a cache read per call; because SQL text is not stored,
queries in one shape can still differ in columns or conditions. Shapes that only touch framework tables (sessions,
cache, queues, migrations, Telescope, Pulse, Horizon) and were not issued by application code are marked **Framework**,
and the summary splits app and framework queries; app code reading such a table (a settings cache read, say) still
counts as app work. **Explain with AI** in the same tab runs the request
analysis, which now also receives these groups (counts, tables, timings and method IDs, never SQL text) and the source
of the methods behind repeated queries first; its database findings appear in the tab, the matching shapes are marked
**AI finding**, and **Preview a code change** opens the finding in the AI analysis tab. When the model call fails, the API logs the
provider and the underlying error (for example an HTTP 403 from the remote host or a refused local connection),
never the prompt. Open Runtime views refresh on `runtime.updated` events; each event stream sends at most one
event every 5 seconds (later changes are folded into it), and a host agent's deployment report sends one only when
a deployment actually changed, so an open view does not make ClickHouse rebuild the summary on every ingest. Expanding a request shows its database calls,
safe operation and database metadata, table IDs, attributed application method, and bounded per-request
method caller/callee counts when available. The counts are observations from configured hooks; they do
not show execution order or complete method coverage.
Indexed source paths are stored relative to the project root, allowing captured method IDs to map to
files in the exact source revision for AI diff previews.
Request rows may also show PHP worker CPU-time and PHP-managed peak-memory estimates; these are not
whole-container resource measurements. Clear hides the loaded rows in the current view. Project
administrators can separately delete the stored traces for up to 50 loaded requests; this also
removes their spans from Runtime aggregates and requires confirmation. The list currently loads at
most 50 samples and paginates those loaded rows 10 at a time; it does not request additional pages
from the telemetry store. Coverage depends on the application's sampling and
instrumentation. Runtime details are scoped to the selected project and revision, and raw SQL,
bindings, request bodies, and headers are not returned. Older spans or unsupported PHP runtimes may
not include every field. Request details say when data is incomplete: a trace with more than 500
spans shows only the first 500, and the Methods tab reports how many method calls the PHP library
could not record because a request exceeded its 256-edge or 128-frame call-depth limit. A failed
request's exception class (`error_type`) is included in the detail response.

When Remote ClickHouse storage is selected, the Runtime tab also shows current
CPU, RAM, load average, and free/total disk for `/var/lib/clickhouse` on the
configured SSH host. SSH and ClickHouse health are reported separately, with
the sample time shown. The view polls every five seconds while the tab is
visible and stops when hidden or closed. CPU stays in a warming state until a
counter delta is available. If SSH or the OS keyring becomes unavailable, the
view shows an actionable message and labels the last successful sample stale;
ClickHouse failure does not hide host metrics. Local and Disabled modes have no
remote host monitor. The API uses the saved fingerprint-verified SSH identity,
runs a fixed read-only query without sudo, and keeps only the latest sample and
CPU counters in memory.

App pages, configuration panels, dialogs, toasts, and controls use brief
transitions. The application honors the operating system's reduced-motion
preference, and changing host metric values are not animated.

Full-stack startup (`just up` and `just up-dev`) reads the saved Runtime storage mode before starting
the telemetry backend. Local starts ClickHouse and waits for its health check before verifying it
and starting the Collector. Remote verifies ClickHouse through the SSH tunnel without starting the
local ClickHouse. Disabled stops local ClickHouse and the Collector while preserving stored data.
The API, router, and SSH tunnel remain available for Settings and remote provisioning. The Collector
starts only after the selected backend verifies successfully; startup stops on failure and never
falls back to another backend. Use `just runtime-storage local`, `just runtime-storage remote`, or
`just runtime-storage disabled` to start, verify, and select a mode explicitly. If a backend is
stopped, Settings shows the matching command to run before retrying the mode switch. This keeps the
2 GiB ClickHouse container from consuming resources while Remote or Disabled is selected. Runtime
ClickHouse restarts after a process exit unless it was manually stopped; repeated OOM exits still
require investigation despite the configured memory limits. When starting API services separately,
run the mode-aware orchestrator after the API is up:
`python3 telemetry-dev/runtime_stack.py start` from `api/`.
Runtime ClickHouse is capped at 1.2 GiB of tracked server memory and 256 MiB per query, with small
cache ceilings for this local telemetry workload. The settings are maintained in
`api/telemetry-dev/clickhouse-memory.xml` and
`api/telemetry-dev/clickhouse-users-memory.xml`.
Selecting Disabled applies even if the Runtime Collector or its restart relay is stopped; the next Collector start reads the Disabled configuration. Existing telemetry data is preserved.
Settings & Connection also lets administrators select Disabled, Local, or Remote ClickHouse storage. Local remains the default. Remote setup connects over SSH, verifies the host fingerprint and sudo access, then installs ClickHouse on supported Ubuntu or Debian hosts. When enabling a saved Remote target while its bridge is running, the app updates the router and SSH tunnel target, then verifies ClickHouse through that tunnel; a failed verification restores the previous target. If a selected backend is stopped, run the command shown in Settings (for example, `just runtime-storage local`) to start and verify it. The desktop SSH helper must be running for key status and SSH operations; if it is unavailable, restart Developer Assistant with `just up-dev` and refresh credential status. The remote ClickHouse HTTP (8123) and native (9000) listeners stay bound to loopback; only the configured SSH port needs network access. The desktop helper resolves direct host entries and aliases from `~/.ssh/config` and stores the SSH key and optional passphrase in macOS Keychain or Linux Secret Service. On macOS, the helper uses Security.framework directly and sends credential data over stdin, never in process arguments. A restricted Runtime tunnel sidecar maintains the SSH tunnel. macOS uses Docker Desktop's forwarded desktop SSH agent; Linux uses a dedicated Runtime agent. On macOS, the API and tunnel containers can request signatures from every identity currently loaded in the forwarded desktop agent, not just the Runtime key. The helper only adds or removes the Runtime identity and preserves other macOS identities. Private key bytes remain in the OS credential store and are not sent to containers. SSH `ProxyJump` and `ProxyCommand` aliases are not supported. Unlock the OS keyring and retry when it is locked. The remote database uses persistent storage and the configured Runtime credentials; set `TELEMETRY_CLICKHOUSE_PASSWORD` in `api/.env` to a strong password of at least 32 characters before setup. Switching targets never copies or deletes telemetry and does not change PHP capture or sampling. The Collector's bounded in-memory queue is not durable across process failure. See the [Runtime telemetry guide](../api/docs/telemetry/runtime-phase3.md#clickhouse-storage-target) for setup and recovery steps.
For SSH setup, paste the full private key including its `BEGIN` and `END` lines. The SSH port
defaults to 22; change it only when the server uses a custom port. Fingerprint retrieval does not
use the private key. Save the key to the desktop credential store before testing SSH and sudo; the
test button remains disabled until the credential status is Ready. Pasted CRLF line endings and
outer whitespace are normalized.

Instrumented PHP projects must send the Developer Assistant project ID and immutable indexed
revision ID. See [installation guide](installation.md) and the
[Runtime telemetry guide](../api/docs/telemetry/runtime-phase3.md) for setup details.
Runtime live updates are disabled by default and can be enabled with the Auto-refresh toggle; the
manual Refresh button remains available while live updates are disabled. Automatic Runtime refreshes
keep the currently expanded request and its loaded database-call details open while the same project
revision remains selected. Boolean controls previously shown as checkboxes use the card-style On/Off
toggle from Settings & Connection, with a visible focus state and keyboard-operable button behavior.
Runtime header actions, the project selector, and Auto-refresh share a consistent 40 px control height.

Expanded requests can also be analyzed on demand with the app's selected chat provider. Before
submission, Runtime lists the request telemetry and exact-revision source categories sent to that
provider. Request/response bodies, headers, credentials, raw SQL and bindings, cache keys, and
exception messages are excluded; credential-like literals in source excerpts are redacted. The
result shows the provider and model actually used, including when the existing gateway falls back
from remote to local. Findings cite available evidence and list missing coverage; they are AI
inferences, separate from the deterministic trace summary. After selecting a finding, developers can
request a revision-pinned unified diff preview. Preview generation revalidates the request evidence
and source mapping and never edits a checkout. Analysis and previews exist only in browser memory
and disappear on reload. Missing source allows telemetry-only analysis but disables code-diff
preview. Model output is advisory and should be reviewed before any manual change.

Definitions expanded rows also show observed method callers and callees plus the method's share of
retained Excimer profile samples. The PHP library can collect bounded exact call counts from an
optional generated hook manifest and 5 ms sampled profiles; profile capture is disabled when
Excimer is unavailable, and profile export requires the internal API endpoint and secret to be
configured in the instrumented PHP project. These values are runtime observations and statistical
hot-path estimates, not proof that an unobserved method is unused. See the
[Phase 4 Runtime guide](../api/docs/telemetry/runtime-phase4.md) for configuration, limits, and data
flow.

Runtime storage settings now check ClickHouse connectivity and show the active target's health and
last successful verification.

#### Installing PHP telemetry manually

Install the adapter in your PHP application directory. Both adapters automatically install
`aminmh/developer-assistant-otel-core`, so you do not need to require it separately.

**Laravel** (PHP 8.1+, Illuminate 10–12):

```sh
composer require aminmh/developer-assistant-otel-laravel
```

The Laravel package has been submitted to Packagist, but its first release is still awaiting indexing.
See the [Laravel installation and configuration guide](https://github.com/aminmh/developer-assistant-otel-laravel/blob/main/docs/installation.md) for setup.

**Symfony** (PHP 8.1+, Symfony 6.4 or 7):

```sh
composer require aminmh/developer-assistant-otel-symfony
```

See the [Symfony installation and configuration guide](https://github.com/aminmh/developer-assistant-otel-symfony/blob/main/docs/installation.md) for setup.

**Core** (PHP 8.1+):

For a PHP application without a Laravel or Symfony adapter, install and integrate the core directly:

```sh
composer require aminmh/developer-assistant-otel-core
```

See the [core installation and configuration guide](https://github.com/aminmh/developer-assistant-otel-core/blob/main/docs/installation.md) for setup.

#### Setting up a PHP project in one step

**Package source:** This version of the one-step script uses `indexr/indexer-otel-*` package names,
including with `--version`; it does not install the `aminmh/developer-assistant-otel-*` packages.
Use the manual package instructions above for those packages and inspect the script before running it.

For a PHP project that runs in Docker Compose, one command installs, indexes, and deploys telemetry:

```sh
just php-otel-setup ../my-php-app --yes
```

It starts the Developer Assistant API and Runtime services when they are down, allows the local host
agent to report the project, creates the project's source-upload token when missing, installs the
Indexr adapter (Laravel or Symfony is detected; core otherwise) from the neighbouring
`indexr-otel-library` checkout (or `--version` for a published package), commits only
`composer.json` and `composer.lock`, uploads the committed source, writes the telemetry settings into
the project's `.env`, labels the PHP Compose service, restarts the project at that commit, and waits
until the revision is indexed and ready. Without `--yes` it shows the plan and asks once; the same
action is in the `just` menu under **PHP telemetry → Set up a PHP project (one step)**.

- The project ID defaults to the service's existing telemetry label or the folder name
  (`--project-id`); the PHP service is detected (`--service`).
- Labels, the Runtime network, and a read-only mount of a local library checkout are added through a
  generated `compose.indexr.yaml`, loaded by `COMPOSE_FILE` in `.env` and listed in
  `.git/info/exclude`. The project's own Compose files are not edited.
- It refuses to run when the checkout has other uncommitted changes, because the indexed source must
  match what runs. The generated hook manifest (`indexer-otel-hooks.json`) and its refresh stamp do
  not count, even when the project tracks the manifest in Git. `--no-commit` stops instead of committing the Composer changes; `--no-restart`
  leaves the restart to you.
- After new commits, run it again: it indexes the new revision, updates `.env`, and restarts at that
  commit. Rerunning on an unchanged commit reuses the same revision.
- It writes the method hook manifest that **Method activity** reads. For Laravel and Symfony it sets
  `INDEXER_OTEL_HOOKS_FILE` in `.env` when it is not set yet (`storage/indexer-otel-hooks.json` or
  `var/indexer-otel-hooks.json`, relative to the project root), then runs
  `indexer:export --write-hooks` in the restarted container. The manifest lists up to 500 public
  methods of classes under `INDEXER_OTEL_HOOK_NAMESPACES` (default `App`): route controllers and
  registered services first, then every class Composer can autoload there (actions, resources,
  models...). Overrides of framework accessors such as `getIncrementing()` and methods from framework
  traits such as `SoftDeletes` are left out, because the framework calls them hundreds of times per
  request. After that the app keeps it current: at most every 30 seconds, after a response is sent,
  the library checks whether class files under those namespaces were added, removed or edited, and
  rewrites the manifest if so; the next request counts the new methods (`INDEXER_OTEL_HOOKS_REFRESH`
  sets the interval in seconds, `0` turns it off). A request that calls no listed method shows
  "No method-call data was captured for this request." A hooked controller appears in Method
  activity from the moment it is actually called, so route middleware shows as calling the
  controller, not the reverse.
- It needs the local host agent, which **Runtime → Set up Runtime** (or `just runtime-setup`) creates once. A project that does not run
  in Compose is installed and configured, but its revision can only become ready once a matching
  container deployment is reported. Excimer profile upload (`INDEXER_OTEL_PROFILE_*`) is not
  configured automatically.

The individual steps remain available with explicit options for scripts and agents:
`just php-otel-index --project-path PATH --project-id ID --yes`,
`just php-otel-install --project-path PATH --revision ID --endpoint docker-network --yes` (or
`--index` instead of `--revision`), and `just php-otel-prune --project-id ID [--keep REV] --dry-run`
to remove older revisions (then `--yes`). Each takes `--help`. See [installation guide](installation.md).

The PHP library (`indexr-otel-library`) supports PHP 8.1 through 8.4; CI runs its tests on 8.1, 8.2,
8.3, and 8.4.

#### Local ClickHouse storage and performance

Local mode stores only what the app reads: the Collector runs one ingest pipeline (memory limit,
privacy filter, transform) that forwards to the full-span pipeline writing `otel_spans_full`. The
tail-sampled `otel_traces` pipeline and span metrics run only in Remote mode (see the accepted ADR
`.archcore/runtime-telemetry/local-runtime-reads-full-spans-without-tail-sampling.adr.md`).

- The Python service owns the `otel_spans_full` schema (`telemetry/span_schema.py`; the Collector
  runs with `create_schema: false`). It adds materialized `ProjectId`, `RevisionId`, `Eligible`,
  `SymbolId`, and `HttpRoute` columns, sorts by `(ProjectId, RevisionId, SpanKind, Timestamp)`, and
  keeps spans for `TELEMETRY_AGGREGATION_RETENTION_HOURS` (72 by default; the exporter-created table
  had silently kept 7 days). An older exporter-created table is migrated in place on API startup,
  keeping the rows inside the retention window.
- Runtime summaries read hourly per-revision rollups (`runtime_span_rollups`) merged with the raw
  spans of hours not rolled up yet, so their cost no longer grows with the window. Aggregation skips a
  revision whose spans have not changed, and deleting requests re-rolls the affected hours.
- The Collector batches up to 4,096 spans or 5 seconds per insert, so new requests can take up to
  5 seconds longer to appear. Excimer profiles use ClickHouse asynchronous inserts. Request and
  profile deletion uses lightweight `DELETE`. ClickHouse's own metric logs are disabled.
- Measured on the Pizza Builder demo: span insert parts per window 24 → 2, profile parts about 125 → 2,
  ClickHouse CPU 7.5% → 4%, an aggregation cycle without new spans 1.7 s → 5 ms, and one summary
  9 queries / 33 MB read → 3 queries / 0.05 MB.
- The Collector keeps the library's span names (`http.server`, `db.query`, `messaging.process`,
  `cli.command`) and rewrites any other name to `runtime.span`; `error.type` passes only when it
  looks like a class name or status code. Spans carry the head-sampling ratio
  (`indexer.sampling.ratio`), which the Runtime summary uses to estimate real span volume.
- In Remote mode, slow traces are judged against per-route p95 thresholds (project p95 as fallback),
  and tail sampling waits 30 seconds and caches its decisions so late spans follow them.

The runtime Collector image is `devassistant/otelcol-runtime:0.136.0-phase4`; rebuild it with
`docker compose --profile runtime build telemetry-collector-runtime` from `api/` after pulling.

#### Span accuracy in the PHP library

- The server span starts at the request start (`REQUEST_TIME_FLOAT`) from an early hook, so bootstrap,
  middleware, routing, and the Symfony firewall are included. 404/405 requests produce a server span
  with route `(unmatched)`, any 5xx response marks it as an error, and the route ID uses the actual
  request method.
- Symfony requests report route, method, and status. An incoming W3C `traceparent` is honoured when
  `trust_traceparent` is enabled, and queued Laravel jobs and Symfony Messenger messages continue the
  dispatching request's trace. A Laravel job that throws and is retried closes its span, so the next
  job starts its own trace.
- Database spans are attributed to the application method that issued the query, never to a
  `vendor/` closure, including PHP 8.4 closure frames. Attribution runs once per query (backtrace
  depth 40, `INDEXER_OTEL_BACKTRACE_DEPTH` between 5 and 200), and without `ext-opentelemetry` a
  back-dated `db.query` span is created from Laravel's `QueryExecuted` event.
- OTLP export defaults to a 2-second timeout with one retry, so an unreachable Collector cannot hold
  PHP-FPM workers. The unused cache listeners were removed (`INDEXER_OTEL_CACHE` now has no effect).
