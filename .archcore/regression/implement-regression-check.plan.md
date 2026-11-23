---
title: "Implement the Regression Check for Refactors and Upgrades"
status: draft
tags:
  - "api"
  - "code-quality"
  - "frontend"
---

## Goal

Build the regression check per @.archcore/regression/regression-check.spec.md: compare a base and a candidate revision with static evidence, HTTP replay against developer-run targets, and generated characterization tests, shown in a web view and the `regression_check` agent tool, language-neutral through framework capabilities.

Baseline (2026-11-23): the impact engine is done (@api/Python-Service/app/inspection/impact.py `analyze_revisions`, @api/Python-Service/app/inspection/impact_service.py `run_impact`; every task of @.archcore/agent-tools/implement-change-impact-analysis.plan.md proven). Contract diffs exist (@api/Python-Service/app/api_contracts/breaking.py `classify`). Benchmark egress, auth and run limits exist (@api/Python-Service/app/code_quality/performance.py). Capabilities `ROUTE_ANALYSIS`, `TEST_CONVENTIONS` exist; `PROBE_HINTS`, `CHARACTERIZATION_TESTS`, `RUN_HINTS` do not. No `app/regression/` package exists.

## Tasks

### Phase 1: Static check

1. [x] T1 Add report model and SQLite store for checks, verdicts and evidence rows

   - files: @api/Python-Service/app/regression/model.py @api/Python-Service/app/regression/store.py @api/Python-Service/app/tests/test_regression_check.py
   - covers: none
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_check.py -k store` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_check.py -k store` @ d8380bc

2. [x] T2 Resolve and authorize the revision pair, refusing unready or foreign revisions

   - files: @api/Python-Service/app/regression/service.py @api/Python-Service/app/tests/test_regression_check.py
   - covers: regression-check#1, regression-check#F1
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_check.py -k rejects_unready_or_foreign` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_check.py -k rejects_unready_or_foreign` @ d8380bc

3. [x] T3 Fill symbols, routes and contract changes from `analyze_revisions` and `breaking.classify`

   - files: @api/Python-Service/app/regression/service.py @api/Python-Service/app/tests/test_regression_check.py
   - covers: regression-check#2-3, regression-check#F2
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_check.py -k static_evidence` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_check.py -k static_evidence` @ d8380bc

4. [x] T4 Add the budgeted equivalence reviewer returning cited, labelled verdicts per changed symbol

   - files: @api/Python-Service/app/regression/equivalence.py @api/Python-Service/app/tests/test_regression_equivalence.py
   - covers: regression-check#4-5, regression-check#F7
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_equivalence.py` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_equivalence.py` @ d8380bc

5. [x] T5 Add `RUN_HINTS` capability, coverage map, tiers, partial list and baseline checklist

   - files: @api/Python-Service/app/agent/core/capability.py @api/Python-Service/app/regression/service.py @api/Python-Service/app/agent/plugins/frameworks/laravel/plugin.py @api/Python-Service/app/tests/test_regression_check.py
   - covers: regression-check#6-9
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_check.py -k tiers_uncovered_and_partial` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_check.py -k tiers_uncovered_and_partial` @ d8380bc

6. [x] T6 Add regression-check HTTP routes and the read-only `regression_check` agent tool with citations

   - files: @api/Python-Service/app/regression/http.py @api/Python-Service/app/agent_tools/navigation.py @api/Python-Service/app/agent_tools/types.py @api/Python-Service/app/tests/test_navigation_tools.py
   - covers: regression-check#22
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_navigation_tools.py -k regression_check` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_navigation_tools.py -k regression_check` @ d8380bc

7. [ ] T7 Add the web proxy route and a Regression check view showing the static report

   - files: @web/src/server/regressionRoutes.ts @web/src/components/regression/RegressionCheck.vue
   - covers: none
   - done when: the view lists symbols, routes, uncovered and partial for a pizza revision pair in the browser pane

### Phase 2: Replay

