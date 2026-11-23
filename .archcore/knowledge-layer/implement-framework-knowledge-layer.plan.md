---
title: "Implement the Framework Knowledge Layer"
status: draft
tags:
  - "api"
  - "code-quality"
---

## Goal

Build the knowledge layer per @.archcore/knowledge-layer/framework-knowledge-layer.spec.md: cited, version-specific documentation for a project revision, from the installed package first, then the library's files at the installed tag, then GitMCP (@.archcore/knowledge-layer/gitmcp-is-the-remote-documentation-provider.adr.md). Exposed as the `framework_docs` agent tool, and later used by the regression check.

Baseline (2026-11-23):
- No `app/knowledge/` package exists.
- Only one language plugin exists: PHP (@api/Python-Service/app/agent/plugins/languages/php/plugin.py, capabilities `TEST_CONVENTIONS`, `CONFIG_RISKS`).
- Revision archives are reachable through `rule_code.load_revision` and `framework_evidence.archive_context`.
- Per-project switches follow @api/Python-Service/app/infra/remote_consent.py.
- Host allowlisting and public-address checks exist in @api/Python-Service/app/infra/tool_executor.py (`_allowed_hosts`, `_blocked_host`).
- No MCP client library is installed.

## Tasks

### Phase 1: Installed versions and the vendor provider (offline)

1. [x] T1 Add `INSTALLED_PACKAGES` with `PackageRecord`, and read `composer.lock` in the PHP language plugin

   - files: @api/Python-Service/app/agent/core/capability.py @api/Python-Service/app/agent/core/records.py @api/Python-Service/app/agent/interfaces/capabilities.py @api/Python-Service/app/agent/plugins/languages/php/plugin.py @api/Python-Service/app/agent/plugins/languages/php/packages.py @api/Python-Service/app/tests/test_knowledge_layer.py
   - covers: framework-knowledge-layer#1
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k installed_version` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k installed_version` @ cda5cbb

2. [x] T2 Add the knowledge service resolving the installed version, reporting unavailable without a record or lockfile

   - files: @api/Python-Service/app/knowledge/__init__.py @api/Python-Service/app/knowledge/model.py @api/Python-Service/app/knowledge/service.py @api/Python-Service/app/tests/test_knowledge_layer.py
   - covers: framework-knowledge-layer#1-2, framework-knowledge-layer#F5
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k installed_version_from_lockfile_records` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k installed_version_from_lockfile_records` @ cda5cbb

3. [x] T3 Read topic-matched, cited, capped excerpts from the installed package's UPGRADE, CHANGELOG and README files

   - files: @api/Python-Service/app/knowledge/providers/__init__.py @api/Python-Service/app/knowledge/providers/vendor.py @api/Python-Service/app/tests/test_knowledge_layer.py
   - covers: framework-knowledge-layer#3, framework-knowledge-layer#7, framework-knowledge-layer#F4
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k "excerpts_are_cited or vendor"` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k "excerpts_are_cited or vendor"` @ cda5cbb

4. [x] T4 Return upgrade notes for each version between installed and target from vendor upgrade files

   - files: @api/Python-Service/app/knowledge/service.py @api/Python-Service/app/knowledge/providers/vendor.py @api/Python-Service/app/tests/test_knowledge_layer.py
   - covers: framework-knowledge-layer#8
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k upgrade_notes_between_versions` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k upgrade_notes_between_versions` @ cda5cbb

5. [x] T5 Cache excerpts by library, version and topic with per-provider lifetimes

   - files: @api/Python-Service/app/knowledge/cache.py @api/Python-Service/app/tests/test_knowledge_layer.py
   - covers: framework-knowledge-layer#11
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k cache_lifetimes` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k cache_lifetimes` @ cda5cbb

6. [x] T6 Register the read-only `framework_docs` agent tool with cited items and an untrusted-data prompt helper

   - files: @api/Python-Service/app/agent_tools/knowledge.py @api/Python-Service/app/agent_tools/builtins.py @api/Python-Service/app/knowledge/prompt.py @api/Python-Service/app/tests/test_knowledge_layer.py @api/Python-Service/app/tests/test_agent_tools.py
   - covers: framework-knowledge-layer#12-13
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k "framework_docs or prompt_caps"` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k "framework_docs or prompt_caps"` @ cda5cbb

### Phase 2: Tag-pinned repository files, the project switch, and egress

