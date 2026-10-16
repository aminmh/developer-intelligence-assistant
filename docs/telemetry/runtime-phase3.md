# Runtime telemetry pipeline and aggregation

Phase 3 joins Collector processing, project and immutable-revision scoped rollups, the
`PhpSymbol` graph, exception search, and live UI invalidation.

## Data path

1. The PHP SDK sends OTLP spans to the runtime Collector. Its resource identity
   must include `indexer.project_id`, the app-generated `indexer.revision_id`,
   `indexer.code_dirty=false`, and
   `indexer.schema.version=indexer-telemetry-v2`.
2. The Collector removes unapproved resource and span attributes. It writes the
   spans it received before tail sampling to `otel_spans_full`; `spanmetrics`
   derives count and duration metrics from that same input.
3. A second trace pipeline applies the project's rolling p95 threshold and
   tail-sampling policy, then writes retained traces to `otel_traces`.
4. The API periodically reads the pre-tail table over the configured rolling
   window. It updates runtime properties and `RUNTIME_CALLS`, `HANDLED_BY`, and
   `QUERIES` facts on the indexed `PhpSymbol` snapshot identified by its
   revision, and stores normalized exception signatures in the
   `runtime_exceptions` Qdrant collection.
5. The API publishes a project-free `runtime.updated` SSE invalidation after
   deployment reports and aggregation. Runtime, Definitions, and Schema views
   reload their data when they receive this event; they do not use a UI polling
   timer.

The Collector table is called `otel_spans_full` for compatibility. “Full” means
before Collector tail sampling. PHP SDK head sampling happens before the
Collector and may already have reduced the input. Therefore the pre-tail
aggregates are counts of spans received, not guaranteed counts of every
request. Error and slow-trace retention only applies to traces that reach the
Collector.

Runtime also lists up to 50 sampled server requests from the selected project's
immutable revision and aggregation window. Expanding a request loads its trace
through a revision-scoped query and returns only the request span and database
spans with safe operation, system, namespace, table, and symbol attributes.
SQL text and bindings are not returned. Requests without matching DB spans
show an empty database-call list; unavailable fields remain unavailable for
older spans or unsupported PHP runtimes.

Request spans may include the PHP worker's user-plus-system CPU time delta
(microseconds) and PHP allocator peak above request-start memory (bytes). These
are worker-level estimates, not container CPU or resident memory. Request
sampling may omit traffic, and callers need project access to read the details.
The Runtime view can hide the currently loaded request rows without changing
storage. Project administrators can permanently delete the stored spans for up
to 50 currently listed request traces; this is revision-scoped, includes child
database spans, and reduces the aggregates computed from those spans. The UI
requires confirmation before deletion.

## Sampling controls and retention

- The rolling p95 window defaults to 24 hours, bounded by
  `TELEMETRY_AGGREGATION_RETENTION_HOURS` (default 168 hours).
- The Collector policy refresh defaults to 5 minutes. The Runtime telemetry
  settings card can change the p95 window and refresh interval.
- `TELEMETRY_RECORDING_PERCENT` controls the Collector's baseline tail sample
  after upstream SDK sampling. It defaults to 10%.
- `TELEMETRY_RETENTION_HOURS` controls retained tail-sampled traces in
  `otel_traces`. `TELEMETRY_AGGREGATION_RETENTION_HOURS` controls the pre-tail
  table used for rollups and p95 policy generation.

## ClickHouse storage target

Runtime settings offer three storage modes: Disabled, Local, and Remote.
Local remains the default and uses the existing ClickHouse environment values.
Disabled stops Collector pipelines and makes Runtime reads unavailable; it
does not remove stored data. Switching targets never copies or deletes data,
and does not change PHP capture or sampling settings.

Remote setup is available to administrators in Settings & Connection. It
supports Ubuntu and Debian hosts with x86_64 or ARM64, at least 2 GiB free disk
space, outbound package access, and an SSH account with sudo. First retrieve the
SSH host fingerprint and compare it with the remote administrator's value.
After confirming it, save the SSH private key through the desktop helper and
then test SSH and sudo. Direct hosts and aliases with a HostName, User, and Port
are resolved from `~/.ssh/config`; ProxyJump and ProxyCommand aliases are not
supported. On macOS, the helper uses Keychain; on Linux desktop
sessions, it uses Secret Service. If the keyring is locked, unlock it and use
Refresh credential status to retry. Sudo passwords are requested only for the
check or installation and are not stored. A failed SSH or sudo check stops
before package or server configuration changes and displays host-side repair
guidance.