Expanded 2026-11-23 at the user's request while T7 still waits on its live check. Replay targets are saved benchmark targets (`PerformanceStore`), so origin validation, DNS pinning, Bearer auth and project scoping are reused; `http_login` targets are refused for replay in this phase. Requests use the standard library with the pinned address, no redirects followed.

8. [x] T8 Add `PROBE_HINTS` with `ParamHintRecord` and a Laravel provider listing seeders, factories and form requests

   - files: @api/Python-Service/app/agent/core/capability.py @api/Python-Service/app/agent/core/records.py @api/Python-Service/app/agent/interfaces/capabilities.py @api/Python-Service/app/agent/plugins/frameworks/laravel/probe_hints.py @api/Python-Service/app/tests/test_regression_probes.py
   - covers: regression-check#10
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_probes.py -k probe_hints` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_probes.py -k probe_hints` @ c13f342

9. [x] T9 Build probes from report routes with model-proposed values from hint files, and store edited sets

   - files: @api/Python-Service/app/regression/probes.py @api/Python-Service/app/regression/store.py @api/Python-Service/app/tests/test_regression_probes.py
   - covers: regression-check#10, regression-check#12
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_probes.py -k probes_from_capabilities` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_probes.py -k probes_from_capabilities` @ c13f342

10. [x] T10 Import HAR files and request lists, keeping only the target origin and dropping credentials

   - files: @api/Python-Service/app/regression/probes.py @api/Python-Service/app/tests/test_regression_probes.py
   - covers: regression-check#11
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_probes.py -k har` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_probes.py -k har` @ c13f342

11. [x] T11 Compare responses: status, selected headers, structural JSON paths, normalized HTML, noise and ignore rules

   - files: @api/Python-Service/app/regression/diff.py @api/Python-Service/app/tests/test_regression_diff.py
   - covers: regression-check#13, regression-check#15-16
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_diff.py` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_diff.py` @ c13f342

12. [x] T12 Store recordings on disk per check with size caps, hash-only large bodies and retention

   - files: @api/Python-Service/app/regression/recordings.py @api/Python-Service/app/tests/test_regression_replay.py
   - covers: regression-check#F6
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_replay.py -k recordings` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_replay.py -k recordings` @ c13f342

13. [x] T13 Replay probes in order through a pinned client, recording the baseline twice, gating writes

   - files: @api/Python-Service/app/regression/replay.py @api/Python-Service/app/tests/test_regression_replay.py
   - covers: regression-check#13-14, regression-check#F3-F5
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_replay.py -k order_egress_and_write_gating` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_replay.py -k order_egress_and_write_gating` @ c13f342

14. [x] T14 Merge replay results into route verdicts, coverage and tier, with span deltas per route

   - files: @api/Python-Service/app/regression/replay.py @api/Python-Service/app/regression/coverage.py @api/Python-Service/app/tests/test_regression_replay.py
   - covers: regression-check#17-18
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_replay.py -k "verdicts or span_deltas"` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_replay.py -k "verdicts or span_deltas"` @ c13f342

15. [x] T15 Add routes for probes, import, generation and replay runs, and replay rows in the agent tool

   - files: @api/Python-Service/app/regression/http.py @api/Python-Service/app/agent_tools/regression.py @api/Python-Service/app/tests/test_regression_replay.py
   - covers: none
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_replay.py -k routes` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_replay.py -k routes` @ c13f342

16. [x] T16 Add web proxy routes, with Bearer tokens from the credential helper, and the replay UI

   - files: @web/src/server/regressionRoutes.ts @web/src/server/codeQualityRoutes.ts @web/src/components/regression/ReplayPanel.vue @web/tests/regressionRoutes.test.ts
   - covers: none
   - done when: `npx tsx --test tests/regressionRoutes.test.ts` passes and `vite build` succeeds
   - proof: `npx tsx --test tests/regressionRoutes.test.ts` and `vite build` @ 20b77e6 (web)

### Phase 3: Characterization tests