Expanded 2026-11-23 after Phase 1 was proven at cda5cbb. `PackageRecord` already carries the source commit (`reference`) from `composer.lock`, so files are read at that exact commit first and tag forms are only a fallback.

7. [x] T7 Add the per-project `remote_docs_allowed` switch, on by default, with read and update routes

   - files: @api/Python-Service/app/knowledge/settings.py @api/Python-Service/app/routers/remote_consent.py @api/Python-Service/app/tests/test_knowledge_layer.py
   - covers: framework-knowledge-layer#9
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k remote_docs_switch` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k remote_docs_switch` @ d20fda6

8. [x] T8 Add an allowlisted HTTPS fetcher with public-address checks, timeouts and rate-limit detection

   - files: @api/Python-Service/app/knowledge/http.py @api/Python-Service/app/tests/test_knowledge_layer.py
   - covers: framework-knowledge-layer#10, framework-knowledge-layer#F1, framework-knowledge-layer#F3
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k "only_identifiers_leave_the_machine or remote_failures_are_partial"` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k "only_identifiers_leave_the_machine or remote_failures_are_partial"` @ d20fda6

9. [x] T9 Read UPGRADE, CHANGELOG and README at the lockfile commit, falling back to tag forms

   - files: @api/Python-Service/app/knowledge/providers/github_ref.py @api/Python-Service/app/tests/test_knowledge_layer.py
   - covers: framework-knowledge-layer#4, framework-knowledge-layer#F2
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k github_ref_reads_at_tag` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k github_ref_reads_at_tag` @ d20fda6

10. [x] T10 Call providers in order with partial coverage, and read upgrade notes at the target tag

   - files: @api/Python-Service/app/knowledge/service.py @api/Python-Service/app/tests/test_knowledge_layer.py
   - covers: framework-knowledge-layer#3, framework-knowledge-layer#8, framework-knowledge-layer#F1
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k "provider_order_and_consent or upgrade_notes_at_target_tag"` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k "provider_order_and_consent or upgrade_notes_at_target_tag"` @ d20fda6

### Phase 3: GitMCP provider

Expanded 2026-11-23 after Phase 2 was proven at d20fda6. GitMCP's tool names differ between sources (`search_generic_documentation`, `fetch_generic_documentation`, `search_generic_code` on `gitmcp.io/docs`, per its README), so the client discovers them with `tools/list` rather than hard-coding them. Topic words may go to GitMCP (clause 10 allows topic words); project code never does.

11. [x] T11 Add a minimal MCP client over streamable HTTP: initialize, tools/list, tools/call through the fetcher

   - files: @api/Python-Service/app/knowledge/mcp_client.py @api/Python-Service/app/knowledge/http.py @api/Python-Service/app/tests/test_knowledge_layer.py
   - covers: framework-knowledge-layer#10
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k mcp_client` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k mcp_client` @ 8466d25

12. [x] T12 Return GitMCP search results as unversioned excerpts naming the default branch

   - files: @api/Python-Service/app/knowledge/providers/gitmcp.py @api/Python-Service/app/tests/test_knowledge_layer.py
   - covers: framework-knowledge-layer#6-7
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k gitmcp_excerpts_unversioned` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k gitmcp_excerpts_unversioned` @ 8466d25

13. [x] T13 Call GitMCP last, only when earlier providers lack the topic, with a one-day cache

   - files: @api/Python-Service/app/knowledge/service.py @api/Python-Service/app/tests/test_knowledge_layer.py
   - covers: framework-knowledge-layer#5, framework-knowledge-layer#11, framework-knowledge-layer#F1
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k gitmcp_called_last` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k gitmcp_called_last` @ 8466d25

### Phase 4: Consumers in the regression check, and framework documentation sources

Expanded 2026-11-23 after Phase 3 was proven at 8466d25. These tasks use the layer; the spec's clauses are already covered, so they name the spec Purpose's dependents instead.

14. [x] T14 Add `DOC_SOURCES` so a plugin can name a separate docs repository, read at the target's branch

   - files: @api/Python-Service/app/agent/core/capability.py @api/Python-Service/app/agent/core/records.py @api/Python-Service/app/agent/interfaces/capabilities.py @api/Python-Service/app/agent/plugins/frameworks/laravel/doc_sources.py @api/Python-Service/app/agent/plugins/frameworks/laravel/plugin.py @api/Python-Service/app/agent/plugins/frameworks/symfony/doc_sources.py @api/Python-Service/app/agent/plugins/frameworks/symfony/plugin.py @api/Python-Service/app/knowledge/providers/doc_source.py @api/Python-Service/app/knowledge/cache.py @api/Python-Service/app/knowledge/service.py @api/Python-Service/app/tests/test_knowledge_layer.py
   - covers: framework-knowledge-layer#8
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k doc_sources` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_knowledge_layer.py -k doc_sources` @ 1b10db2

