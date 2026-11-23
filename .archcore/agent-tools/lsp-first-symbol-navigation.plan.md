---
title: "Move Symbol Navigation to an LSP-First, Language-Neutral Graph"
status: draft
tags:
  - "api"
  - "code-quality"
---

## Goal

Make symbol navigation (definitions, references, implementations, call hierarchy, document symbols) language-neutral by using language servers as an index-time producer for one `Symbol` graph, per @.archcore/agent-tools/language-servers-feed-the-symbol-graph-at-index-time.adr.md. PHP via phpactor comes first and replaces `php bin/project-inspect` as the main PHP symbol source. This is the first step toward a cross-language assistant; the other PHP-coupled areas are listed under Follow-ups.

Baseline (audit 2026-11-15): LSP handles only answer-block diagnostics; `LspClient` speaks `didOpen`, `publishDiagnostics` and `didClose`; the PHP symbol graph comes from nikic/php-parser; `PhpSymbol` and the `php-parser` default origin are hard-coded in the graph and index layers; `PhpLspValidator` is not wired to `LspRegistry`.

## Tasks

### Phase 0 — Feasibility spike (done 2026-11-15: passed; see Clarifications)

1. Drive phpactor over the pizza-builder fixture with `documentSymbol` and `references` from a script. — @api/Python-Service/php/lsp/ (phpactor install), @api/Python-Service/app/lsp/client.py
2. Record wall time, request count, and reference recall against `project-inspect` output on the same files. — @api/Python-Service/app/inspection/cli.py
2a. Hand-check 50 sampled reference edges from each producer against the source; record precision per producer. — fixture: pizza-builder revision (no code target)
2b. Include Laravel facades, `__call` and Eloquent dynamic properties in the sample; record them separately as unresolved by both. — @api/Python-Service/app/agent/plugins/frameworks/laravel/
3. Check whether phpactor resolves references without `vendor/`, and record which ones it misses.
4. Record the result in this plan's Clarifications; stop and revisit the decision if recall or precision is below the parser's.

### Phase 1 — LSP client and driver (done 2026-11-15; see Clarifications)

5. Add request methods for `documentSymbol`, `references`, and `implementation`; use `callHierarchy` only when the server advertises it. — @api/Python-Service/app/lsp/client.py (done; no call hierarchy request, since phpactor lacks it)
6. Add a workspace-mode session: `rootUri` set to the revision root and server-side indexing enabled. — @api/Python-Service/app/lsp/client.py, @api/Python-Service/app/lsp/registry.py (done: `root_uri`, `initialization_options`, `env`, `cwd`; `$/cancelRequest` on timeout)
7. Extend the adapter interface with `index_settings()` and a per-language list of disabled features. — @api/Python-Service/app/lsp/base.py (done: `IndexingAdapter`, with `prepare_argv`, `namespace_of`, `qualify`, `member_name`, `declaration_kind`)
7a. Set phpactor `composer.enable: false` so it never loads the project's `vendor/autoload.php`; test that setting. — @api/Python-Service/app/lsp/adapters/php.py (done; also PHPStan, Psalm, CS Fixer, CodeSniffer, Prophecy, Behat, Symfony and diagnostics off)
8. Add an `lsp.index` block per language (enabled, limits, settings) to the LSP config. — @api/Python-Service/app/config/lsps.yml (done; `LSP_INDEX_PHP` switches it)
9. Write a driver that turns LSP results into the existing symbol export shape. — @api/Python-Service/app/lsp/indexer.py (done)
9a. Derive call edges from each reference and its enclosing definition, as the SCIP conversion does. — @api/Python-Service/app/lsp/indexer.py (done)
9b. Start from the Phase 0 spike script. — @api/Python-Service/scripts/lsp_phase0/drive.py (done)
10. Apply limits per request (15 s) and per run (900 s); report coverage as complete, partial, or unavailable. — @api/Python-Service/app/lsp/indexer.py (done)
10a. Skip references for framework entry points whose callers are the framework (`run`, `handle`, `boot`, `register`, `up`, `down`, `__invoke`). — @api/Python-Service/app/config/lsps.yml (done)
10b. Drop use sites whose source text names another symbol, so deep search adds no subclass or trait-user hits. — @api/Python-Service/app/lsp/indexer.py (`_names_target`; done)
10c. Run each server with its own empty HOME and XDG folders, so no operator or project config loads. — @api/Python-Service/app/lsp/indexer.py (`isolated_env`; done)