Expanded 2026-11-23. Route tests are rendered deterministically from the newest baseline recording (no model): request, recorded status, and one assertion per JSON leaf, leaving out noise, ignore rules, CSRF-like keys and values compared by shape (timestamps, UUIDs). Plugins supply text templates through `TestTemplateRecord`; the core fills placeholders (`{method}`, `{path}`, `{status}`, `{dot_path}`, `{bracket_path}`, `{value}`) and never names a language. Symbol tests for uncovered changed symbols without a route are written by the model and labelled review-only, as `rule_assist.generate_tests` does for rules. Nothing is run or written to the repository.

17. [x] T17 Add `CHARACTERIZATION_TESTS` with `TestTemplateRecord`, and Laravel (PHPUnit, Pest) and Symfony providers

   - files: @api/Python-Service/app/agent/core/capability.py @api/Python-Service/app/agent/core/records.py @api/Python-Service/app/agent/interfaces/capabilities.py @api/Python-Service/app/agent/plugins/frameworks/laravel/test_templates.py @api/Python-Service/app/agent/plugins/frameworks/symfony/test_templates.py @api/Python-Service/app/tests/test_regression_characterize.py
   - covers: regression-check#19
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_characterize.py -k templates` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_characterize.py -k templates` @ 38cb25f

18. [x] T18 Render route tests from baseline recordings, asserting recorded status and JSON fields minus noise

   - files: @api/Python-Service/app/regression/characterize.py @api/Python-Service/app/tests/test_regression_characterize.py
   - covers: regression-check#19-20
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_characterize.py -k route_tests` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_characterize.py -k route_tests` @ 38cb25f

19. [x] T19 Write review-only symbol tests with the model for uncovered changed symbols without routes

   - files: @api/Python-Service/app/regression/characterize.py @api/Python-Service/app/tests/test_regression_characterize.py
   - covers: regression-check#19
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_characterize.py -k symbol_tests` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_characterize.py -k symbol_tests` @ 38cb25f

20. [x] T20 Emit a runner-neutral request file, importable by replay, when no template is provided

   - files: @api/Python-Service/app/regression/characterize.py @api/Python-Service/app/tests/test_regression_characterize.py
   - covers: regression-check#21
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_characterize.py -k tests_from_capability_or_http_file` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_characterize.py -k tests_from_capability_or_http_file` @ 38cb25f

21. [x] T21 Serve the test bundle as a ZIP, list it in the agent tool, and add a download in the UI

   - files: @api/Python-Service/app/regression/http.py @api/Python-Service/app/agent_tools/regression.py @web/src/server/regressionRoutes.ts @web/src/components/regression/RegressionCheck.vue @web/tests/regressionRoutes.test.ts
   - covers: none
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_characterize.py -k bundle` and `npx tsx --test tests/regressionRoutes.test.ts` pass
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_characterize.py -k bundle` @ 38cb25f (api) and `npx tsx --test tests/regressionRoutes.test.ts` @ 1511f88 (web)

### Phase 4: Telemetry body capture (needs its own privacy decision first)

Opt-in, sampled, redacted request and response attributes as a language-neutral span contract, first in indexr-otel's PHP adapter and the collector keep-list, feeding probe sets.

## Acceptance Criteria

- On two pizza-builder revisions with a behavior-preserving refactor, no route or symbol is `suspected_regression`.
- On two pizza-builder revisions with a deliberately breaking change, the changed route and symbol are `suspected_regression` (Phase 2) or flagged by equivalence review (Phase 1).
- A project never run, with no spans, gets tier `static` and a baseline checklist.
- A non-PHP fixture project gets a report whose missing capabilities appear in `partial`; no core module names a language.
- `docker compose run python-api python -m pytest` shows no new failures against the known unrelated ones.

## Dependencies

- @.archcore/regression/regression-check.spec.md — the contract this plan builds.
- @.archcore/agent-tools/change-impact-analysis.spec.md — revision-pair impact, done.
- Hazard: equivalence review on many changed symbols exceeds model budget → per-check budget and `not_covered` with reason → T4 test.
- Hazard: commit-less uploads cannot be compared (`load_revision` needs `commit_sha`) → report the refusal reason → T2 test.
- Hazard: plugins without `RUN_HINTS` give an empty checklist → fall back to `docker-compose.yml`, `.env.example`, `Dockerfile`, manifests → T5 test.

