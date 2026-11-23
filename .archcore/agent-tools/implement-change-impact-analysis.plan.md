---
title: "Implement Change Impact Analysis over the Symbol Graph"
status: draft
tags:
  - "api"
  - "code-quality"
---

## Goal

Build change impact analysis on the existing `Symbol` graph, per @.archcore/agent-tools/change-impact-analysis.spec.md: one engine, exposed as the `code_impact` agent tool and `POST /code-tools/impact`, and used by the business-rule impact check. No new producer and no new graph store; the engine reads what LSP, SCIP, parsers and indexr-otel already write.

Baseline (2026-11-22): `call_hierarchy` returns one hop of callers and runtime callers (@api/Python-Service/app/inspection/queries.py); `changed_symbols` maps diff hunks to symbol ids (@api/Python-Service/app/code_quality/rule_code.py); `check_impact` reviews only rules linked to touched symbols (@api/Python-Service/app/code_quality/rule_assist.py); `RuntimeRoute -[HANDLED_BY]-> Symbol` edges exist (@api/Python-Service/app/inspection/graph.py). No multi-hop walk, route attribution or test attribution exists.

## Tasks

### Phase 1: Engine core over symbol names

1. [x] T1 Add a batched upstream-neighbour query for a symbol id set on one collection

   - files: @api/Python-Service/app/inspection/queries.py @api/Python-Service/app/tests/test_change_impact.py
   - covers: change-impact-analysis#6
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k walks_callers` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k walks_callers` @ f428224

2. [x] T2 Write the impact engine resolving names and walking upstream breadth-first with via paths

   - files: @api/Python-Service/app/inspection/impact.py @api/Python-Service/app/tests/test_change_impact.py
   - covers: change-impact-analysis#1, change-impact-analysis#3-4, change-impact-analysis#7, change-impact-analysis#F1
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k "resolve or ambiguous or via or unready"` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k "resolve or ambiguous or via or unready"` @ f428224

3. [x] T3 Keep one edge per reached symbol in origin preference order

   - files: @api/Python-Service/app/inspection/impact.py @api/Python-Service/app/tests/test_change_impact.py
   - covers: change-impact-analysis#8
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k precise_origin` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k precise_origin` @ f428224

4. [x] T4 Enforce depth, node and time limits and report the limit reached

   - files: @api/Python-Service/app/inspection/impact.py @api/Python-Service/app/tests/test_change_impact.py
   - covers: change-impact-analysis#F7
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k limits_truncate` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k limits_truncate` @ f428224

5. [x] T5 Return unavailable coverage and unresolved inputs without fabricated edges

   - files: @api/Python-Service/app/inspection/impact.py @api/Python-Service/app/tests/test_change_impact.py
   - covers: change-impact-analysis#F4, change-impact-analysis#F6
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k "unresolved_inputs or graph_down"` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k "unresolved_inputs or graph_down"` @ f428224

6. [x] T6 Order impacted symbols by depth, confirmation, then path and line

   - files: @api/Python-Service/app/inspection/impact.py @api/Python-Service/app/tests/test_change_impact.py
   - covers: change-impact-analysis#13
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k impacted_order` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k impacted_order` @ f428224

### Phase 2: Diff and revision-pair inputs, routes, tests, runtime marks

7. [x] T7 Map a unified diff to changed symbols through `changed_symbols` and walk from them

   - files: @api/Python-Service/app/inspection/impact.py @api/Python-Service/app/tests/test_change_impact.py
   - covers: change-impact-analysis#2, change-impact-analysis#F3
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k "diff_maps or diff_without_hunks"` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k "diff_maps or diff_without_hunks"` @ 853e6bd

8. [x] T8 Mark symbols added, removed or with changed body hashes between two revisions

   - files: @api/Python-Service/app/inspection/impact.py @api/Python-Service/app/tests/test_change_impact.py
   - covers: change-impact-analysis#5, change-impact-analysis#F5
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k "revision_pair or base_revision_without_graph"` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k "revision_pair or base_revision_without_graph"` @ 853e6bd