### Phase 2 — Language-neutral graph (done 2026-11-15; see Clarifications)

11. Rename the `PhpSymbol` label to `Symbol` and add a `language` property in every write and query. — @api/Python-Service/app/inspection/graph.py, @api/Python-Service/app/inspection/queries.py, @api/Python-Service/app/index/llamaindex_layer.py (done)
12. Replace the `php-parser` default origin with an origin taken from the producer. — @api/Python-Service/app/inspection/cli.py, @api/Python-Service/app/infra/php_cli.py (`stamp_origin`), @api/Python-Service/app/index/llamaindex_layer.py (`_edge_origin`) (done)
13. Add `lsp` to edge preference (`scip` > `lsp` > parser) in symbol lookup. — @api/Python-Service/app/agent_tools/navigation.py (`_prefer_precise`, was `_prefer_scip`) (done)
14. Add a one-time migration that relabels existing `PhpSymbol` nodes, or mark old collections for re-index. — @api/Python-Service/app/inspection/graph.py (`migrate_legacy_labels`; done)
15. Update the telemetry reader that reads the `PhpSymbol` snapshot. — @api/Python-Service/app/telemetry/runtime.py (done: it reads through the renamed queries; docstring and @api/docs/telemetry/ updated)

### Phase 3 — Wire PHP indexing to LSP (done 2026-11-15; see Clarifications)

16. Call the LSP driver from snapshot indexing, falling back to `project-inspect` when LSP coverage is unavailable. — @api/Python-Service/app/index/indexing_service.py (`_with_lsp`), @api/Python-Service/app/lsp/jobs.py (`merge_into`) (done)
17. Decide where the driver runs. — decided by the user: the consented analyzer worker, else the API process (spec 32a–32c); @api/analyzer-worker/Dockerfile, @api/analyzer-worker/worker.py, @api/Python-Service/app/code_quality/remote_worker.py (`run_lsp_job`) (done)
18. Keep `project-inspect` for skeleton, attributes, and method reads until Tree-sitter or LSP covers them. — @api/Python-Service/app/inspection/cli.py (kept)
18-1. Use LSP in the other paths that still build the graph from the parser only: saved analyses, telemetry source snapshots, and embedding rebuilds. — @api/Python-Service/app/pipelines/orchestrator.py, @api/Python-Service/app/analysis/analysis_service.py, @api/Python-Service/app/telemetry/source.py, @api/Python-Service/app/index/embedding_rebuild.py; one helper, @api/Python-Service/app/lsp/jobs.py (`enrich_export`) (done; working trees skipped, as for SCIP)

### Phase 3a — Code Quality reads graph edges (done 2026-11-15; see Clarifications)

18a. Build the call-reachability adjacency from graph `CALLS` edges instead of chunk payload `calls`. — @api/Python-Service/app/code_quality/flow_tracing.py (`graph_edges`), @api/Python-Service/app/inspection/graph.py (`call_edges`: REFERENCES of kind method_call, static_call, function_call) (done)
18b. Pick one edge per call site in preference order `scip` > `lsp` > parser. — @api/Python-Service/app/code_quality/flow_tracing.py (done, per caller–callee pair)
18c. Record each trace edge's origin and bump `TRACER_VERSION` so old and new reports compare as non-equivalent. — @api/Python-Service/app/code_quality/flow_tracing.py (done: `php-call-reachability-3`, `edge_source`, `edge_origins`)
18d. Keep chunk payload calls as the fallback when the revision has no graph edges, and report coverage partial. — @api/Python-Service/app/code_quality/flow_tracing.py (done: limit reason `graph_edges_unavailable`)
18e. Leave request-source and sink patterns PHP-only; register other languages' patterns later as separate trace analyzers. — @api/Python-Service/app/code_quality/flow_tracing.py (kept)

