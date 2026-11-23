---
title: "Regression Check Between a Base and a Candidate Revision"
status: draft
tags:
  - "api"
  - "code-quality"
  - "frontend"
---

## Purpose & Scope

Normative for the regression check: given a base and a candidate revision of one project, report per route and per changed symbol whether observable behavior stayed the same, so developers can refactor or upgrade projects that have few or no tests. Dependents: the web UI "Regression check" view, the `regression_check` agent tool in the `code_refactor` workflow.

Covered: evidence tiers, static evidence (changed symbols, impact, contract diff, equivalence review, coverage), HTTP replay against developer-run targets, response comparison, characterization test generation, verdicts.

Not covered: building the symbol graph or impact walk (@.archcore/agent-tools/change-impact-analysis.spec.md), upgrade-specific deprecation checks, a CI endpoint, capturing request bodies in telemetry (a later phase with its own privacy decision).

## Surface

- Package @api/Python-Service/app/regression/ [assumption: new]: `model`, `store`, `replay`, `diff`, `equivalence`, `characterize`, `http`.
- Routes under `/projects/{id}/regression-checks` (create, read report, probes, runs, test bundle).
- Agent tool `regression_check`, effect `read_only` for reports; starting a replay goes through the existing tool approval flow.
- Static evidence reuses @api/Python-Service/app/inspection/impact.py `analyze_revisions`, @api/Python-Service/app/api_contracts/breaking.py `classify`, @api/Python-Service/app/code_quality/testing_checks.py.
- Replay reuses @api/Python-Service/app/code_quality/performance.py `validate_origin`, `validate_target`, `fill_route`, login and background-run limits.
- Framework capabilities (@api/Python-Service/app/agent/core/capability.py): `ROUTE_ANALYSIS`, `TEST_CONVENTIONS`, and new `PROBE_HINTS`, `CHARACTERIZATION_TESTS`, `RUN_HINTS`; from the knowledge layer (@.archcore/knowledge-layer/framework-knowledge-layer.spec.md), `INSTALLED_PACKAGES` and `DOC_SOURCES`.
- Report fields: `check_id`, `base_revision_id`, `candidate_revision_id`, `tier` (`static`, `replay`, `telemetry`), `routes[]`, `symbols[]`, `uncovered[]`, `partial[]` (missing capability and reason), `contract_changes[]`, `package_changes[]` (name, from, to, change, dev, cited upgrade notes), `baseline_checklist[]`.
- Verdicts: `unchanged`, `changed_expected`, `suspected_regression`, `not_covered`.

## Normative Behavior

1. WHEN a check is created, the regression service MUST resolve one authorized project and two ready revisions of it.
2. WHEN a check runs, the regression service MUST compute changed and impacted symbols with `analyze_revisions` on the revision pair.
3. WHEN both revisions have contract versions, the regression service MUST add `breaking.classify` items to `contract_changes`.
4. WHEN a symbol changed, the equivalence reviewer MUST compare base and candidate bodies with their callers and return a verdict with cited reasons.
5. WHEN the equivalence reviewer returns a verdict, the report MUST label it a model judgment, never as proof.
6. WHEN an impacted route or symbol has no test, runtime span or replay result, the report MUST list it in `uncovered` with verdict `not_covered`.
7. WHEN a needed framework capability is missing, the report MUST list the capability and affected evidence in `partial`.
8. WHEN no replay target exists, the regression service MUST report tier `static` and still return static evidence.
9. WHEN no replay target exists, the regression service MUST return a `baseline_checklist` built from `RUN_HINTS` and project files.
10. WHEN probes are generated, the probe builder MUST take routes from `ROUTE_ANALYSIS` records and parameters from `PROBE_HINTS` records.
11. WHEN a developer imports a HAR or request list, the probe builder MUST keep only requests to the target's origin.
12. WHEN a developer edits a probe set, the regression service MUST store the edited set as the check's probes.
13. WHEN a baseline is recorded, the replay runner MUST send every probe twice and mark fields differing between the two responses as noise.
14. WHEN a candidate is replayed, the replay runner MUST send the same probes in the same order as the baseline.
15. WHEN responses are compared, the response differ MUST compare status, selected headers and body, ignoring noise and developer ignore rules.
16. WHEN a body is JSON, the response differ MUST compare it structurally and report each differing JSON path.
17. WHEN a route's responses differ beyond noise, the report MUST give that route verdict `suspected_regression` with the differences.
18. WHEN the revisions carry tagged spans for both runs, the regression service MUST add query count, exception and p95 differences per route.
19. WHEN a developer requests tests, the test generator MUST produce tests through `CHARACTERIZATION_TESTS` for uncovered impacted routes and symbols.
20. WHEN a baseline recording exists for a route, the test generator MUST assert the recorded response in that route's test.
21. WHEN no test capability exists, the test generator MUST emit a runner-neutral HTTP request file the replay runner can import.
22. WHEN the agent tool returns a report, the tool runner MUST cite each symbol with path and line and each route with method and path.
23. WHEN the candidate's lockfile changes a package version, the report MUST list it in `package_changes` with cited upgrade notes.
24. WHEN a probe or symbol-test prompt is built, the regression service MUST add the installed framework version's guides, labelled untrusted.
25. WHEN the installed framework version falls outside a template's verified range, the test generator MUST mark the bundle partial with the reason.