## Clarifications

- 2026-11-23: plan created from the approved session plan; decisions recorded in the four ADRs under @.archcore/regression/.
- 2026-11-23: published to GitHub as one commit, f1ffbfc, on `feature/regression-check`, based on `release.1.0` like PR #6: https://github.com/aminmh/developer-intelligence-assistant/pull/7 (open, mergeable, no CI checks configured). It carries the api commits d8380bc..b77a790 and the web commits 575f9cc..1511f88 under `api/` and `web/`; `web/server.ts` and `web/src/components/CodeQualityView.vue` differ on `release.1.0`, so this plan's lines were applied to release's versions by hand. `.archcore/` is not in that repository and was not published. On the PR tree the full Python suite had 1996 passed, 17 failed, 1 collection error (`tests/test_runtime_storage.py`); `release.1.0` alone had 1939 passed with the same 17 failures and the same error. Web: 20 route tests, `tsc --noEmit` and `vite build` passed. Comparison posted as the PR's first comment.
- 2026-11-23: characterization tests run on a real Laravel app. Setup: two scratch clones of pizza-builder at 1670d9e (Laravel 12, Pest 3) on SQLite with `migrate --seed`; the candidate copy shares the base's database file and multiplies `PizzaSizeResource` `sort_order` by 10; both served with `php artisan serve` on 8201/8202; the Python API container alone; a seeded check with three real routes (`/api/v1/pizza-sizes`, `/pizza-sizes/{pizzaSize}`, `/pizzas`) and probes including `/api/v1/settings`. Baseline, candidate and the bundle went through the HTTP routes; templates came from the real Laravel provider on pizza-builder's `composer.json` (Pest), because the seeded revisions have no archive.
  - First run, as generated at 38cb25f: all 4 tests failed on the unchanged base. Causes: (1) `tests/Pest.php` applies `RefreshDatabase` to `tests/Feature`, wiping the recorded data; reseeding cannot replace it, because the seeders mint new ULIDs and timestamps; (2) Laravel pagination links carry the requesting host, so replay also flagged `$.links.first` between the two targets and the tests pinned `http://host.docker.internal:8201/...`; (3) the README blamed a missing template when the base source could not be read.
  - Fixed at b77a790: Laravel files go to `tests/Characterization`, Pest files declare `uses(Tests\\TestCase::class)`, and the run command and README point the run at a copy of the recorded database (`DB_DATABASE=...`); Symfony gets the same note with `DATABASE_URL`. URLs on a target's own origin, including the localhost aliases of host.docker.internal, compare by path in replay (`diff.origin_aliases`, `relative`) and are left out of tests with an "own-origin URL" note. The README carries the bundle notes and the template's `setup_note`.
  - Second run: replay flagged only `sort_order` on the three size routes; the generated Pest file passed on the base version (4 tests, 122 assertions) and failed on the candidate exactly on the three changed routes (`Failed asserting that 10 is identical to 1`), with settings still passing.
  - Open: large responses assert only the first 40 fields (`/api/v1/pizzas` left 508 unasserted); the developer must supply the recorded database, which is a manual step. [assumption: acceptable; revisit if users ask for snapshotting]