9. [x] T9 Walk callers of inherited methods through subclasses that do not override them

   - files: @api/Python-Service/app/inspection/queries.py @api/Python-Service/app/inspection/impact.py @api/Python-Service/app/tests/test_change_impact.py
   - covers: change-impact-analysis#9
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k inherited_method` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k inherited_method` @ 853e6bd

10. [x] T10 Attribute runtime `HANDLED_BY` routes and static backend routes to reached symbols

   - files: @api/Python-Service/app/inspection/queries.py @api/Python-Service/app/inspection/impact.py @api/Python-Service/app/tests/test_change_impact.py
   - covers: change-impact-analysis#10
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k routes_from_runtime` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k routes_from_runtime` @ 853e6bd

11. [x] T11 Collect test symbols, stop past them, and mark static edges confirmed or unconfirmed

   - files: @api/Python-Service/app/inspection/impact.py @api/Python-Service/app/tests/test_change_impact.py
   - covers: change-impact-analysis#11-12
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k "tests_collected or runtime_confirmation"` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k "tests_collected or runtime_confirmation"` @ 853e6bd

12. [x] T12 Persist LSP and SCIP coverage per revision and report partial producer coverage

   - files: @api/Python-Service/app/lsp/jobs.py @api/Python-Service/app/index/indexing_service.py @api/Python-Service/app/inspection/service.py @api/Python-Service/app/inspection/graph.py @api/Python-Service/app/inspection/queries.py @api/Python-Service/app/inspection/impact.py @api/Python-Service/app/tests/test_change_impact.py @api/Python-Service/app/tests/test_lsp_jobs.py
   - covers: change-impact-analysis#F8
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k partial_producer_coverage` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k partial_producer_coverage` @ 853e6bd

### Phase 3: Agent tool and HTTP route

13. [x] T13 Add an impact service resolving revision, collection, definitions and static routes per request

   - files: @api/Python-Service/app/inspection/impact_service.py @api/Python-Service/app/tests/test_impact_route.py
   - covers: none
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_impact_route.py -k service_resolves` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_impact_route.py -k service_resolves` @ 825296a

14. [x] T14 Register the read-only `code_impact` agent tool with project-relative, cited rows

   - files: @api/Python-Service/app/agent_tools/navigation.py @api/Python-Service/app/tests/test_navigation_tools.py
   - covers: change-impact-analysis#14
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_navigation_tools.py -k code_impact` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_navigation_tools.py -k code_impact` @ 825296a

15. [x] T15 Add `POST /code-tools/impact` returning the tool's report and 422 on bad input

   - files: @api/Python-Service/app/routers/code_tools.py @api/Python-Service/app/tests/test_impact_route.py
   - covers: change-impact-analysis#15, change-impact-analysis#F2
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_impact_route.py -k "route_matches_tool_report or requires_exactly_one_input_kind"` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_impact_route.py -k "route_matches_tool_report or requires_exactly_one_input_kind"` @ 825296a

### Phase 4: Business-rule impact check reads impacted symbols

16. [x] T16 Report each impacted symbol's canonical `symbol_id` next to its graph node id

   - files: @api/Python-Service/app/inspection/impact.py @api/Python-Service/app/tests/test_change_impact.py
   - covers: none
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k canonical_symbol_id` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_change_impact.py -k canonical_symbol_id` @ 7684441

17. [x] T17 Review rules linked to symbols impacted up to depth 2 in `check_impact`

   - files: @api/Python-Service/app/code_quality/rule_assist.py @api/Python-Service/app/tests/test_code_quality_rule_assist.py
   - covers: change-impact-analysis#16
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_code_quality_rule_assist.py -k includes_rules_of_callers` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_code_quality_rule_assist.py -k includes_rules_of_callers` @ 7684441

18. [x] T18 Mark indirect rule links with their via path and rank direct links first

   - files: @api/Python-Service/app/code_quality/rule_assist.py @api/Python-Service/app/tests/test_code_quality_rule_assist.py
   - covers: change-impact-analysis#17
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_code_quality_rule_assist.py -k indirect_rule_links` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_code_quality_rule_assist.py -k indirect_rule_links` @ 7684441

### Phase 5: Verification on pizza-builder