## Constraints & Invariants

- The app MUST NOT build, start or execute project code or tests; the developer runs both versions. Rationale: @.archcore/regression/developer-runs-targets-app-only-sends-http.adr.md.
- Replay sends only GET and HEAD unless the target is marked disposable (@.archcore/regression/replay-writes-only-on-disposable-targets.adr.md).
- Recorded bodies live in app storage, never in ClickHouse (@.archcore/regression/replay-recordings-stay-out-of-telemetry.adr.md).
- Core modules never branch on language or framework names (@.archcore/regression/regression-evidence-comes-from-framework-capabilities.adr.md).
- Limits: 200 probes per check, 1 MB per recorded body, 50 MB per check, retention 30 days. [assumption]
- Egress follows `validate_origin`; replay shares the benchmark limits of one active run per user.
- A characterization template's verified range names the framework versions a real run passed on; it widens only after another real run passes. A template never run has no range.

## Failure Behavior

1. IF either revision is not ready or not authorized, THEN the regression service MUST refuse the check without reading the graph.
2. IF the impact engine reports coverage unavailable, THEN the report MUST say so and give no `unchanged` verdict for symbols.
3. IF a target fails origin validation, THEN the replay runner MUST refuse it with the validation reason.
4. IF a non-GET probe targets a non-disposable target, THEN the replay runner MUST skip it and list it as skipped.
5. IF the baseline is unreachable, THEN the replay runner MUST stop and report no comparison for that run.
6. IF a body exceeds the size limit, THEN the replay runner MUST store its hash only and compare by hash.
7. IF the equivalence reviewer exceeds its budget, THEN the report MUST mark remaining symbols `not_covered` with reason `budget`.

## Conformance

An implementation conforms when every verdict is backed by stored evidence, missing evidence shows as `not_covered` or `partial`, and no code path executes project code.

Given pizza-builder revisions A and B differing in one method, When a static check runs, Then `symbols` lists that method with an equivalence verdict and its routes appear in `routes` or `uncovered`.

Check map (`docker compose run --rm --no-deps python-api python -m pytest tests/<file> -k <name>`), file `test_regression_check.py` unless noted:

- 1, F1: test_rejects_unready_or_foreign_revisions
- 2–3: test_static_evidence_from_impact_and_contracts
- 4–5, F7: test_regression_equivalence.py::test_verdict_cited_labelled_budgeted
- 6–9, F2: test_tiers_uncovered_and_partial
- 10–12: test_regression_probes.py::test_probes_from_capabilities_and_har
- 13–17, F5–F6: test_regression_diff.py::test_noise_structural_diff_and_verdicts
- 14, F3–F4: test_regression_replay.py::test_order_egress_and_write_gating
- 18: test_regression_replay.py::test_span_deltas_per_route
- 19–21: test_regression_characterize.py::test_tests_from_capability_or_http_file
- 22: test_navigation_tools.py::test_regression_check_cites
- 23: package_upgrades
- 24: test_regression_probes.py, test_regression_characterize.py: framework_context
- 25: test_regression_characterize.py: verified_range

## Clarifications

- 2026-11-23: clauses 23-25 added at the knowledge-layer closeout (api 1b10db2); they record what that plan delivered to this check, so they outlive the removed plan.
- 2026-11-22: the user chose static diff, record-and-replay and characterization tests; developer-run targets; web UI and agent tool; probes from routes, HAR import and later telemetry; GET by default with writes opt-in.
- 2026-11-22: the user asked that projects never run before, with no telemetry, still get a check (tiers, baseline checklist).
- 2026-11-22: the user asked that the feature be cross-language, not PHP only.