### Phase 3b — Runtime edges from indexr-otel (done 2026-11-15 except a live run; see Clarifications)

Baseline: `upsert_runtime_facts` already writes `RUNTIME_CALLS`, route, and table facts from indexr-otel spans onto the commit-scoped graph (@api/Python-Service/app/inspection/graph.py), but no symbol lookup, navigation tool, or flow trace reads them. The library roadmap (`../indexr-otel-library/docs/roadmap.md`, Phase 4) records parent–child spans with `count` and `last_seen`, plans an Excimer sampling profiler for broader call coverage, and plans a static-versus-runtime diff.

18f. Check which calls the installed adapter records today: hooks only, or hooks plus profiler samples. — `../indexr-otel-library/docs/attribute-schema.md`, @scripts/install-indexr-otel.sh (done: hooks always, profiler when `ext-excimer` is installed)
18g. Return `RUNTIME_CALLS` callers and callees in symbol lookup, with origin `runtime`, `count`, and `last_seen`. — @api/Python-Service/app/inspection/queries.py (`call_hierarchy`), @api/Python-Service/app/agent_tools/navigation.py (done; references unchanged)
18h. Rank edges confirmed at runtime first, and mark static edges never seen at runtime as unconfirmed, not absent. — @api/Python-Service/app/agent_tools/navigation.py (`_with_runtime`; done)
18i. Add runtime-only edges (facades, `__call`, Eloquent relations, container bindings) to flow-tracing adjacency as origin `runtime`. — @api/Python-Service/app/code_quality/flow_tracing.py, @api/Python-Service/app/inspection/graph.py (`call_edges`) (done; ranked below every static producer)
18j. Ignore runtime edges whose commit differs from the selected revision; report them as stale. — @api/Python-Service/app/inspection/graph.py, @api/Python-Service/app/telemetry/snapshot.py (`runtime_reference`), @api/Python-Service/app/telemetry/aggregation.py (done)
18k. Report runtime coverage per revision: share of routes and indexed methods seen running. — @api/Python-Service/app/telemetry/runtime.py, @api/Python-Service/app/inspection/service.py (`runtime_coverage`; done: methods as a share, routes as a count)
18n. Verify on a running pizza project: a facade or Eloquent call shows as a runtime-only caller. — needs pizza running with indexr-otel and the Runtime Collector (open)
18l. Move runtime writes to the `Symbol` label in the same change as task 11. — @api/Python-Service/app/inspection/graph.py (`_RUNTIME_*_CYPHER`) (done with task 11)
18m. Amend source-code-navigation-tools clause 11 to add origin `runtime`. — @.archcore/agent-tools/source-code-navigation-tools.spec.md (done 2026-11-15)

### Phase 4 — Plugin validator wiring (done 2026-11-15; see Clarifications)

19. Inject the live `LspRegistry` into `PhpLanguagePlugin` through the DI container. — @api/Python-Service/app/agent/core/di.py (`default_container`), @api/Python-Service/app/agent/plugins/__init__.py (`register_builtins(registry, container)`), @api/Python-Service/app/agent/__init__.py (`default_registry`) (done)
20. Remove the scope-B branch in `PhpLspValidator` and make it language-generic (`LspValidator(language)`). — @api/Python-Service/app/agent/plugins/validators.py (done; `PhpLspValidator` is the PHP subclass)

### Phase 5 — Second language (proves the abstraction) (skipped by the user on 2026-11-15)

