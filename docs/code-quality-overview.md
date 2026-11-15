# Code Quality assessment

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