- 2026-11-23: Phase 3 committed as 38cb25f (api, 15 files) and 1511f88 (web, 3 files) on `feature/agent-code-tools`, not pushed; other people's uncommitted test changes stayed out. Each commit's tree, exported alone, passed 239 Python tests with 12 regression routes registered, and 19 web tests, `tsc --noEmit` and `vite build`. Generated files for all three templates (Laravel PHPUnit, Laravel Pest, Symfony WebTestCase) pass `php -l` inside the API image. The download button was checked in the browser pane with stubbed responses (error shown, then a ZIP download).
  - Found while writing the tests: JSON keys containing dots shared paths with nested keys (`{"a.b": 1}` vs `{"a": {"b": 1}}`), in diffs as well as tests. Paths now write such keys as `["a.b"]` (@api/Python-Service/app/regression/diff.py `key_part`); test `test_noise_structural_diff_keys_with_dots_stay_unambiguous`.
  - Templates come from the base revision (the tests describe the base version). Route tests skip routes an existing test covers; symbol tests are written only for modified symbols no tested or replayed route depends on, at most 5 per bundle. The README states how to run them (base first, then the change, same seeded data).
  - Verified on pizza-builder the same day (see the next entry); Symfony templates are still checked by `php -l` only.
- 2026-11-23: live replay test (Phase 2 at c13f342 plus fixes). Setup: a two-version Node sample API in the session scratchpad on host ports 8101 (v1) and 8102 (v2: order totals add a delivery fee, orders gain `currency`, `/api/legacy` returns 404; noise from request ids, uptime, timestamps, CSRF tokens); the real Python API container alone (`--no-deps`, throwaway `TELEMETRY_SNAPSHOT_ROOT`); the web dev server against it in development auth. Seeded: two ready revisions of project `shop` and a done static check with three routes the model judged unchanged (static evidence needs Neo4j and an index, so it was not live). Driven through the Regression tab: targets saved through the web API (localhost rewritten to host.docker.internal), probes added by hand, baseline and candidate runs.
  - Worked first time: egress from the container to host.docker.internal, origin rewriting, write gating (POST skipped until disposable), noise detection (`$.request_id`, `$.uptime_ms`; timestamps by shape; HTML CSRF tokens stripped), structural diffs (`$.data.total` 12 → 14, `$.data.currency` added, `/api/legacy` 200 → 404), disposable writes with `POST /__reset` before each pass, replay overriding the model's `unchanged`, HAR import (other origin skipped, Cookie and Authorization dropped), refusals for port 9002, private IPs, an unreachable target and an `http_login` target.
  - Found and fixed: (1) concrete probe paths such as `/api/orders/1` were listed as other requests instead of counting toward `GET /api/orders/{id}` (fixed at 758248c, test `test_verdicts_match_hand_typed_paths_to_route_templates`); (2) the UI left the report stale when a run finished before its first poll (fixed at af50e4e in web); (3) unfilled routes said "no value proposed" when the model was unreachable (fixed at 758248c). After the fixes all three routes read `suspected_regression` with their differences, and `/api/orders/2?expand=items` from the HAR import counted toward `/api/orders/{id}`.
  - Not covered live: model-proposed probes (no model reachable from the container), Bearer targets through the OS credential helper, span deltas (no ClickHouse), the static check on a real index.
- 2026-11-23: Phase 2 committed as c13f342 (api, 17 files) and 20b77e6 (web, 6 files) on `feature/agent-code-tools`, not pushed. `agent/migration/adapters.py` and other uncommitted work stayed out; `server.ts` was staged as HEAD plus this phase's lines only. Each commit's tree, exported alone, passed 228 Python tests (all regression suites, navigation, agent tools, capabilities, evidence categories, impact, benchmark), registered 11 regression routes, and passed 18 web tests, `tsc --noEmit` and `vite build`. The replay panel was checked in the browser pane with stubbed API responses only (targets, probe edit and save, differences, other requests).
  - Code: @api/Python-Service/app/regression/ `probes.py`, `diff.py`, `recordings.py`, `replay.py`, `replay_service.py`; `Capability.PROBE_HINTS` and `ParamHintRecord`; Laravel @api/Python-Service/app/agent/plugins/frameworks/laravel/probe_hints.py; web @web/src/components/regression/ReplayPanel.vue and `createCredentialHelper` in @web/src/server/codeQualityRoutes.ts.
  - Scope limits: replay supports public and Bearer targets; `http_login` targets are refused with a message. Recordings are files under the snapshot root (`regression_recordings/`), not MinIO, and are not in the data-management inventory yet, because that package is uncommitted work outside this plan. Both are noted in @.archcore/regression/replay-recordings-stay-out-of-telemetry.adr.md. [assumption: acceptable until data management is committed]
  - A route's static verdict is kept in `static_verdict`; a replay with differences makes it `suspected_regression`, a matching replay makes `unchanged` only a static `unchanged` or `not_covered` (a model judgment of change stays for a person to review). Each route's test and runtime coverage is kept in `static_coverage`, so a replay merge never drops it.
  - Span deltas use the runs' UUID run ids on `X-DevAssistant-Benchmark`, like benchmarks, and need indexr-otel on both versions; without spans the tier is `replay`.