21. Add `gopls` as the second language server with `GOPROXY=off` and `GOFLAGS=-mod=readonly`. — @api/Python-Service/app/config/lsps.yml, new adapter `go.py` under @api/Python-Service/app/lsp/adapters/
21a. Install a pinned `gopls` in the image that runs the driver (task 17). — @api/analyzer-worker/Dockerfile
22. Index the Go Docker read proxy fixture and compare `lsp` edges with its Tree-sitter edges. — @api/Python-Service/app/projects/ts_index.py
22a. Hand-check every differing edge and record precision and recall for both producers. — fixture: `docker-proxy` upload (no code target)
22b. Report modules with missing dependencies as partial coverage instead of failing the run. — new `go.py` adapter (no existing target)

### Phase 6 — Verification

23. Add client tests for the new request methods with a fake server. — @api/Python-Service/app/tests/test_lsp_client.py (done)
24. Add driver tests for limits, partial coverage, and out-of-revision paths. — @api/Python-Service/app/tests/test_lsp_indexer.py (done)
25. Update graph and lookup tests for the `Symbol` label and `lsp` origin. — @api/Python-Service/app/tests/test_symbol_graph_language.py, @api/Python-Service/app/tests/test_neo4j_integration.py, @api/Python-Service/app/tests/test_navigation_tools.py, @api/Python-Service/app/tests/test_hybrid_graph_legs.py (done)
26. Re-run the pizza agent evaluation and compare facts and citations with the parser baseline. — @api/Python-Service/scripts/agent_eval.py (open: deferred by the user on 2026-11-15; it needs either a throwaway Neo4j loaded twice, parser then LSP, or a relabel and re-index of the shared Neo4j, plus about 20 remote-model answers)

## Acceptance Criteria

- `code_symbol_lookup` on a PHP revision returns edges with origin `lsp` and items that name their language.
- On the pizza fixture, LSP reference recall is at least the `project-inspect` recall measured in Phase 0.
- On the pizza fixture, LSP reference precision in the hand-checked sample is at least the parser's.
- Go indexes through the same driver without new parser code, and its `lsp` edges beat Tree-sitter edges on hand-checked precision and recall.
- Code Quality call traces read graph edges, name each edge's origin, and fall back to chunk calls with partial coverage.
- No language server runs a project build, script, or plugin, and the driver has no network access.
- Indexing reports LSP coverage as complete, partial, or unavailable; nothing in the graph is invented.
- On a running pizza project, symbol lookup shows runtime callers of a facade or Eloquent relation that no static producer resolves.
- Runtime edges from another commit never appear in lookups or traces for the selected revision.
- The pizza evaluation shows no drop in facts or citation accuracy.
- All tests pass in `docker compose run python-api pytest`.

## Dependencies

- @.archcore/agent-tools/language-servers-feed-the-symbol-graph-at-index-time.adr.md — the decision this plan carries out.
- @.archcore/code-quality/external-static-analyzer-integrations.spec.md — clauses 32–37 and failure clause 8 (LSP indexer).
- @.archcore/agent-tools/source-code-navigation-tools.spec.md — clauses 11 and 11a (origins and the language-neutral graph).
- @.archcore/code-quality/remote-analyzer-worker.spec.md — if task 17 places the driver in the worker.
- Hazard (found in Phase 0): phpactor's default `composer.enable: true` loads the project's `vendor/autoload.php`, which runs project code → task 7a → startup-settings test.
- Hazard: rust-analyzer build scripts and procedural macros, `tsserver` plugins, and `gopls` module downloads run project code or reach the network → each adapter turns them off → a test per adapter checks the startup settings; owner: Phase 1.
- Hazard: one `references` request per definition can make indexing slow on large projects → request and time limits with partial coverage → measured in Phase 0 and the driver tests.
- Hazard: `gopls` loads module dependencies and may try to download them → `GOPROXY=off` and no network → modules with missing dependencies report partial coverage; owner: Phase 5.
- Hazard: switching flow tracing to graph edges changes Code Quality evidence → bump the tracer version so comparisons mark old reports non-equivalent → checked by comparing pizza traces before and after; owner: Phase 3a.
- Hazard: runtime edges cover only executed code → lookups mark static edges as unconfirmed, never absent, and answers state runtime coverage → checked by tests on a graph with partial runtime data; owner: Phase 3b.
- Hazard: the running app may not match the indexed commit → runtime edges are scoped to their commit and ignored otherwise (task 18j) → checked with a stale-commit fixture.
- Hazard: method-level tracing slows the developer's app → rely on hooks plus the sampling profiler, whose roadmap gate is under 5% p95 latency; owner: indexr-otel-library.
- Hazard: renaming `PhpSymbol` breaks existing collections and telemetry snapshots → migration or re-index (task 14) → checked on an existing pizza collection.