The installer configures ClickHouse to listen on remote loopback only. The
remote HTTP and native ports are 8123 and 9000; do not open them in host or
cloud firewalls. Only the configured SSH port needs inbound network access.
ClickHouse uses persistent `/var/lib/clickhouse` storage and an account based
on `TELEMETRY_CLICKHOUSE_USER` and `TELEMETRY_CLICKHOUSE_PASSWORD`. Remote setup
requires a password of at least 32 characters (the local development default
is rejected). Set it in `api/.env` before setup, then recreate the API,
Runtime Collector, and local ClickHouse containers so they receive it. ClickHouse
credentials are not stored in Runtime settings or returned to the browser.
SSH private keys and optional key passphrases are held in the OS credential
store and loaded into an SSH agent for forwarding; they are not sent to the
Python API or containers. On macOS, Docker Desktop forwards the desktop
session's SSH agent. On Linux, the helper uses its dedicated Runtime agent.
The Runtime credential controls add or remove only the matching SSH identity;
other identities in the macOS agent are preserved. The API and tunnel
containers receive agent signing access only. On macOS this Docker Desktop
forwarded agent contains all identities already loaded in the desktop agent,
so those identities are also usable by the API and tunnel containers while
Runtime services run. Linux uses a dedicated Runtime agent. Sudo passwords are
request-scoped and are not persisted.

The Runtime settings page checks ClickHouse health through the SSH tunnel
before showing the target as verified. It shows the last successful check and
reports a remote target as unavailable when SSH or ClickHouse cannot be
reached. Remote provisioning does not move local telemetry; switching back to
Local makes the existing local data available again. Full-stack startup keeps
local ClickHouse available so the settings UI can switch between Local and
Remote without Docker control from the API. The Collector stays on the
Developer Assistant host and remains running when the target changes; the
router closes existing exporter connections so the Collector reconnects to
the selected endpoint. A brief write retry may occur, and the bounded
in-memory exporter queue is not durable across process failure.

If the keyring is locked, unlock it in the desktop session and refresh
credential status. If SSH authentication fails, confirm the authorized public
key, SSH account, port, and verified host fingerprint. The guided installer
provisions supported Ubuntu/Debian hosts. For a preinstalled ClickHouse, bind
its HTTP and native listeners to loopback, configure the Runtime account, and
use the same SSH host settings; direct ClickHouse network connections are not
supported. Confirm that the SSH account can reach ClickHouse at
`127.0.0.1:8123` and `127.0.0.1:9000` on the remote machine.

## Remote host monitor

When Remote storage is selected, the Runtime view calls the authenticated web
route `/api/runtime/host-metrics`, which proxies the internally authenticated
`GET /telemetry/runtime/host-metrics` endpoint. The endpoint uses the saved
SSH target and its confirmed host fingerprint, then runs a fixed read-only
Linux query without sudo. It reports CPU utilization from consecutive `/proc`
counter samples, RAM, load averages, and free/total disk for
`/var/lib/clickhouse`. ClickHouse health is checked separately through the
selected Runtime connection, so SSH can remain healthy while ClickHouse is
reported unavailable.

The monitor polls every five seconds while the Runtime view is open and the
browser tab is visible. It prevents overlapping requests, aborts polling when
the tab is hidden, and stops when the view unmounts. CPU displays a warming
state until the first counter delta. SSH or keyring failure reports an
actionable unavailable state; if a prior sample exists, the UI marks it stale
and shows its original sample time. Local and Disabled storage modes omit the
monitor. The API keeps only its latest sample and CPU counters in process
memory and returns neither credentials nor raw command output. No metric
history is stored.

The application uses brief CSS and Vue transitions for page changes, panels,
dialogs, toasts, and controls. Reduced-motion preferences suppress those
transitions; live metric values change without animation.

## Implementation status

The API, web, PHP library, and custom Runtime Collector expose bounded,
revision-scoped request details with database calls and PHP worker estimates.
The production PHP SDK sampler remains at its existing 10% default; changing
it to 100% was not included.