- 2026-11-23: committed as d8380bc (api, 18 files) and 575f9cc (web, 5 files) on `feature/agent-code-tools`, staging only this plan's lines out of `main.py`, `server.ts` and `CodeQualityView.vue`, which hold other uncommitted work. Each commit's tree, exported alone, passed: 184 Python tests (regression, navigation, agent tools, capabilities, evidence categories, impact) with 4 regression routes registered; 8 web proxy tests, `tsc --noEmit` and `vite build`. T1–T6 proven at d8380bc; T7 waits on its live check. Not pushed.
- 2026-11-23: Phase 1 T1–T7 implemented, uncommitted on `feature/agent-code-tools` (api) and `feature/agent-code-tools` (web); no task is checked because no commit carries a proof yet.
  - Code: @api/Python-Service/app/regression/ (`model`, `store`, `service`, `equivalence`, `coverage`, `http`), @api/Python-Service/app/agent_tools/regression.py (`regression_check`, workflows `chat` and `code_refactor`), `Capability.RUN_HINTS` and `RunHintRecord` (@api/Python-Service/app/agent/core/capability.py, records.py, interfaces/capabilities.py), Laravel provider @api/Python-Service/app/agent/plugins/frameworks/laravel/run_hints.py; router registered in @api/Python-Service/app/main.py; tool in `agent_tools/builtins.py`. Web: @web/src/server/regressionRoutes.ts (read: project readers; start and delete: operators), wired in @web/server.ts; @web/src/components/regression/RegressionCheck.vue as a Regression tab in @web/src/components/CodeQualityView.vue.
  - Checks: 22 tests in test_regression_check.py and test_regression_equivalence.py plus 2 in test_navigation_tools.py pass; test_agent_tools.py, test_framework_capabilities.py, test_evidence_categories.py, the impact suites and every laravel/capability/plugin test (188) still pass. `test_runtime_storage.py` fails at collection on unrelated uncommitted runtime work. Web: 4 proxy tests pass; `vite build` passes.
  - T7 checked only with stubbed API responses in the browser pane (dev auth): every report section renders, no console errors. Its real check waits on two indexed pizza revisions, as under the impact plan's Waiting on.
  - Routes live at `/projects/{id}/regression-checks` (not under `/code-quality`); the web path is `/api/regression/projects/{id}/checks`.
  - A static check runs in a daemon thread, one pending or running check per project (409 with `active_check_id`); checks left running by a restart are marked failed on first use.
  - Equivalence review sends source to the remote model only when the project's remote consent allows it; otherwise local. Added symbols are `changed_expected` without a model call; removed symbols are `suspected_regression` while still called.
  - A symbol row keeps its equivalence verdict; its entry in `uncovered` carries `not_covered` (clause 6 reads as: uncovered lists items with no test, span or replay evidence). [assumption: confirm with the user]
  - Route coverage by tests uses `TestRecord.references` (route paths via `testing_checks.reaches`, or the handler class) and test symbols the impact walk reached.

## Declared Delta

- create spec regression-check
- create adr developer-runs-targets-app-only-sends-http
- create adr replay-writes-only-on-disposable-targets
- create adr replay-recordings-stay-out-of-telemetry
- create adr regression-evidence-comes-from-framework-capabilities
- none spec change-impact-analysis — reused unchanged
