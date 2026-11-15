# Developer Intelligence Assistant

A developer assistant for exploring source code, assessing code quality, and investigating runtime
behavior. Upload a project, index its code, and ask questions in natural language. The application
retrieves relevant context and uses a locally configured language model to help answer.

## Installation and first use

The local installer supports macOS and Ubuntu/Debian. It requires Docker, Ollama, and other host
tools; allow at least 40 GiB of free disk space, four Docker CPUs, and 8 GiB of Docker memory
(16 GiB for analyzers). See the [installation guide](docs/installation.md) for the complete
requirements, supported versions, configuration, credentials, and recovery instructions.

```bash
git clone https://github.com/aminmh/developer-intelligence-assistant.git
cd developer-intelligence-assistant
bash install.sh
```

The installer checks dependencies and asks you to review its proposed setup. When installation
finishes, run `bash install.sh info` to view the local application address and credentials. Sign
in, upload a source project through the app, index a revision, and use chat to ask about its code.
Explore **Code Quality** for source assessments and **Runtime** for configured telemetry.
Keep the generated credentials private.


## What this project does

It's a developer-assistant chat over uploaded source projects. ZIP archives can be parsed on demand
into PHP AST chunks, embedded, and indexed for code questions. Retrieval and generation run in a
Python service; a Node web app provides the chat UI and public API; the Python image also ships a
pure-PHP CLI that performs the AST chunking and project-structure extraction.

## Architecture

- **web** (Node/Express) — public entry point (`/api/query`), chat UI, config UI, file management.
- **python-api** (FastAPI + in-image PHP CLI, internal to the Compose network) — routing, retrieval, and answer generation (LlamaIndex,
  Qdrant, Neo4j, Ollama). Also owns MinIO storage, ZIP uploads, workspace extraction, and the
  pure-PHP CLI tools (`code-parse`, `project-export-structure`).
- **Qdrant** — vector store for code chunks.
- **Neo4j** — property graph of code symbols and `CALLS` edges (used for hybrid retrieval).
- **Ollama** — local models: `gemma3-4b` (LLM) and `BGE-M3` (embeddings).
- **MinIO + tusd** — object storage and resumable uploads.

## Features implemented

### Semantic Router
Routes each query by meaning (not keywords) into one of six intents (`code_explain`, `code_refactor`,
`debug`, `search`, `knowledge`, `chat`). A feedback loop stores corrections in Qdrant so the same or
similar queries route correctly later; uncertain cases fall back to an LLM-based selector.

### Query normalization
A shared normalization step runs on both the route examples and the incoming query: NFKC unicode
normalization, case folding, whitespace/punctuation cleanup, and domain-jargon expansion
(`api/Python-Service/app/config/domain_terms.json`). A typo-correction pass (SymSpell over a
domain-only dictionary) is **disabled by default** behind `ENABLE_TYPO_CORRECTION`.

### Hybrid retrieval (vector + graph)
Code is indexed into Qdrant *and* a Neo4j property graph. Retrieval does a vector search, then
walks the graph to pull callee/caller neighbours of the top hits, merges and reranks. If the graph
is unavailable it degrades to vector-only search.

### Intent & entity extraction
Before answering, an LLM extracts structured intent and entities (file, class, method, error code,
framework) via Instructor + Pydantic (`app/intent.py`), used to filter retrieval and enrich the prompt.

### Answer generation
The final answer is generated in Python (`app/answer.py`) from retrieved code context using the
local LLM. Prompts are logged to `logs/llm-prompts.log`.

### Code validation with language servers (LSP framework)
When the LLM emits code blocks in an answer, the Python service validates them with in-image
language servers over the Language Server Protocol (stdio). The framework is modular and
config-driven:

- `app/lsp/client.py` — generic stdio LSP client (framing, diagnostics settle-wait, auto-restart).
- `app/lsp/base.py` — the `LanguageAdapter` contract + shared helpers (block extraction, repair
  prompt, multi-language validation note).
- `app/lsp/adapters/<lang>.py` — one custom adapter per language (see `php.py` for the reference).
- `app/config/lsps.yml` — declarative per-language server config (`enabled`, `adapter`,
  `fence_tags`, `lsp:{binary, args, language_id, validate_dir, timeout}`); `${VAR:-default}` is
  resolved from the environment.