15. [x] T15 Add upgrade notes to a regression report when the candidate's lockfile changes package versions

   - files: @api/Python-Service/app/regression/service.py @api/Python-Service/app/regression/coverage.py @api/Python-Service/app/regression/model.py @api/Python-Service/app/agent_tools/regression.py @api/Python-Service/app/tests/test_regression_check.py @web/src/components/regression/RegressionCheck.vue
   - covers: none
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_check.py -k package_upgrades` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_check.py -k package_upgrades` @ 1b10db2

16. [x] T16 Give probe proposals and model-written symbol tests version-exact excerpts as untrusted context

   - files: @api/Python-Service/app/regression/probes.py @api/Python-Service/app/regression/characterize.py @api/Python-Service/app/regression/replay_service.py @api/Python-Service/app/tests/test_regression_probes.py @api/Python-Service/app/tests/test_regression_characterize.py
   - covers: framework-knowledge-layer#12
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_probes.py tests/test_regression_characterize.py -k framework_context` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_probes.py tests/test_regression_characterize.py -k framework_context` @ 1b10db2

17. [x] T17 Mark characterization templates partial when the installed framework is outside their verified range

   - files: @api/Python-Service/app/agent/core/records.py @api/Python-Service/app/agent/plugins/frameworks/laravel/test_templates.py @api/Python-Service/app/agent/plugins/frameworks/symfony/test_templates.py @api/Python-Service/app/regression/characterize.py @api/Python-Service/app/regression/replay_service.py @api/Python-Service/app/tests/test_regression_characterize.py
   - covers: none
   - done when: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_characterize.py -k verified_range` passes
   - proof: `docker compose run --rm --no-deps python-api python -m pytest tests/test_regression_characterize.py -k verified_range` @ 1b10db2

## Acceptance Criteria

- On a pizza-builder revision, `framework_docs` for `symfony/http-foundation` and a topic its CHANGELOG covers returns excerpts from the installed package, each citing the version from `composer.lock`, with no network access. (Amended 2026-11-23 with the user: pizza-builder's installed `laravel/framework` ships only a README with no upgrade text, so the original example could not hold; Laravel's guides come from `laravel/docs` through `doc_source`.)
- With a target version, the answer lists upgrade notes for each version between installed and target, or names the versions it could not find.
- A project that turns remote documentation off makes no remote calls; with it on, nothing but repository names, versions, file paths and topic words leaves the machine (Phase 2 test on recorded requests).
- GitMCP excerpts are always marked unversioned (Phase 3).
- `docker compose run python-api python -m pytest` shows no new failures against `release.1.0` (17 failures and 1 collection error at f1ffbfc, per @.archcore/regression/implement-regression-check.plan.md).

## Dependencies

- @.archcore/knowledge-layer/framework-knowledge-layer.spec.md: the contract this plan builds.
- @.archcore/knowledge-layer/gitmcp-is-the-remote-documentation-provider.adr.md: provider choice and order.
- @.archcore/code-quality/framework-knowledge-lives-in-plugins-over-language-facts.adr.md: lockfile reading belongs to language plugins (`INSTALLED_PACKAGES`), not the core.
- @.archcore/agent-tools/mcp-servers-reach-only-allowlisted-hosts-through-egress-proxy.adr.md: remote hosts are allowlisted (Phase 2).
- Hazard: uploads often exclude `vendor/` and `node_modules/` → the vendor provider finds nothing and coverage reads partial → test the missing-package case in T3; Phase 2 fills it from the tag.
- Hazard: tags do not always match versions (`v7.1.3`, `7.1.3`, monorepo tags) → try known tag forms and report the missing tag → Phase 2 failure clause 2.
- Hazard: unauthenticated GitHub rate limits → optional token in the OS credential store, stop remote calls on limit → Phase 2 failure clause 3.
- Hazard: no MCP client library → call GitMCP with plain JSON-RPC over HTTP (initialize, tools/list, tools/call) → Phase 3. [assumption: avoid a new dependency for one remote server]
- Hazard: upgrade files differ per project (Symfony `UPGRADE-x.y.md`, Laravel docs-only, npm CHANGELOG sections) → T4 parses `UPGRADE-*` files and `## [x.y.z]` changelog headings, and reports versions it could not find.