19. [x] T19 Write an impact evaluation script that checks the acceptance criteria against a live revision

   - files: @api/Python-Service/scripts/impact_eval.py
   - covers: none
   - done when: `docker cp scripts/impact_eval.py python_api:/tmp/impact_eval.py && docker exec python_api python /tmp/impact_eval.py --project pizza --revision latest --base-revision <older pizza revision>` exits 0 and prints `pass` for timing, citations, routes and comparison
   - proof: `docker cp scripts/impact_eval.py python_api:/tmp/impact_eval.py && docker exec python_api python /tmp/impact_eval.py --project pizza --revision latest --base-revision <older pizza revision>` @ 12cc8e5

## Waiting on

- A ready pizza-builder revision indexed into the shared Neo4j after the `Symbol` relabel, with LSP coverage recorded. (owner: user)
- A second ready pizza-builder revision that differs from the first in exactly one method, for the revision comparison criterion. (owner: user)
- pizza-builder running with indexr-otel and the Runtime Collector, so runtime confirmation marks have data. (owner: user; shared with task 18n of the LSP-first plan)

## Acceptance Criteria

- On pizza-builder, an impact request for `ApiResponse::successResponse` at the default depth finishes within the 10 s tool limit.
- On pizza-builder, a diff touching one repository method yields at least one route and every impacted row cites `path:line` that exists in the revision.
- Comparing two indexed pizza revisions that differ in one method reports that method as changed and no unchanged method as changed.
- The business-rule impact check on a fixture with a rule linked only to a caller now reviews that rule; with no linked rules it still skips the model.
- `docker compose run python-api pytest` shows no new failures against the 26 known unrelated ones recorded in @.archcore/agent-tools/lsp-first-symbol-navigation.plan.md.

## Dependencies

- @.archcore/agent-tools/change-impact-analysis.spec.md — the contract this plan builds.
- @.archcore/agent-tools/lsp-first-symbol-navigation.plan.md — the `Symbol` label, edge origins and runtime edges the walk reads (Phases 2–3b done; uncommitted on `feature/agent-code-tools`).
- Hazard: hub symbols and framework entry points fan out to hundreds of callers → node and time limits with truncation (T4) → a test on a synthetic hub of 2,500 callers; owner: Phase 1.
- Hazard: Laravel dynamic calls (facades, `__call`, Eloquent) have no static edge → runtime edges fill some, the rest stay in `unresolved` and coverage reads partial → Phase 2 test on partial coverage.
- Hazard: an older base revision may have no graph left after cleanup → `coverage: unavailable` for that revision → Phase 2 test F5.
- Hazard: indirect rule links widen the model prompt in `check_impact` → keep `MAX_IMPACT_RULES` and rank direct links first → Phase 4 test.
- Hazard: the shared Neo4j still holds `PhpSymbol` nodes for branches without the relabel → the engine reads only `Symbol` and relies on `migrate_legacy_labels`; owner: Phase 1.

## Clarifications