**PHP** uses **Phpactor** (syntax + worse-reflection diagnostics) with **PHPStan** (static
analysis) integrated. Unresolvable external references (classes/functions from the surrounding
project, which a snippet doesn't contain) are demoted to warnings.

Invalid blocks are fed back to the LLM with the diagnostics to auto-repair (up to
`LSP_MAX_RETRIES`, default 2); truncated/illustrative blocks (`...`) are annotated but not
repaired. The answer gets a compact per-language note, e.g. `> ✅ PHP validation passed — 1
block(s), 1 auto-repaired.` The `/answer` response also carries a structured `code_validation`
field (list of `{language, index, status, errors, warnings}`).

If a language server is unavailable, validation falls back to `bin/lint-php` (php-parser +
`php -l`) for PHP. Toggle the whole feature with `LSP_VALIDATION_ENABLED` (default `true`).

#### Adding a new language LSP
1. Install the language server binary in the image (Dockerfile).
2. Add an entry to `app/config/lsps.yml` (`enabled: true`, `adapter`, `fence_tags`, `lsp{...}`).
3. Create `app/lsp/adapters/<adapter>.py` implementing `LanguageAdapter` — copy `php.py` /
   `app/lsp/base.py` as the template (wrap fragments, map diagnostics + downgrade heuristics,
   optional non-LSP fallback, block extraction).
4. Add a test (see `tests/test_php_adapter.py`, `tests/test_lsp_registry.py`) and rebuild the
   image. Adapter + yml changes are live via the `/app` bind-mount; a **new server binary**
   requires `docker compose build python-api`.

For a guided walkthrough, use the `add-language-lsp` skill.

### Code Quality assessment
Code Quality offers static source assessment and a separate **Setup Performance** tab. The static
Performance category remains evidence-based source analysis. Live HTTP runs use the pinned k6 runner
bundled in the Python API image and never change static scores. Rebuild `python-api` after changing the
pinned runner version.

- **Targets** belong to a project and have a name, so one origin can have several profiles (for example
  public and logged in). Only project operators and admins can change targets or start runs; viewers read
  and compare results. Local Docker targets use `host.docker.internal` with the application's published
  port and are available only in Developer Mode; ports of Developer Assistant's own services are refused
  (extend the list with `PERFORMANCE_BLOCKED_HOST_PORTS`). A target that uses a token or login must use
  HTTPS unless it is local.
- **Routes** come from the revision's Runtime traffic (busiest first, with production p95), or can be added
  as GET paths. Templated routes such as `/pizzas/{pizza}` ask for parameter values, remembered per target.
- **Workload:** each route runs as its own k6 scenario with constant users, ramping users (stages) or a
  constant request rate; optional think time, an unmeasured warm-up, and p95 / error-rate budgets.
  At most 100 virtual users across all routes.
- **Authentication:** public, a Bearer token saved per target in macOS Keychain or Linux Secret Service, or
  a login form with presets for Laravel (XSRF cookie sent as the `X-XSRF-TOKEN` header), Sanctum SPA and
  Symfony. A login counts only if it does not redirect back to the login page and the optional logged-in
  check route returns 2xx; login time is excluded. Credentials are used for one run only and never saved.
- **Runs** start in the background (one per user, two in total), show live progress, survive leaving the
  page, and can be stopped with their partial result kept. Redirects, 4xx and 5xx responses make a run
  partial with a stated reason.
- **Results** show per-route requests/s, 2xx/3xx/4xx/5xx counts, p50–p99, production p95, budget status, and
  the slowest Runtime-traced requests the run produced (the PHP library tags them from the
  `X-DevAssistant-Benchmark` header). Export as CSV or JSON, or download the k6 script with secrets
  replaced by `BENCHMARK_TOKEN` / `BENCHMARK_USERNAME` / `BENCHMARK_PASSWORD` environment variables.
- **Compare** two runs, typically a baseline on the previous revision against the latest one: per-route
  p50/p95/p99, throughput and error deltas, regressions (p95 up more than 10% or more errors) when the
  inputs are equivalent, and whether the code revision changed.

The **Custom rules** tab lets each signed-in user create, edit, enable, disable, preview, and delete
their own bounded Semgrep YAML rule sets. Built-in rules are read-only. Rules are schema-checked,
limited to supported languages and Code Quality categories, and run offline against inventoried
business source files only. A preview scans the selected immutable revision without saving an
assessment report. Completed reports retain the exact custom rule text and content hash used, so
later edits do not change prior results. Custom rules cannot download rule packs or run commands.
Rebuild the `python-api` image to install the pinned Semgrep runner after updating the analyzer
dependency.

The **Business rules** tab connects each project to one dedicated Outline collection. Code Quality reads
documents from Outline without modifying them. Save the read-only Outline API token in the host OS credential
store; the API reads at most 50 documents in stable ID order and reviews at most 20 per assessment. Reports
snapshot each document ID, title, body, update time, and content hash so later Outline edits do not change
saved reviews. Assessments compare requirements with bounded findings and code excerpts from the selected
revision, then report supported, concern, or unknown with source evidence citations. Unknown means the indexed
evidence could not verify the behavior; it does not mean the requirement is missing or satisfied. Existing
locally stored rules remain exportable as Markdown for migration, and saved reports remain readable. The
evaluator does not execute project commands or infer compliance from missing matches.

Below the connection, **Rules in the code** keeps a read-only synced copy of the collection, refreshed by
**Sync now**, every 10 minutes while the desktop credential helper runs, and by every assessment; every view
shows when it was last synced. Assessments still read Outline live. Developers use the copy to:

- **Link rules to code.** **Suggest code links** searches the selected revision's code index with each rule
  and proposes methods (test files and weak matches are skipped). Confirm, reject, or link a method,
  `route:GET /path` or `table:connection.name` by hand. Linked rules appear next to the code in the source
  preview, a Runtime request's Method activity tab, and the Database Schema table panel.
- **Ask chat.** Chat answers for a project the user can access include up to three matching rules, cited as
  `[R1]` with a link to Outline and the code each rule is linked to.
- **Check a change.** Paste a `git diff` (or use **Check against business rules** under a Runtime diff
  preview). Changed lines are mapped to methods of the selected revision, and only rules linked to those
  methods are reviewed: may break, changes behaviour, unclear, or unaffected.
- **Spot drift.** A confirmed link is flagged when its rule changes in Outline or its method's code changes in
  a later indexed revision; **Still holds** clears the flag.
- **See production use.** Each rule shows calls, error rate and p95 of its linked code from Runtime telemetry,
  or "never executed".
- **Write tests.** **Generate test skeleton** writes Given/When/Then scenarios and a Pest or PHPUnit file
  tagged with the group `rule-<document id>`; nothing is written to the repository. A rule shows as tested
  once the indexed source contains that group.

Rule text goes only to the local model and local embeddings unless the project enables **Allow rule text
to go to the remote model**. With a remote embedding provider and the setting off, chat matches rules by
keyword instead.

The **External analyzers** tab optionally enables SonarQube and Joern per project. They execute in an
isolated analyzer worker, never in the API process. Developer Mode can run that worker locally or on an
approved remote host. The remote setup described below runs SonarQube, PostgreSQL, and the isolated worker
container; it does not run the Developer Assistant application or require a repository checkout. The API
requires the worker's mutual-TLS identity and explicit project consent before sending selected source to
the worker. SonarQube additionally requires a project key, separate source-upload consent, and a token
saved in the application host OS credential store. Reports record worker identity, tool and server
versions, supported and skipped language coverage, results, and failure reasons. Unavailable, partial,
and zero-finding runs do not prove that the source is defect-free. These integrations add evidence to the
existing rubric and do not replace Semgrep or assign scores themselves.

The global administrator can disable Developer Mode remote analyzers from this tab for every project.
Project configuration is retained, but worker checks and remote analysis stop until the feature is enabled
again. Production Mode locks Developer Mode off and continues to use its deployment-installed local worker.

In the Docker-based development stack, the Web container reaches the host credential helper through
`host.docker.internal`; the helper accepts only short-lived, signed, single-use operations scoped to a
project and credential. The helper port is reachable on host interfaces while the stack runs, so keep the
host firewall enabled and do not forward port 47831. Run `just up-dev` after updating the stack.

### Analyzer worker and remote SonarQube

The analyzer worker uses the opt-in `analyzer` Compose profile, so ordinary development startup does not
build or start the large analyzer image. Build and run the worker locally from this repository with
`docker compose --profile analyzer up -d analyzer-worker`. The worker image pins SonarScanner and Joern,
runs non-root without Docker socket access, and applies memory, CPU, process, and temporary-storage limits.
The first image build needs several gigabytes of free disk and may take a while.

For a remote worker, generate mutual-TLS certificates on the Developer Assistant host with the worker's
DNS name and IP address:

```sh
api/analyzer-worker/provision-certs.sh /secure/path/analyzer-certs worker.example.local 192.0.2.20
```

Keep `offline-ca/` private keys in secure storage outside both runtime hosts. Keep the `api/` bundle on
the Developer Assistant host. Copy only the `worker/` bundle to the remote host. In External analyzers,
set the worker HTTPS address, fetch its fingerprint from the CA-verified connection, test the connection, and enable
the worker only after consenting to selected-source transfer for that project.

The remote host needs only the pinned worker image, `sonarqube-only.compose.yaml`,
`remote-worker.compose.yaml`, the worker TLS bundle, and a private `.env`; do not copy or check out the
Developer Assistant repository there. Set `SONAR_DB_PASSWORD`, `SONARQUBE_HOST_PORT`, and
`ANALYZER_WORKER_BIND` in `.env`. Run both Compose files as one project:

```sh
docker compose -f sonarqube-only.compose.yaml -f remote-worker.compose.yaml up -d
```

SonarQube binds its management port to loopback (default host port 9000); the worker reaches it over the
private Compose network at `http://sonarqube:9000`. The worker listens only on the configured host
interface and requires mutual TLS. Restrict the worker port to the Developer Assistant host at the host
firewall or private network boundary. Keep SonarQube volumes persistent and backed up. Configure the
project's SonarQube URL as `http://sonarqube:9000`, set its project key, separately consent to SonarQube
source upload, and save the token in the application host OS credential store. The worker sends only the
selected revision to SonarQube during that opted-in scan.

Production Mode keeps the worker and SonarQube on the production application host. Project settings cannot
enable a remote worker or off-host source transfer in Production Mode. Keep the analyzer database and
SonarQube volumes in the environment's backup, retention, upgrade, and rollback procedures; worker job
files remain ephemeral.

The **Git source** tab can connect a project to a GitHub `owner/repository` and branch. Initial
support is HTTPS against GitHub only; private repositories need a fine-grained personal access token
with read-only Contents permission. The desktop helper stores that token in macOS Keychain or Linux
Secret Service, scoped to the project credential ID. It is released for one synchronization request,
used only for `git fetch`, and never persisted by the API. A fetch resolves the branch head to a full
commit SHA and deterministic source archive checksum. Existing matching ready revisions are reused;
new snapshots enter the source-only indexing queue and do not assert that the project is deployed.
Assessments can use the revision after indexing reaches ready. Static assessment supports configured
languages whether or not PHP is present. Semantic search and call tracing remain limited by the PHP
graph exporter; non-PHP revisions show semantic evidence as unavailable. Git submodules are not
supported yet.

Semantic flow tracing currently covers bounded PHP request-input-to-sensitive-operation call paths.
It follows indexed static call edges, cites each symbol, and marks unresolved edges or limits as
partial. A complete trace means complete within those patterns and the selected revision's indexed
PHP business files; it does not prove variable-level data flow or whole-program behavior. Retrieved
semantic excerpts are filtered against the assessment's business-source inventory, excluding
dependency, build, cache, generated, and temporary paths from evaluator context.

To enable optional GitHub push notifications, set `CODE_QUALITY_GITHUB_WEBHOOK_SECRET` in the Web
server environment to the same random secret configured in the GitHub webhook. Use the Webhook URL
`/api/code-quality/github/webhook`, content type `application/json`, and the `push` event. The Web
server verifies GitHub's SHA-256 HMAC signature before resolving the configured repository and
triggering a fresh authenticated fetch. Webhook commits are never accepted directly; manual
**Fetch branch head and reconcile** remains available when notifications are missed or rejected.

### Configurable settings
Settings live in a SQLite store and are editable from the UI without restarting: Ollama base URL,
LLM, embedding, and evaluation judge model dropdowns (populated from pulled Ollama models), Qdrant
connection, temperature, and top-P. The evaluation judge model is configurable independently and is
used only by RAGAS scoring.

### Upload and indexing
ZIP projects are stored in MinIO (via the internal Python service) and indexed on demand through the
chat orchestration flow or the file indexing action. New uploads require a project slug and full
lowercase Git commit SHA-1 or SHA-256. Exact-byte retries are accepted; different bytes for the same
project and commit are rejected. The Knowledge Base shows the stored identity. Older uploads stay
unattributed. Document uploads (PDF/Markdown/text) are not supported — the pipeline is code-archive
only.

The tab shell stores the selected tab in the `tab` query parameter, so a tab URL opens consistently
after refresh and participates in browser back/forward navigation.

Browser API access requires an OIDC sign-in. Configure the issuer, client, callback URI, session
secret and bootstrap administrator subject before starting the web service. The first administrator
can grant project roles (`viewer`, `operator`, `admin`) from the Project access control in the UI.
The Python service has no host-published port; browser calls go through the authenticated web API.

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
Settings & Connection also lets administrators select Disabled, Local, or Remote ClickHouse storage. Local remains the default. Remote setup connects over SSH, verifies the host fingerprint and sudo access, then installs ClickHouse on supported Ubuntu or Debian hosts. When enabling a saved Remote target while its bridge is running, the app updates the router and SSH tunnel target, then verifies ClickHouse through that tunnel; a failed verification restores the previous target. If a selected backend is stopped, run the command shown in Settings (for example, `just runtime-storage local`) to start and verify it. The desktop SSH helper must be running for key status and SSH operations; if it is unavailable, restart Developer Assistant with `just up-dev` and refresh credential status. The remote ClickHouse HTTP (8123) and native (9000) listeners stay bound to loopback; only the configured SSH port needs network access. The desktop helper resolves direct host entries and aliases from `~/.ssh/config` and stores the SSH key and optional passphrase in macOS Keychain or Linux Secret Service. On macOS, the helper uses Security.framework directly and sends credential data over stdin, never in process arguments. A restricted Runtime tunnel sidecar maintains the SSH tunnel. macOS uses Docker Desktop's forwarded desktop SSH agent; Linux uses a dedicated Runtime agent. On macOS, the API and tunnel containers can request signatures from every identity currently loaded in the forwarded desktop agent, not just the Runtime key. The helper only adds or removes the Runtime identity and preserves other macOS identities. Private key bytes remain in the OS credential store and are not sent to containers. SSH `ProxyJump` and `ProxyCommand` aliases are not supported. Unlock the OS keyring and retry when it is locked. The remote database uses persistent storage and the configured Runtime credentials; set `TELEMETRY_CLICKHOUSE_PASSWORD` in `api/.env` to a strong password of at least 32 characters before setup. Switching targets never copies or deletes telemetry and does not change PHP capture or sampling. The Collector's bounded in-memory queue is not durable across process failure. See the [Runtime telemetry guide](api/docs/telemetry/runtime-phase3.md#clickhouse-storage-target) for setup and recovery steps.
For SSH setup, paste the full private key including its `BEGIN` and `END` lines. The SSH port
defaults to 22; change it only when the server uses a custom port. Fingerprint retrieval does not
use the private key. Save the key to the desktop credential store before testing SSH and sudo; the
test button remains disabled until the credential status is Ready. Pasted CRLF line endings and
outer whitespace are normalized.

Instrumented PHP projects must send the Developer Assistant project ID and immutable indexed
revision ID. See [Get started](get_started.MD) and the
[Runtime telemetry guide](api/docs/telemetry/runtime-phase3.md) for setup details.
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
[Phase 4 Runtime guide](api/docs/telemetry/runtime-phase4.md) for configuration, limits, and data
flow.

Runtime storage settings now check ClickHouse connectivity and show the active target's health and
last successful verification.

#### Setting up a PHP project in one step

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
to remove older revisions (then `--yes`). Each takes `--help`. See [Get started](get_started.MD).

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

### Misc
Feedback button in chat, step-by-step progress while a query runs, session-scoped skip of
re-indexing, exception logging to `logs/exceptions.log`, and a shared inset dropdown chevron on
single-select controls.

## Documentation rule

Every new feature and every bug fix must update this README in the same change. Describe the
user-visible behavior and, when relevant, its setup, configuration, and limitations. Treat the
README update as part of completing the feature or fix.

## Prompt lifecycle

What happens to a prompt from the moment you hit send, using
*"Explain the `planWeekly` method in `BookingSchedulePlanningService`"* (with `Archive.zip` attached):

1. **Normalize** — the raw text is normalized for routing (NFKC → lowercase → cleanup → jargon expansion):
   `explain the planweekly method in bookingscheduleplanningservice`
2. **Classify** — `/classify` (Semantic Router) returns a route, e.g. `code_explain`.
3. **Extract** — `/extract` (Instructor) returns structured entities:
   `{intent: "explain", class_name: "BookingSchedulePlanningService", method_name: "planWeekly"}`.
4. **Orchestrate** — `/orchestrate` runs the route's tool (`chunk_code`) via the in-image PHP CLI,
   parses the zip, and indexes chunks into Qdrant + Neo4j (`llamaindex_ingest`). The frontend polls
   for progress.
5. **Retrieve** — `/answer` runs hybrid retrieval: vector search in Qdrant, then graph traversal
   in Neo4j for callee/caller neighbours.
6. **Generate** — the answer is built from a final prompt and sent to the local LLM:

```text
[system] You are an expert developer assistant RAG system…

Retrieved code context:
[1] …/BookingSchedulePlanningService.php | planWeekly
```php
public function planWeekly(Offer $offer, …): Schedule { … }
```

Detected context: Intent: explain, Class: BookingSchedulePlanningService, Method: planWeekly

User question: Explain the planWeekly method in BookingSchedulePlanningService
```

7. **Answer** — the LLM response is returned to the UI; the final prompt is logged to
   `logs/llm-prompts.log`.

Without an attached file, steps 2–4 are skipped and the query goes straight to retrieve + generate.
This direct path searches existing Qdrant project collections; it does not make an unindexed upload
retrievable automatically.

## Evaluation

Per-response, reference-free Ragas evaluation is implemented. The UI can evaluate a completed
response through `/api/query/:queryId/evaluate`; it reports `faithfulness`, `answer_relevancy`,
and `context_precision` with human-readable score tiers. Evaluation uses the configured Ollama
LLM and embedding model and is cached in memory.

An offline regression suite with ground-truth fixtures and `context_recall` is **not implemented**.

## How to run locally

Prerequisites: Docker + Docker Compose, and Ollama with the models pulled:

```bash
ollama pull gemma3-4b
ollama pull BGE-M3
```

Install the local task runner and TUI menu. On macOS or Linux with Homebrew:

```sh
brew install just gum
```

From the repository root, run `just` to open the service/action menu. To start
the hot-reload development stack directly, run `just up-dev`; use `just up` for
the production Web service with local Keycloak. For lower idle memory, configure
an external OIDC provider in `web/.env` and run `just up-lite`. Set
`OIDC_LITE_INTERNAL_ISSUER` only when the Web container needs a provider URL
different from `OIDC_ISSUER`. Lite mode uses a 30-second Ollama model idle
timeout; override it for one run with `OLLAMA_KEEP_ALIVE=1m just up-lite`.
Run `just down` before switching between full, development, and lite modes.
`just --list` shows all direct actions.
Full-stack startup also checks the optional local Runtime host agent; if it
cannot start, Just prints a warning and continues with the remaining services.
It starts the Runtime Collector and ClickHouse on the API's private telemetry network so sampled
request details can be queried. This is separate from the synthetic-only Phase 0 telemetry sandbox.


Stop and remove the stack containers with:

```bash
just down
```

The corresponding direct Runtime actions are `just runtime-setup`,
`just runtime-status`, and `just runtime-cleanup`. Setup and cleanup are also
available in the browser under **Runtime**, with approval in a dialog on this computer.

You can also start API and Web separately:

```bash
make -C api up
make -C web dev
```

Services: web `:3000`; Python API is internal on `:9002`; Qdrant `:6333`; Neo4j `:7474`/`:7687`.

## Project structure

```
api/                # Python service + in-image PHP CLI, shared docker-compose, feature specs (*.MD)
  Python-Service/app/   # FastAPI app (router, orchestrator, retrieval, generation, settings, storage)
  Python-Service/app/lsp/   # Modular LSP framework (client, base contract, registry, adapters/)
  Python-Service/app/config/lsps.yml   # Declarative per-language LSP config
  Python-Service/php/   # Pure-PHP CLI composer project (code-parse, project-export-structure, lint-php)
  Python-Service/php/lsp/   # Phpactor + PHPStan toolchain (LSP validation of LLM-generated PHP)
  Python-Service/scripts/  # generate_domain_dictionary.py (rebuilds the typo-correction dictionary; runs automatically after code indexing)
web/                # Node/Express server + Vue frontend
```

## Configuration

Key environment variables / flags (with defaults):

| Var | Default | Purpose |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama server |
| `OLLAMA_EMBED_MODEL` | `BGE-M3` | Embedding model |
| `OLLAMA_LLM_MODEL` | `gemma3-4b` | Chat/LLM model |
| `OLLAMA_KEEP_ALIVE` | `5m` | Idle time before local Ollama chat and embedding models are unloaded from RAM (`0` unloads immediately) |
| `OLLAMA_EVAL_MODEL` | Falls back to `OLLAMA_LLM_MODEL` | RAGAS evaluation judge model |
| `QDRANT_URL` | `http://qdrant:6333` | Vector database URL |
| `ENABLE_TYPO_CORRECTION` | `false` | SymSpell typo correction (ship off) |
| `LLM_TEMPERATURE` / `LLM_TOP_P` | `0.7` / `0.9` | Sampling |
| `AWS_ENDPOINT` | `http://minio:9000` | MinIO/S3 endpoint for RAG uploads |
| `AWS_TUS_BUCKET` | `tus-uploads` | MinIO bucket holding uploads |
| `UPLOAD_TOKEN_SECRET` | `change-me` | JWT secret for TUS upload sessions |
| `LSP_VALIDATION_ENABLED` | `true` | Validate + auto-repair LLM-generated code |
| `LSP_WARMUP_ENABLED` | `false` | Start PHP language-server warmup during API startup instead of waiting for the first validation |
| `LSP_MAX_RETRIES` | `2` | LLM repair attempts per invalid block |
| `LSP_TIMEOUT` | `10` | Per-language server diagnostic wait (seconds) |
| `NEO4J_USER` / `NEO4J_PASSWORD` | `neo4j` / `password` | Graph DB auth |
| `RAG_CALLBACK_SECRET` | `change-me` | Internal service auth |
| `OIDC_ISSUER` | unset | OIDC issuer URL; HTTPS required except localhost development issuers |
| `OIDC_LITE_INTERNAL_ISSUER` | Falls back to `OIDC_ISSUER` | Optional provider URL reachable from the Web container when using `just up-lite` |
| `OIDC_CLIENT_ID` | unset | OIDC confidential client ID |
| `OIDC_CLIENT_SECRET` | unset | OIDC client secret |
| `OIDC_REDIRECT_URI` | unset | Exact web callback URI, ending in `/api/auth/callback` |
| `OIDC_BOOTSTRAP_ADMIN_SUB` | unset | OIDC `sub` claim for the first administrator; access is denied when unset |
| `SESSION_SECRET` | unset | Random secret of at least 32 characters for server-side session-key protection |
| `SESSION_TTL_MS` | `28800000` | Session lifetime in milliseconds |
| `SESSION_COOKIE_SECURE` | `true` | Set `false` only for local HTTP development |
| `AUTH_STORE_PATH` | `data/auth-store.json` | Persisted server-side sessions and project memberships |
| `WEB_ORIGIN` | `http://localhost:3000` | Allowed browser origin for credentialed API requests |

Most UI-visible settings are stored in SQLite and editable from the app — see `api/Python-Service/app/settings_store.py`.

## Known limitations / next steps

- Typo correction ships disabled; review `logs/typo-corrections.log` before enabling.
- Graph influence on answers is only partially observable through the `via_graph` result flag; a
  a fuller comparison harness is not yet available.
- Neo4j node IDs are not project-scoped, so identical symbols from separate projects can collide
  or contribute cross-project graph neighbours.
- Runtime request details are sampled observations; missing instrumentation, upstream sampling, or
  older spans can leave fields unavailable. Spans recorded before the sampling ratio was reported
  make the Runtime summary withhold its estimated real volume until they age out of the window.
- Runtime summaries round their window down to whole hours, and a span arriving more than 5 minutes
  after its hour closed is not counted until that hour is rolled up again.
- `just php-otel-setup` covers PHP projects in Docker Compose; projects outside Compose cannot reach a
  ready revision because the host agent only reports containers. Excimer profile upload still
  authenticates with the API's internal secret, so it is not configured automatically; a
  per-project profile token is a follow-up.
- The Symfony adapter's request handling is covered by unit tests but has not yet been verified
  against a live Symfony application.
- Existing `route_feedback` embeddings predate query normalization and may need re-submitting.
- Safety and guardrails are not fully implemented.