## Follow-ups (out of scope)

- Move migrations, backend routes, PHP design metrics, and Joern scripts behind framework and language plugins (@api/Python-Service/app/analysis/schema_migrations.py, @api/Python-Service/app/projects/backend_routes.py, @api/Python-Service/app/code_quality/php_design.py, @api/Python-Service/app/code_quality/external_analyzers.py).
- Make the `php_*` telemetry fields (`php_cpu_time_us`, `php_app_time_ms`) runtime-neutral (@api/Python-Service/app/telemetry/runtime.py).
- Remove `php_base_url` and Artisan output parsing from the orchestrator (@api/Python-Service/app/pipelines/orchestrator.py).

## Clarifications

- 2026-11-15: the user chose language servers as an index-time producer, not live queries per tool call or a hybrid.
- 2026-11-15: Phase 3b added. Runtime data from indexr-otel resolves Laravel dynamic calls (facades, magic methods, Eloquent relations, container bindings) that neither the parser nor phpactor can see. It complements static indexing, because it covers only code that ran and needs a running, configured project.
- 2026-11-15: Phase 0 result — **passed; proceed.** Setup: phpactor 2025.12.21.1 on host PHP 8.4, pizza-builder `app routes config database bootstrap` (198 PHP files) copied without `vendor/` and `storage/`; `composer.enable`, watchers and diagnostics off; scripts in @api/Python-Service/scripts/lsp_phase0/ (`drive.py`, `compare2.py`; the JSON outputs were not kept). Baseline `project-inspect index`: 737 definitions, 3,660 references, 0.4 s.
  - Speed: `index:build` 7 s; 709 document symbols in 0.3 s; 657 `references` requests in 75 s (median 5 ms, p95 217 ms, max 5.0 s), 131 MB peak, no timeouts or errors. The 10 seeder `run()` methods took 47 of the 75 s, because phpactor scans every file containing the name; `ApiResponse::successResponse` took 3.2 s. Hence tasks 10 and 10a.
  - Recall, member call sites to project definitions keyed by file, line and member: 732 found by both; 20 by LSP only (18 the parser left unresolved, such as `$this->status->canRetry()` and `$cart->items()`, plus 2 constructors); 163 by the parser only, and all 163 are framework calls whose names collide with a project method (`Pizza::query()`, `Schema::create`, `Route::delete`): none is on a class that defines the member. LSP recall on project edges is therefore at least the parser's.
  - Attribution: in 156 shared call sites LSP names the declaring trait (`ApiResponse::successResponse`) and the parser names the calling controller; LSP's is the definition the graph needs.
  - Class references: LSP misses 131 of the parser's 758 `new`/`catch`/static/constant uses; 125 are `self::Case` inside the enum itself, and 128 of the 131 lines carry an LSP member edge. LSP also finds type-hint and import uses the parser does not record (1,416 class uses in total).
  - Precision (task 2a): 50 random parser member edges and 50 random LSP member edges checked against the source line: 50/50 correct for each.
  - Laravel dynamic calls (task 2b): 127 references are unresolved by both; about 40 point at project code (`app(PublishPizzaAction::class)->execute()`, `$user->isAdmin()` after `$request->user()` or `Auth::user()`, methods on Eloquent query results); the rest are framework calls. Neither producer fixes these; a Laravel framework-plugin hint (container bindings, `Auth::user()` return type) is the candidate follow-up.
  - Capabilities: phpactor advertises `references`, `implementation`, `documentSymbol` and `definition`, but no `callHierarchyProvider`. Hence tasks 5 and 9a.
  - Security: phpactor's default `composer.enable: true` would load the project's `vendor/autoload.php`. Hence task 7a.