- 2026-11-22: surfaces, inputs and rule-check reuse were chosen by the user; see the spec's Clarifications.
- 2026-11-22: Phase 1 T1–T6 proven at f428224 on `feature/agent-code-tools` (13 tests pass in a clean worktree of that commit, outside the branch's other uncommitted work). Code: `SymbolGraphQuery.available` and `SymbolGraphQuery.upstream` (@api/Python-Service/app/inspection/queries.py, `_UPSTREAM`), and the engine @api/Python-Service/app/inspection/impact.py (`analyze_symbols`, `RevisionRef`, `ImpactError`). Checks: 13 tests in @api/Python-Service/app/tests/test_change_impact.py, which failed before the change (module missing) and pass after it. The neighbouring suites (runtime edges, symbol graph language, navigation, flow tracing, rule assist) still pass: 87 tests. On a throwaway Neo4j 5.26 (`--network none`) the `_UPSTREAM` Cypher returned the REFERENCES caller, the `implements` declarer and the same-revision runtime caller. It left out a `uses_trait` declaration, a runtime edge from another revision, and a node of another collection.
- 2026-11-22: Phase 2 expanded from the code at f428224. Diff input reuses `rule_code.changed_symbols` with the definitions from `rule_code.load_revision`. Revision pairs reuse `symbol_hashes` on both archives. Static routes come from `projects/backend_routes.py` (`action` names the controller method), runtime routes from `RuntimeRoute -[HANDLED_BY]-> Symbol`. No LSP or SCIP status is stored with a revision today: `lsp.jobs.merge_into` reads the export's `status` and then drops it, so T12 has to persist it before the engine can report it.
- 2026-11-22: Phase 2 T7–T12 proven at 853e6bd (every task check, plus 59 tests across the impact, LSP job and inspection suites, passes in a clean worktree of that commit).
- 2026-11-22: Phase 3 expanded from the code at 853e6bd. The router follows `routers/code_tools.py` `read_citation`, which resolves the revision with `rag.revision_source.resolve_revision`, relies on the web server to authorize the project, and maps errors to HTTP codes. The tool follows `_symbol_lookup` (`_graph_collection`, `_graph_path` for project-relative paths). Snapshot collections are named `sym_snapshot_<storage_id(project, reference, '')>`.
- 2026-11-22: Phase 3 T13–T15 proven at 825296a (every task check, plus 122 tests across the agent-tool, impact, navigation and code-tools suites, passes in a clean worktree of that commit). The committed `test_agent_tools.py` holds HEAD's tool list plus `code_impact`. The working copy also lists the uncommitted contract and frontend tools, which stay unstaged.
- 2026-11-22: Phase 4 expanded from the code at 825296a. `check_impact` (@api/Python-Service/app/code_quality/rule_assist.py) looks up rule links by canonical `symbol_id`, but impacted rows carry the revision-scoped graph id, hence T16. The impact lookup is injected into `check_impact` as a parameter, so @api/Python-Service/app/code_quality/http.py, which has other uncommitted work, needs no change.
- 2026-11-22: Phase 4 T16–T18 proven at 7684441 (every task check, plus 93 tests across the impact, rule-assist, impact-route and navigation suites, passes in a clean worktree of that commit).
- 2026-11-22: live run of T19 on an isolated stack (separate compose project `impact-eval`, a clean checkout of 4b22348, fresh Neo4j/Qdrant/MinIO and registry; the shared stack and pizza-builder repository untouched). Two revisions were uploaded from a scratch clone of pizza-builder: `rev_c939f2cf…` at 1670d9e, and `rev_fba59829…` at a scratch commit changing one line in `ChangeUserRoleAction::execute`. Revision 1 indexed 1,049 chunks and 714 symbol nodes, with all 1,533 references linked.
  - First run: HTTP 500. `_static_routes` imported `projects.backend_routes` outside its `try`, and that module is not committed (it exists only in the working tree), so every request failed on committed code. Fixed by moving the import into the `try`. Test `test_service_resolves_without_static_route_support` failed before the fix and passes after it. Committed at d5d86f2; 94 impact tests pass in a clean worktree of that commit.
  - Second run, with the fix: timing pass (1.1 s, 59 impacted, matching the 59 callers the LSP plan counted); citations pass (59/59 lines exist); comparison pass (exactly `App\Actions\Admin\ChangeUserRoleAction::execute`, modified); routes fail (0 routes).
  - Routes cause: with the working-tree `projects/backend_routes.py` copied into the stack, it read 93 Laravel routes, but the actions name import aliases with the default namespace in front, for example `App\Http\Controllers\AdminAuditLogController@index` for `use App\Http\Controllers\Api\Admin\V1\AuditLogController as AdminAuditLogController;` in pizza's `routes/api.php`. No such class exists, so no action matches an impacted symbol. The fix belongs in the route reader (resolve `use … as …` imports), which is uncommitted work outside this plan. Runtime routes were not available (no telemetry in the isolated stack).
  - Route reader fixed (uncommitted, in the working tree): `projects/backend_routes.py` `_laravel_routes` now reads routes from the Laravel plugin's PHP facts (`routes_from_facts.provide_routes`, real parser, so `use … as …` imports resolve), and falls back to the regex reader only when facts cannot be exported. `Route::view` and `Route::redirect` stay skipped. New test `test_laravel_routes_resolve_aliased_controller_imports` failed before the change and passes after it. The existing `withRouting` test now uses a valid Laravel 11 `bootstrap/app.php`; its old one-line fixture was not valid PHP, which only the regex reader accepted. Full suite: the same 20 failures as before.
  - Third run, with the route reader fix copied into the isolated stack: all four checks pass and the script exits 0. timing 2.1 s (59 impacted, 63 routes); citations 59/59; routes 63 (for example `GET /api/v1/admin/audit-logs/{auditLog}`); comparison exactly `ChangeUserRoleAction::execute`.
  - T19 proven at 12cc8e5, which commits the route reader alone: an isolated stack built from a clean checkout of that commit, with nothing copied in, indexed both pizza revisions (`rev_252fdd6a…` at 1670d9e, `rev_14f865e5…` one method later). The script passed timing (1.2 s, 59 impacted, 63 routes), citations (59/59), routes (63) and comparison (exactly `ChangeUserRoleAction::execute`), and exited 0. At d5d86f2 the same check failed on routes, so 12cc8e5 is the first passing commit. The route reader's tests import the untracked `api_contracts/` package and stay with the front-end work.
  - Before 12cc8e5: and `projects/backend_routes.py`, its test and the `api_contracts/` package it imports are untracked front-end assistance work (@.archcore/frontend-assist/frontend-developer-assistance.plan.md). Rerun the check at the commit that adds them, then record the proof here.
  - Diff and revision-pair inputs need commit-identified revisions: `load_revision` rejects uploads without a `commit_sha`. [assumption: acceptable for now; uploads without a commit can still use symbol input]
- 2026-11-22: T19 script committed at 4b22348; not yet proven, because its check needs the live pizza revisions: @api/Python-Service/scripts/impact_eval.py, standard library only. It runs inside `python_api`, because the container publishes no port, and calls `POST /code-tools/impact` and `/code-tools/read`. The routes check builds a one-line diff inside the hub symbol's method instead of needing a diff file. The comparison check is skipped without `--base-revision`. Against a stub API on localhost it printed `pass` for all four checks and exited 0. With a missing cited line, no routes and three changed methods it failed those three and exited 1. A 409 revision failed the timing check and skipped the rest. Its real check waits on the indexed pizza revisions under Waiting on.
- 2026-11-22: published to GitHub as one commit, e61f1d6, on `feature/change-impact-analysis`, based on `release.1.0` like PRs #1–#5: https://github.com/aminmh/developer-intelligence-assistant/pull/6 (open, mergeable, no CI checks configured). It carries the api impact commits f428224..d5d86f2, the web proxy 4635adb (merged into release.1.0's `server.ts` with `authIsGlobalAdmin`), the alias fix to the published `backend_routes.py` and its test, and a README section. On that tree the Python suite had the same 34 failures as `release.1.0` without the change; the web proxy tests (4) and `tsc --noEmit` passed. Locally, 853e6bd also committed the uncommitted front-end `contract_service.on_revision_ready` hook in `indexing_service.py`; `release.1.0` already has it, and locally it resolves when the front-end work is committed.
- 2026-11-22: the web proxy is done: `POST /api/code/impact` (@web/src/server/codeImpactRoutes.ts, wired in @web/server.ts) checks that the caller can read the project, forwards only the documented fields, caps the body at 210 KB and passes 422 and 409 through. Commit 4635adb in the `web` repository on `feature/agent-code-tools`, not pushed; the commit contains 4 files with 164 lines added.
- 2026-11-22: Phase 5 expanded. Its one task, the evaluation script, can be written now, but its check needs the indexed pizza revisions listed under Waiting on; the script goes in `scripts/` next to `agent_eval.py` and calls the running API.
- 2026-11-22: Phase 4 detail: Changed and impacted rows carry `canonical_symbol_id` next to the graph `symbol_id`. `check_impact` takes an `impact_lookup` (default `_graph_impact`: `analyze_diff` at depth 2 on the revision's snapshot graph). It looks up links for touched plus impacted symbols, ranks rules by direct link, then confirmed, then id, within `MAX_IMPACT_RULES`, and sends the model the hunks of the changed symbol each indirect path ends at. Prompt links and reviews mark indirect links with their `via` path. When the graph cannot be read, only rules of touched symbols are reviewed and the result says so in `impact_unavailable`. Checks: 5 new tests, which failed before the change and pass after it. Full suite: the same 20 failures as before.
- 2026-11-22: Phase 3 detail: Service: @api/Python-Service/app/inspection/impact_service.py (`run_impact`, `ImpactRequestError` → 422, `ImpactUnavailable` → 409). It resolves `latest` or a revision id, takes the snapshot collection from `runtime_reference`, uses `load_revision` for diffs and revision pairs and `routes_for_revision` for static routes, and makes paths project-relative (a path outside the revision becomes empty). Each route gets the handler's `path:line`. The tool `code_impact` (`impact_report` returns the route's report; the handler adds cited `items`) is in @api/Python-Service/app/agent_tools/navigation.py; the route `POST /code-tools/impact` is in @api/Python-Service/app/routers/code_tools.py. Checks: 9 new tests (6 in @api/Python-Service/app/tests/test_impact_route.py, 2 in test_navigation_tools.py, plus the tool-name list), which failed before the change and pass after it. Two existing tests that list every builtin tool by name gained `code_impact`. Full suite: the same 20 failures as before Phase 3.
- 2026-11-22: `code_impact` stays out of assessment review, which keeps its own tool list (@api/Python-Service/app/code_quality/category_review.py `TOOLS`).
- 2026-11-22: the route is not reachable from outside the API yet. @web/server.ts proxies only `/code-tools/read`, after authorizing the project, so CI or a UI needs a matching authorized proxy route in the web repository. [assumption: out of this plan's scope; listed as a follow-up]
- 2026-11-22 (Phase 2 detail): Engine: `analyze_diff`, `analyze_revisions`, `is_test_path`, routes, runtime marks and producer reasons in @api/Python-Service/app/inspection/impact.py. Queries: `inherited_callers`, `resolve_by_symbol_ids`, `routes_for`, `runtime_observed`, `producer_coverage`. Producer status: `lsp.jobs.record_producer`, called by `enrich_export` and `_with_scip`, then passed as `symbol_producers` to the graph write, which stores one `SymbolCoverage {collection}` node holding JSON. Checks: 24 impact tests, which failed before the change and pass after it. On a throwaway Neo4j 5.26 the inherited-caller query found callers through a two-level non-overriding subclass and through the class itself; it left out an overriding subclass, a trait user and another member name. The route query dropped a route from another revision. Full unit suite: 20 failures, the same 20 as on a copy without the Phase 2 diff. `test_lsp_jobs::test_unavailable_everywhere_keeps_parser_export` was updated, because the export now records LSP `unavailable` instead of being returned unchanged.
- 2026-11-22: producer status is recorded only on the upload and snapshot indexing path (`IndexingService`). The orchestrator, embedding rebuild, Git/CI source snapshots and saved analyses still call `enrich_export` without storing coverage, so their revisions report no producer reasons, not partial ones. [assumption: acceptable until those paths write the graph through `_index_symbol_graph`]
- 2026-11-22: revision pairs match symbols by canonical `symbol_id`, because snapshot node ids are scoped per revision (`storage_id`). Removed symbols are walked in the base graph, and their dependents carry the base `revision_id`.
- 2026-11-22: the test command is `python -m pytest` from `/app`, because plain `pytest` cannot import the app packages in the container (existing suites fail collection the same way). The `done when` lines were corrected.
- 2026-11-22: a graph read failure partway through the walk returns `coverage: unavailable` with no impacted rows, rather than a partial list. Graph errors and limits therefore stay distinguishable. A changed method whose parser references point at its class node with `to_member` is not walked from that class node yet. [assumption: LSP and SCIP edges target the method node; revisit in Phase 2 with clause 9]

## Declared Delta

- create spec change-impact-analysis
- amend spec source-code-navigation-tools — `code_impact` named in the tools covered
- amend spec business-rule-knowledge — clause 13 reviews rules of changed or impacted symbols
- create plan implement-change-impact-analysis
- none spec contract-conformance-findings — contract impact over UI maps stays separate
- none adr language-servers-feed-the-symbol-graph-at-index-time — no new producer