## Clarifications

- 2026-11-23 closeout: every task proof verified against git (commit on the branch, named files changed, check present at that commit). Full API suite on the committed tree 1b10db2: 20 failed, 1912 passed, 1 collection error (`test_runtime_storage.py`); on b77a790, the tree before this plan, in the same environment: 21 failed, 1873 passed, the same error. Every failure after is also a failure before; none is new. The recorded 17 at f1ffbfc came from another environment.

- 2026-11-23: Phase 4 T14-T17 proven at 1b10db2 on `feature/agent-code-tools` (api), with the report UI at 4768785 (web); neither pushed. The staged tree, exported alone, passed 358 tests across the knowledge, remote-consent, agent-tool, navigation, capability, evidence, regression, impact and Laravel-knowledge suites; the knowledge and remote-consent files passed in both orders. Every task is proven; the plan is ready for closeout.
  - Provider order became vendor, then `doc_source` (a framework's guides repository at the matching version line, named by the plugin through `DOC_SOURCES`), then `github_ref`, then GitMCP. `doc_source` is version-exact like `github_ref`, so the ADR's rule (exact sources before GitMCP) holds, so the ADR's ordering rule holds. With the user's confirmation, the spec (new clauses 14-15, `DOC_SOURCES` in its surface, a 7-day guide cache) and the ADR (four sources in order) were amended the same day.
  - Behavior change in `lookup`: a target no longer forces remote calls when the installed copy's notes already reach the target's minor line. The regression check needs this (the candidate's own copy holds the new notes); `test_remote_failures_are_partial` moved its target from 8.0 to 8.1 to keep testing a failed remote call.
  - Laravel guides: `laravel/docs` at `{major}.x`; Symfony guides: `symfony/symfony-docs` at `{major}.{minor}` (topics only; Symfony upgrade notes stay in each component's CHANGELOG). Every URL was checked live before use.
  - Live on pizza-builder's real lockfile (12.69.1): 13.0 upgrade notes from `laravel/docs@13.x` in 1.4 s with one request; "route model binding" from `12.x/routing.md` in 1.7 s; probe context 8,000 chars from routing and validation guides in 5.2 s; a candidate lockfile bumped to 13.0.0 gave `package_changes` with 11 cited excerpts in 0.7 s.
  - Found live and fixed before the commit: (1) in document order the report's 6,000-character notes budget ran out on the guide's index and introduction; link-only index sections are dropped and changes are ranked by the guide's own "Likelihood Of Impact", high first; (2) the Laravel package README's marketing sections crowded out the testing guide in prompt context; guides now come first.
  - Verified ranges are recorded only where a real run passed: Laravel Pest on `laravel/framework` 12.x (pizza-builder, 2026-11-23). The Laravel PHPUnit and Symfony templates were never run, so every bundle from them is marked partial until a live run widens them.
  - Not checked in the browser: the report's new Package changes section (type-check and production build passed).

- 2026-11-23: plan created after the user accepted the spec; Phase 1 needs no network so it can be built and proven first.
- 2026-11-23: Phase 3 T11-T13 proven at 8466d25 on `feature/agent-code-tools` (api), not pushed. The staged tree, exported alone, passed 232 tests across the knowledge, remote-consent, agent-tool, navigation, capability, evidence, regression and impact suites, and the knowledge and remote-consent files passed in both orders. 27 tests in @api/Python-Service/app/tests/test_knowledge_layer.py.
  - Probed the real gitmcp.io first: streamable HTTP with server-sent-event replies and an `mcp-session-id` header; notifications answer 202; tools `search_generic_documentation`, `fetch_generic_documentation`, `search_generic_code`, `match_common_libs_owner_repo_mapping` and `fetch_generic_url_content`. The client allows only the two documentation tools: the URL fetcher would fetch arbitrary hosts for us, and GitMCP's own results tell the client to use it.
  - Search depends on GitMCP's own index: `fastapi/fastapi` matched; `symfony/symfony`, `laravel/docs` and `idosal/git-mcp` answered "No relevant documentation found ... either being indexed" with the README as a fallback, which the provider treats as no match. The comparison's "no indexing step" holds only for its documentation fetch, not its search; corrected in @.archcore/knowledge-layer/remote-documentation-provider.rnd.md.
  - Found live and fixed before the commit: (1) GitMCP keeps the event stream open after replying, so reading to the end hit the 10 s timeout; event-stream replies are read up to their first complete event (`knowledge.http._first_event`, test `test_mcp_client_reads_only_the_first_event_of_an_open_stream`); (2) Cloudflare in front of gitmcp.io answers urllib's default User-Agent with 403; the fetcher always sends `DevAssistant-Knowledge/1` (asserted in `test_mcp_client_session_tools_and_allowlist`); (3) FastAPI's page came back in three languages; translations of a page already returned are dropped.
  - Live result after the fixes: `fastapi/fastapi` "dependency injection" returned 5 unversioned excerpts from `master` in 4.4 s; `symfony/symfony` returned a no-match reason; the URL fetcher was refused before any request; all requests went to `gitmcp.io/docs` only.
  - The Phase 2 commit d20fda6 shipped a test that set the internal secret on the settings object in a way monkeypatch could not undo, so `test_remote_consent.py` failed when run after the knowledge tests. The Phase 2 proof ran those files separately and missed it. Fixed at 8466d25; Phase 2's product code was not affected.
- 2026-11-23: Phase 2 T7-T10 proven at d20fda6 on `feature/agent-code-tools` (api), not pushed. The staged tree, exported alone, passed 203 tests across the knowledge, agent-tool, navigation, capability, evidence, regression and impact suites, plus the 23 existing `test_remote_consent.py` tests for the router the switch was added to (not a new `routers/knowledge.py`, which would also have touched `main.py`, which holds other people's uncommitted work). 20 tests in @api/Python-Service/app/tests/test_knowledge_layer.py.
  - The Phase 1 vendor section logic now works on any set of documents (`topic_from_documents`, `upgrade_from_documents`, `Citation`), so `github_ref` reuses it.
  - GitHub cost per remote lookup: one contents-API call (rate-limited, 60 per hour without a token) plus raw files, which do not count against the API limit.
  - Live against GitHub with pizza-builder's lockfile: `symfony/http-foundation` 7.4.18 to target 8.0 read `v8.0.0`'s CHANGELOG and returned the real 8.0 section (3.5 s); `guzzlehttp/guzzle` with its installed copy removed was read at its lockfile commit `ee80339f…` (3.0 s); requests carried only repository names, refs and file names.
  - Found live: `laravel/framework` to 13.0 reads `unavailable`, correctly. Its CHANGELOG at `v13.0.0` still holds 12.x notes (to 12.54.1), and Laravel's upgrade guide lives in `laravel/docs` (`13.x/upgrade.md`, HTTP 200). A framework plugin naming such documentation repositories is added to Phase 4.
- 2026-11-23: Phase 1 T1-T6 proven at cda5cbb on `feature/agent-code-tools` (api), not pushed. The staged tree, exported alone, passed 206 tests across the knowledge, agent-tool, navigation, capability, evidence, regression and impact suites; 13 tests in @api/Python-Service/app/tests/test_knowledge_layer.py.
  - Live check on pizza-builder (real `composer.lock` with 93 packages and 59 dev packages, real `vendor/`, zipped like an upload): lookups take 0.1-0.4 s; `symfony/http-foundation` resolves to 7.4.18 with excerpts from its CHANGELOG; `laravel/framework` 12.69.1 has only a README in its dist; asking `http-foundation` for target 8.0 reads unavailable, because the installed copy's changelog stops at 7.4: upgrade notes for a newer target need Phase 2 (files at the target tag).
  - Found live and fixed before the commit: ranking by word occurrences put an old `5.1.0` section that repeats "cookie" above the 7.x notes; sections now rank by distinct topic words matched, then newer version headings, then occurrences (test `test_excerpts_are_cited_ranked_by_topic_coverage_then_recency`).
  - Lockfiles are read through `archive_context`, which extracts `.lock` and `.json` files outside `vendor/` and `node_modules/`; installed packages are read straight from the ZIP, never extracted.
  - The optional GitHub token for Phase 2 cannot come from the OS credential store directly in the Python API (the desktop helper is reached through the web server). [assumption: an environment variable `KNOWLEDGE_GITHUB_TOKEN` first; the credential helper later]

## Declared Delta

- create plan implement-framework-knowledge-layer
- update spec framework-knowledge-layer: clauses 14-15 and provider `doc_source` added after Phase 4
- update adr gitmcp-is-the-remote-documentation-provider: four sources in order, guides repository second
