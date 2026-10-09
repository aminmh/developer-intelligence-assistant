# Runtime telemetry Phase 4: method calls and profiles

Phase 4 augments the Phase 3 request, database, and span-derived call data with bounded method
hooks and optional Excimer sampling. The Definitions expanded row displays observed callers,
callees, and hot-path share when these data are available.

## Data flow

1. Generate a method manifest from the statically exported Laravel or Symfony routes, controllers,
   and configured service classes. It contains at most 500 public instance methods.
2. With `ext-opentelemetry` available, the PHP library installs pre/post hooks for those methods.
   It records bounded caller-to-callee counts and attaches them to the request or job root span.
3. When `ext-excimer` is installed and profiling is enabled, the library samples at 5 ms, with a
   maximum stack depth of 128 and a 10,000-sample cap per request/job. It folds the stack after
   termination, retains only application frames as canonical method IDs, and retains a profile for
   5xx responses, requests slower than the SLA, or the configured baseline sampling rate.
4. Framework termination stops and folds the profile after the response is flushed (or after a CLI job); the bounded exporter sends profile envelopes
   to `POST /telemetry/runtime/profiles` using the API's internal secret. The API validates the
   project, indexed revision, clean-source/schema identity, canonical frames, sample period, and
   size before storing profiles in ClickHouse.
5. Runtime aggregation merges span-parent edges, exact hook counts, and sampled stack edges as
   distinct sources of `RUNTIME_CALLS` facts. Definitions shows the caller/callee edges and each
   method's share of retained in-app profile samples.

Static `CALLS` and observed `RUNTIME_CALLS` remain different graph relationships. Hook calls are
exact for selected methods. Excimer counts, edge weights, and hot-path percentages are statistical
estimates. An absent edge or profile does not prove that code is unused. Data is scoped by project
and immutable revision and expires with the configured aggregation retention.

## PHP project configuration

Enable method hooks in the Laravel package configuration, or Symfony bundle configuration, and
set the following environment values in the instrumented PHP service:

```dotenv
INDEXER_OTEL_HOOKS=true
INDEXER_OTEL_HOOK_NAMESPACES=App\Services,App\Domain,App\Repositories
INDEXER_OTEL_HOOKS_FILE=/app/storage/indexer-otel-hooks.json
INDEXER_OTEL_PROFILER=true
INDEXER_OTEL_EXCIMER_EVENT=EXCIMER_REAL
INDEXER_OTEL_PROFILE_SLA_MS=200
INDEXER_OTEL_PROFILE_SAMPLE_RATE=0.01
INDEXER_OTEL_PROFILE_ENDPOINT=http://python-api:9002/telemetry/runtime/profiles
INDEXER_OTEL_PROFILE_TOKEN=<same value as the API RAG_CALLBACK_SECRET>
```

The API endpoint shown is the internal Docker service address for the Developer Assistant Compose
network. Use the address reachable from the instrumented app when it runs elsewhere, and provide
the same internal secret to the PHP service without putting it in source control. The profile queue
is best effort and bounded; a missing endpoint, invalid token, unavailable extension, or failed
export does not fail the application request.

Generate the manifest from the PHP package's static export command with `--hooks-output=PATH` after
the app classes are autoloadable. The exporter includes selected routes/controllers and classes
under `INDEXER_OTEL_HOOK_NAMESPACES`. Keep this namespace list focused; generated manifests are
capped at 500 methods. The profiler requires `ext-excimer`; use `INDEXER_OTEL_PROFILER=false` to
disable it, or select `EXCIMER_CPU` for CPU-time profiling instead of the default wall-clock event.

## Runtime data and limitations

- `RUNTIME_CALLS` merges evidence by edge without adding independent sources together, which avoids
  double-counting the same call when hooks and spans both observe it.
- “Hot-path share” is the percentage of retained application-frame samples attributed to a method.
  It is not a percentage of all request time or CPU unless CPU sampling was selected.
- Aggregation reads at most the latest 1,000 profiles in the selected window.
- The API retains at most 64 profiles and 2,000 folded stacks per profile, 256 KiB per folded
  profile, and 2 MiB per HTTP upload; the PHP exporter splits queued profiles into safe-sized
  batches.
- Long-running PHP workers reset hook state at request/job boundaries. Framework lifecycle hooks
  cover Laravel and Symfony requests, commands, and supported job middleware.
- The roadmap's overhead gates (<5% p95 for hooks and <1% for Excimer), correct trace/span
  correlation in demo apps, and 80% exercised-static-edge coverage require measurements in the
  target PHP runtime and demo test suites. A local syntax/build pass alone does not establish those
  performance or coverage gates.