- 2026-11-15: Phase 1 done (uncommitted on branch `feature/agent-code-tools` in the api repository).
  - Live run on the Phase 0 pizza copy through `LspRegistry.index_workspace`, inside the `api-python-api` image with `--network none`: status `complete`, 198 files, 709 definitions, 1,528 references, 50 relations, no timeouts, 82 s (prepare 4 s, references 72 s). Host run: 92 s.
  - Through `InspectionService.index` with a counting graph stand-in: all 1,528 LSP references link to nodes (parser: 702 of 3,660). Every edge the parser links is also linked by LSP (686 shared, 0 parser-only); LSP links 645 more. Relations match the parser exactly (50/50).
  - Found while building: phpactor answers only for open documents (the driver opens each definition's file once); with deep search on, it returns uses of subclasses and of classes that use a trait (`OrderItem::create()` for `HasUlid`); 543 such hits are dropped by the name check (task 10b). Deep search off was 20 s faster but found 22% fewer references, so it stays on. Traits arrive as classes; the PHP adapter reads the keyword (`uses_trait` relations).
  - Request limit: 1 s cut the most-used methods (`ApiResponse::successResponse` needs over 5 s), losing their edges, so the default is 15 s and the 900 s run limit bounds the run.
  - The API image's global `/root/.config/phpactor/phpactor.json` enables PHPStan and sets `language_server.enable_trust_check: false`; the isolated XDG folders keep indexing runs from reading it.
  - Tests: 87 pass in the API image (`test_lsp_indexer.py` new; client, adapter and registry tests extended). On the host, `test_stream_answer.py` fails on a NumPy/SciPy mismatch unrelated to this work.
  - Not done in Phase 1: the driver is not called from indexing yet (Phase 3), and properties get definitions but no reference edges.
- 2026-11-15: Phase 1 gap closed: properties now get `property_fetch` edges that link to the property node (@api/Python-Service/app/inspection/service.py `_target_key`). Pizza: 5 edges, all correct; the other properties are Laravel settings (`$fillable`, `$casts`, `$model`) that only the framework reads. Open: constructor-promoted properties (`private AuditAction $auditAction`) are not document symbols in phpactor, so their uses stay unlinked.
- 2026-11-15: Phase 2 done (uncommitted on `feature/agent-code-tools`).
  - Label `Symbol` in every Cypher statement (writer, queries, runtime facts, hybrid retrieval legs); every node carries `language` (from the record, else the file suffix via `LANG_EXTENSIONS`) and `origin`.
  - Origin: the PHP CLI exports stamp `php-parser` where they are produced; other producers already stamp theirs. Records with no origin fall back to `php-parser` for PHP and `unknown` otherwise; read-side rows without an origin are pre-origin graphs and read as `php-parser`.
  - Migration: the first graph connection per process relabels `PhpSymbol` nodes to `Symbol` in batches of 10,000 and sets `language` from the file suffix; ids and edges stay, so no re-index is needed. A failure is logged and retried on the next connection.
  - Checks: the developer's own `neo4j` container was not touched, because older branches still read `PhpSymbol`; a throwaway Neo4j 5.26 ran the integration tests (3 pass; the full-index test needs Qdrant). Loading the pizza LSP export there: 709 nodes, 1,533 links, 0 unlinked in 2.4 s; `ApiResponse::successResponse` returns 59 callers, all `lsp`/`php`; `Controller` has 19 subclasses.
  - Unit suite in the API image: 1,566 pass. All 26 failures also fail on `HEAD` or come from other uncommitted work on the branch (ClickHouse credentials, PHP CLI binaries, `force_reindex`, `embed_hash`, a telemetry route returning 422); none comes from Phases 1–2.
  - Deployment note: once this runs against the shared Neo4j, branches without this change no longer see the relabelled nodes.
- 2026-11-15: Phase 3 done (uncommitted on `feature/agent-code-tools`).
  - Location (user decision): snapshot indexing asks the project's consented analyzer worker first (`lsp` analyzer, `run_lsp_job`), then indexes in the API process; when both are unavailable the parser export stands. The worker's reply is validated as untrusted (kinds, files in the bundle, relation kinds, sizes), like SCIP's.
  - Merge: on complete coverage LSP references replace the PHP parser's (SCIP and other producers stay); on partial coverage both are kept with LSP written last. Parser-only definitions are kept.
  - Worker image: phpactor is built in a Composer stage with the worker's own PHP packages and copied to `/opt/php-lsp`; the worker stage still has no Composer. Added PyYAML 6.0.2 and `LOG_DIR=/tmp/analyzer-logs`. Built as `api-analyzer-worker:lsp-test` (5.39 GB, was 5.06 GB); `:latest` untouched.
  - Network: the worker joins the default network (for its published port) and the API container has network, so neither blocks outbound traffic at the process level. Spec clause 32 was reworded and a constraint states that isolation rests on clause 33 (project-code features off).
  - Checks: in the worker image with the compose hardening (read-only root, noexec `/tmp`, no capabilities, uid 10001, 4 GB, 2 CPUs) and `--network none`, `run_lsp` on pizza's 198 PHP files: complete, 709 definitions, 1,533 references, 77 s; `/v1/capabilities` reports `lsp` ready. In the API image, `_with_lsp` on the real parser export (737 definitions, 3,660 references): merged 779 definitions (70 parser-only kept) and 1,533 references, all `lsp`, 78 s. Not checked: the HTTPS round trip to a running worker with mutual TLS (covered by unit tests with a fake transport).
  - Cost: indexing a pizza-sized upload takes about 80 s longer.
  - Tests: @api/Python-Service/app/tests/test_lsp_jobs.py (16). Unit suite in the API image: 1,582 pass, the same 26 unrelated failures as after Phase 2.
- 2026-11-15: Task 18-1 and Phase 3a done (uncommitted).
  - 18-1: the worker → API → parser logic moved from `IndexingService._with_lsp` into `lsp.jobs.enrich_export`, now called by snapshot indexing, Git/CI source snapshots (committed revisions only), embedding rebuilds (committed revisions only), the orchestrator's PHP index step and the analysis service's PHP graph. Without an archive it zips the workspace's LSP-language files for the worker; the API fallback skips dependency trees when deciding whether to run.
  - 3a: the tracer reads the revision's `sym_<collection>` call edges. Chunk calls containing `__unknown::` still count as unresolved in graph mode, so `complete` is never claimed for calls nobody resolved (semantic-flow-tracing spec 12–13).
  - Pizza (198 PHP files, LSP complete): resolved call edges 182 → 406, unresolved 1,453 → 640; no trace either way (pizza's code has no request-to-sink path, as the earlier Joern check found). With a planted controller that reads `$request->input()` and calls a repository through a constructor-promoted property that runs `DB::select` with the input: chunk calls find no trace (the parser recorded `__unknown::sorted`); graph edges find `ReportController::show → ReportRepository::sorted`, origin `lsp`.
  - Tests: @api/Python-Service/app/tests/test_lsp_jobs.py (19), @api/Python-Service/app/tests/test_code_quality_flow_tracing.py (4 new; the determinism test now passes graph edges). Unit suite in the API image: 1,589 pass, the same 26 unrelated failures.
  - Not checked: an assessment run end to end against Neo4j and Qdrant with a real snapshot.
- 2026-11-15: Phase 3b done except the live run (task 18n; uncommitted).
  - 18f: `setup-php-telemetry.sh` always writes the method-hook manifest (routes, controllers, service namespaces; capped at 500 methods); the Excimer profiler runs by default only when `ext-excimer` is installed. `RUNTIME_CALLS` edges record their evidence in `sources` (span, hook, profile).
  - 18j: the aggregator keyed facts by the metadata commit. Working trees (no commit) and uploads were therefore skipped entirely rather than mis-assigned. It now writes into the revision's own snapshot (`runtime_reference`: commit, `wt_<archive>` or `upload_<archive>`), and stamps `revision_id` on each `RUNTIME_CALLS` edge; readers drop edges of another revision or without one and count them as stale. The runtime UI reader uses the same reference. Not changed: the request-analysis and business-rule readers (@api/Python-Service/app/telemetry/analysis.py, @api/Python-Service/app/code_quality/rule_code.py) still require a commit, so they do not support working trees.
  - 18g/18h: `call_hierarchy` returns `runtime_callers`/`runtime_callees`, the stale count and whether the target was in an aggregation window. `code_symbol_lookup` marks static callers/callees `confirmed` (with count and `last_seen`) or, when runtime data exists, `unconfirmed`; adds `runtime_only` rows with origin `runtime` and their `sources`; lists confirmed and runtime-only first; adds a `runtime` block with coverage. Spec clauses 11b–11d.
  - 18i: flow tracing reads current `RUNTIME_CALLS` edges as origin `runtime`, ranked below every static producer, so they fill in only calls nothing static resolved.
  - 18k: `runtime_coverage` reports methods seen as a share of indexed methods with a symbol id, plus the number of routes observed (there is no static route total to divide by); shown in symbol lookup and the runtime summary.
  - Checks: on a throwaway Neo4j 5.26, a seeded revision with one static edge, two current runtime edges and one edge from another revision: lookups return the static caller, two runtime callers and one stale edge; coverage 3 of 4 (75%); the tracer's edge read returns `lsp` plus two `runtime` edges. Unit suite in the API image: 1,597 pass, the same 26 unrelated failures (two existing tests updated for the new `revision_id` argument and the two extra runtime queries).
- 2026-11-15: Phase 4 done (uncommitted).
  - DI: `default_container()` registers `lsp_registry` (the live `lsp.registry`, imported on first resolve). `register_builtins` takes an optional container; without one plugins get no registry, so tests stay hermetic. `agent.default_registry()` passes the default container, so its PHP plugin validates with phpactor.
  - Validator: `LspValidator(language, extensions, registry)` checks each file (sorted, at most 200, the rest reported) off the event loop; registry `errors` fail validation, `warnings` do not; issue locations are `path:line`. A disabled language or no registry falls back to the structural check. The old scope-B branch read a `diagnostics` key the registry never returns, so it could not have reported anything.
  - Live check in the API image (`--network none`) through `agent.default_registry()`: two PHP files, one missing a semicolon: invalid, 1 error at `src/Broken.php:10` ("syntax error, unexpected token \"}\", expecting \";\""), 7 s.
  - Tests: 6 new in @api/Python-Service/app/tests/test_agent_validators.py. Unit suite in the API image: 1,603 pass, the same 26 unrelated failures. Nothing calls the validators in production yet; they serve the planned agent loop.
- 2026-11-15: the user skipped Phase 5 (gopls). The acceptance criterion about Go stays open, and nothing yet shows that a second language needs no new parser code.
- 2026-11-15: quality review. Python and TypeScript already have precise SCIP edges, so the second language moved to Go, where Tree-sitter name matching is the only source. Phase 0 now measures precision as well as recall. Phase 3a added, because Code Quality flow tracing read only PHP chunk calls and would not benefit from better graph edges.

## Declared Delta

- create adr language-servers-feed-the-symbol-graph-at-index-time
- amend spec external-static-analyzer-integrations — LSP indexer clauses 32–37, failure clause 8, constraint
- amend spec source-code-navigation-tools — origins `lsp` and `python-ast` in clause 11, clause 11a, no live LSP in tools
- create plan lsp-first-symbol-navigation
- none spec remote-analyzer-worker — amend only if task 17 places the driver in the worker
- amend spec source-code-navigation-tools — origin `runtime` in clause 11 (task 18m)
- none spec semantic-flow-tracing — clauses 3–5 already allow traversal over declared graph edge types
- none plan implement-source-code-navigation-tools — its Phase 5 SCIP work stays; scip-php stays deferred
