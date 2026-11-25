---
title: "Codebase Quality Assessment"
status: accepted
tags:
  - "api"
  - "code-quality"
  - "frontend"
---

## Purpose & Scope

This specification defines on-demand static quality assessment for one selected, immutable indexed project revision and live HTTP performance benchmarking against a configured target. It also covers fetching source from a configured Git repository and assembling compact, revision-scoped code evidence for AI semantic review. The assessment UI, API, analyzers, rule management, benchmark runner, source synchronization, and reports rely on this contract. It covers language-aware evidence-based scoring, configurable weights, report and benchmark history, comparison dashboards, partial coverage, custom rule management, and user-configured HTTP benchmarks; it excludes refactoring and cross-project leaderboards.

For the official Clean Code score, @.archcore/code-quality/deterministic-clean-code-assessment.spec.md defines the metric-based path and its legacy fallback. Its rules supersede this specification's evaluator-scoring clauses for Clean Code when that path produces a score.

## Surface

- Assessment UI, history, comparison, and authenticated proxy: @web/src/App.vue, @web/src/components/CodeQualityView.vue, and @web/src/server/codeQualityRoutes.ts.
- API registration, Git source synchronization, indexed source access, and revision-scoped retrieval: @api/Python-Service/app/main.py, @api/Python-Service/app/telemetry/source.py, and @api/Python-Service/app/telemetry/snapshot.py.
- Assessment, rule management, scoring, benchmarking, and report persistence: @api/Python-Service/app/code_quality/.
- Sampled GET route suggestions: @api/Python-Service/app/telemetry/runtime.py.

## Normative Behavior

1. WHEN a user starts an assessment, the API MUST authorize access to the selected project and immutable revision.
2. WHEN an authorized revision is assessed, the analyzer runner MUST inventory supported business source files and their programming languages.
3. WHEN inventorying source files, the runner MUST discover supported files without assuming a framework-specific source root such as `src/`.
4. WHEN inventorying source files, the runner MUST exclude source-like files beneath dependency, build, cache, log, generated, and temporary artifact directories.
5. WHEN inventorying source files, the runner MUST report the count of excluded source-like files separately from scanned files.
6. WHEN the runner selects analyzers, the runner MUST follow a fixed policy matched to detected languages and assessment categories.
4. WHEN an analyzer is registered, the policy MUST declare its supported languages, categories, version, and execution limits.
5. WHEN an analyzer runs, the runner MUST prevent source changes and enforce time and output limits.
6. WHEN evidence reaches the evaluator, the evaluator MUST receive normalized findings and the versioned rubric.
7. WHEN the evaluator scores a category, the evaluator MUST return a 0–100 score and cite supplied evidence identifiers.
8. WHEN evidence cannot support a category score, the evaluator MUST return that category as not assessed with a reason.
9. WHEN no user weight preset exists, the scoring service MUST assign equal weights to the ten categories (decision @.archcore/code-quality/assessment-scores-ten-categories.adr.md).
9a. WHEN a saved preset holds only the five source categories, the scoring service MUST scale those weights to half the total and give each new category the default weight.
9b. WHEN an evidence category (process, testing, documentation, localization, accessibility) has no collected findings, the scoring service MUST leave it unassessed with a reason instead of asking the evaluator to score it.
10. WHEN a user saves category weights, the scoring service MUST require their total to equal 100 percent.
11. WHEN every category has an assessed score, the scoring service MUST calculate the weighted overall score.
12. WHEN any category is not assessed, the scoring service MUST omit the overall score, except Localization and Accessibility: WHEN one has no evidence of its own kind (no translation catalogues, no templates or HTML), the scoring service MUST mark it not applicable, leave it out of the overall score and rescale the remaining weights to 100 percent, and the report MUST list the categories left out (decision @.archcore/code-quality/assessment-scores-ten-categories.adr.md, amended 2026-11-25).
13. WHEN an assessment completes, the report store MUST persist its revision, timestamp, scores, weights, coverage, and findings.
14. WHEN a user reruns an assessment, the report store MUST preserve earlier reports for that revision.
15. WHEN a report completes, the report store MUST keep that report immutable until an authorized user explicitly deletes it.
16. WHEN a user opens project history, the dashboard MUST list saved reports by revision, time, overall score, category scores, coverage, and token usage.
17. WHEN a user selects two reports for one project, the dashboard MUST compare category scores, overall scores, and finding changes.
18. WHEN compared reports differ in rubric, model, analyzer versions, coverage, weights, or inference parameters, the dashboard MUST mark score deltas as non-equivalent.
19. WHEN project history is empty, the dashboard MUST show an empty state and an assessment action.
20. WHEN a user opens, compares, or deletes a report, the API MUST authorize access to its project.
21. WHEN a report is displayed, the UI MUST show category scores, coverage, reasons, evidence-linked findings, and evaluator token usage with its source.
22. WHEN the scoring service requests a model score, it MUST use temperature 0 and top_p 1 and persist both parameters in the report.
23. WHEN an assessment is running, the Code quality view MUST show an accessible indeterminate progress indicator.
24. WHEN report content exceeds the viewport, the Code quality view MUST allow users to scroll vertically through the report.
25. WHEN the scoring service requests model scores, it MUST request provider-constrained JSON output with a completion budget sufficient for all categories.
26. WHEN an assessment runs, the runner MUST NOT execute project test suites or project-defined commands.
27. WHEN static analyzers are applicable to Reliability or Performance, the runner MUST collect their bounded, normalized source findings.
28. WHEN static evidence cannot support a category score, the evaluator MUST leave that category unassessed and explain the analyzer or retrieval gap.
29. WHEN a user opens Code Quality, the view MUST provide a Setup Performance tab separate from static assessment results.
30. WHEN Setup Performance offers routes, the view MUST list GET routes from Runtime history, allow explicitly added GET paths, and require explicit user selection.
31. WHEN no benchmark target is configured, Setup Performance MUST default to the local Docker target used by Runtime telemetry.
32. WHEN a benchmark starts, the runner MUST request only the configured target and explicitly selected GET routes.
33. WHEN a user configures benchmark workload, the view MUST allow adjustment of virtual-user count and run duration.
34. WHEN a virtual user uses HTTP login, the runner MUST support an optional CSRF bootstrap request before login.
35. WHEN a virtual user completes HTTP login, the runner MUST reuse that user's session for selected routes.
36. WHEN the runner measures route performance, the runner MUST exclude login and CSRF bootstrap requests from route results.
37. WHEN a user configures Bearer authentication, the credential manager MUST save the token in the OS credential store scoped to its target.
38. WHEN a user manages target authentication, the view MUST allow saving, replacing, and removing that target's Bearer token.
39. WHEN a scenario starts for a Bearer-authenticated target, the runner MUST retrieve that target's token.
40. WHEN the runner sends a Bearer-authenticated request, the runner MUST attach the token only to selected routes at the configured target.
41. WHEN a scenario uses HTTP login, the view MUST request credentials for that run.
42. WHEN the runner receives HTTP login credentials, the runner MUST hold them only in memory.
43. WHEN a benchmark completes, the report store MUST persist its target, selected routes, authentication mode, workload, timestamp, code revision, per-route latency percentiles, throughput, status-class counts, errors, and tool version.
44. WHEN the view displays benchmark results, it MUST show them separately from the static Performance score.
45. WHEN a user opens benchmark history, the dashboard MUST list saved runs and their target, routes, and workload settings.
46. WHEN a user compares benchmark runs, the dashboard MUST mark results non-equivalent when targets, routes, authentication modes, workloads, or tool versions differ, and MUST show whether the code revision changed.
47. WHEN the report store persists benchmark results, it MUST omit Bearer tokens, login credentials, CSRF tokens, and session cookies.
48. WHEN benchmark credentials are used, the runner MUST omit them from logs and model context.
49. WHEN the evaluator returns provider token usage, the scoring service MUST persist prompt, completion, and total token counts with provider provenance.
50. WHEN the evaluator omits token usage, the scoring service MUST estimate prompt and completion counts and mark them as estimates.
51. WHEN a report displays token usage, the view MUST distinguish provider-reported counts from estimates.
52. WHEN a user compares saved reports, the dashboard MUST display each report's total evaluator token usage.
53. WHEN an assessment completes, the report store MUST persist tool versions, rubric version, model, and inference parameters.
54. WHEN an assessment completes, the report store MUST persist evaluator token usage with its source provenance.
55. WHEN static analyzers are selected, the runner MUST apply pinned tools and built-in or validated user rules matched to languages and categories.
56. WHEN a static analyzer runs, the runner MUST report tool and rule versions, applicable languages, scanned files, skipped files, and outcome.
57. WHEN a finding is normalized, the runner MUST retain its rule identifier, category, severity, source location, and stable evidence identifier.
58. WHEN category coverage is reported, the report MUST include scanned and skipped file counts grouped by language and analyzer outcome.
59. IF a category has only zero-finding coverage metadata, THEN the evaluator MUST leave it unassessed and explain that defects are not ruled out.
60. WHEN static Performance is assessed, the evaluator MUST use static code evidence and keep live benchmark measurements separate.
61. WHEN a static analyzer executes, the runner MUST read inventoried business source and enforce time, memory, output, and network limits.
62. WHEN a registered language or framework analyzer is unavailable, the report MUST identify unsupported files and affected categories.
63. WHEN Semgrep runs, the runner MUST use a pinned version and offline built-in or validated user rules without runtime downloads.
63a. WHEN a detected framework plugin provides the `route_analysis` and `route_authorization` capabilities, the route authorization check MUST read only the normalized route, guard and access-rule records those plugins produce from language facts and configuration in the revision archive, without executing project code and without naming any framework itself.
63b. WHEN the route authorization check evaluates a route and HTTP method, it MUST treat as protection a non-public guard on the handler, the handler's class, or the route, or a deciding access rule with requirements; access rules are evaluated layer by layer against the route path with placeholders filled in, taking the first non-conditional match in each layer and continuing to the next layer only after a pass-through match.
63c. WHEN a route has no such protection and neither its guards nor the deciding access rule explicitly allow anonymous access, the check MUST emit a Security finding at the handler (error for state-changing methods, warning for GET, HEAD and OPTIONS) and count it in the report's protected / public / unprotected route summary.
63d. WHEN a framework is detected whose plugin does not provide these capabilities, the report MUST show the route authorization check as partial or unavailable coverage with the plugin named instead of reporting zero unprotected routes.
63e. WHEN a revision contains PHP source, the taint analyzer MUST run Psalm with an analyzer-owned root and configuration, never loading project autoloaders, configuration or plugins.
63f. WHEN detected framework plugins provide taint models, the taint analyzer MUST include them; WHEN a detected PHP framework provides none, the report MUST name that gap.
63g. IF the analyzer worker does not run taint analysis, THEN the API MUST run it locally when Psalm is installed, otherwise report it unavailable.
63h. WHEN a user saves intentionally public route patterns, the API MUST validate and persist them for that project.
63i. WHEN an unprotected route matches a declared public pattern, the route authorization check MUST report it public and name the pattern.
63j. WHEN a revision contains PHP code files, the assessment MUST report `declare(strict_types=1)` coverage as one maintainability finding with a per-file list.
63k. WHEN detected framework plugins provide configuration risks, the assessment MUST report each as a finding in its own category and severity.
63l. WHEN a plugin reads configuration for risks, it MUST NOT read `.env` files or include secret values in messages.
63m. WHEN the service synchronizes a Git branch, it SHOULD also store up to 500 commits of the head's history as a bundle beside the revision's source archive; a history that cannot be fetched or exceeds 64 MiB MUST NOT fail the synchronization.
63n. WHEN a revision has stored history, the assessment MUST scan it for committed secrets with the pinned scanner (on the analyzer worker when it offers the scanner, otherwise in the API) and report each secret once per rule, file and line, at the newest commit that carries it with the number of commits that do. Rules for a known credential format MUST be security errors; keyword-and-entropy rules (`generic-*`) MUST be security warnings and MUST NOT displace errors under the finding limit.
63o. WHEN a revision has no stored history (uploaded archives, working trees, revisions synchronized before history was kept), the assessment MUST report the history scan as not applicable rather than clean.
63p. The history scan MUST NOT report, store or log a secret value, matched text or commit author, MUST scan a bare repository built from the bundle (no project scanner configuration, ignore file or allow comment applies), and MUST NOT validate secrets against their providers.
63q. WHEN a revision is removed, the service MUST remove its stored history with its source archive.
64. WHEN a user opens rule management, the view MUST allow creating, editing, enabling, disabling, and deleting custom rules; built-in rules remain read-only.
65. WHEN a user saves a custom rule, the API MUST validate its schema, language, category, and limits, then scope storage to that user.
66. WHEN a user tests a custom rule, the runner MUST scan an authorized revision offline and return bounded findings without saving an assessment report.
67. WHEN a report uses custom rules, the report store MUST preserve each rule definition and version or content hash.
68. IF a custom rule is invalid or unsupported, THEN the API MUST reject it with an actionable validation error.
69. WHEN a user connects a Git repository, the source service MUST store its repository identity and selected branch under that user's project access.
70. WHEN the source service synchronizes a Git branch, it MUST fetch the branch head and compare its full commit SHA with the latest indexed Git revision, ignoring upload and working-tree revisions.
71. WHEN a fetched commit is not indexed, the source service MUST create an immutable source snapshot and queue its indexing.
72. WHEN a fetched commit already has a complete indexed snapshot, the source service MUST reuse that revision without creating duplicate indexed content.
73. WHEN a Git provider webhook triggers synchronization, the source service MUST verify the event and fetch the branch before treating its commit as current.
74. WHEN a user requests synchronization, the source service MUST fetch and compare the selected branch even if no webhook was received.
75. WHEN source content is indexed, the indexer MUST associate every searchable code chunk with its project and immutable revision identity.
75a. WHEN chat answers from a project's latest revision, it MAY read a working-tree revision.
75b. WHEN selecting a revision to assess, the assessment service MUST NOT accept a working-tree revision.
76. WHEN semantic retrieval runs, the service MUST filter by assessed project and revision.
77. WHEN semantic retrieval returns code, the service MUST include its source location.
78. IF the assessed revision lacks a complete search index, THEN semantic review MUST report unavailable context.
79. WHEN semantic review sends context to the evaluator, it MUST bound retrieved code evidence.
80. WHEN semantic review sends context to the evaluator, it MUST include compact analyzer traces.
81. WHEN Git hooks are configured, the source service MAY use them to trigger synchronization.
82. WHEN a user saves assessment exclusions, the API MUST persist validated project-relative glob patterns for that project.
83. WHEN applying exclusions, the analyzer runner MUST filter normalized business-source paths before static analysis and semantic retrieval.
84. WHEN exclusions match a file or directory, the runner MUST exclude that path and every descendant from source-based evidence collection.
85. WHEN an assessment completes, the report store MUST persist effective exclusion patterns and their excluded source-file count.
86. WHEN reports use different exclusion patterns, the dashboard MUST mark their score comparisons as non-equivalent.
87. IF an exclusion pattern is absolute, traverses a parent directory, or exceeds configured limits, THEN the API MUST reject it with an actionable error.
88. IF exclusions leave no supported source files, THEN the report MUST explain the exclusion impact and leave categories unassessed.
89. WHEN an assessment completes, the report store MUST retain static analyzer outcomes and per-language, per-category coverage.
90. WHEN semantic evidence is used, the assessment service MUST retain its revision identity, source locations, and retrieval or trace status.
91. IF static analyzers or semantic retrieval cannot provide direct category evidence, THEN the report MUST identify the gap without inferring that defects are absent.
92. WHEN revision-scoped Clean Code snippets reach the evaluator, the evaluator MUST return each candidate observation with supplied semantic evidence identifiers.
93. IF supplied revision-scoped evidence does not support a Clean Code observation, THEN the evaluator MUST omit that observation.
94. WHEN the service accepts a model-generated Clean Code observation, the service MUST derive its file and line from cited source evidence.
95. WHEN the report store persists a model-generated Clean Code observation, the report store MUST retain its cited evidence identifiers.

## Constraints & Invariants

- Source discovery is path-agnostic; artifact directories are excluded, and excluded or skipped source files are reported separately.
- The application owns analyzer selection, registration, execution, validation, limits, evidence normalization, and persistence; language gaps remain visible.
- Custom rules are user-scoped, validated, bounded, and separate from immutable built-in rules.
- The evaluator receives bounded, cited evidence and never selects analyzers or calculates the weighted overall score. Model-generated Clean Code observations must cite supplied revision-scoped code evidence and inherit its source location.
- Static Performance scores use source evidence; live latency and throughput remain separate benchmark results.
- Zero findings do not prove defect-free code; the overall score exists only when all ten categories are assessed.
- Analyzer execution is read-only, offline, and bounded; the assessment does not execute project test suites or project-defined commands.
- Runtime history supplies route suggestions only; benchmark requests remain scoped to selected routes and the configured target.
- Bearer tokens stay in the OS credential store; login credentials and session data remain transient and are omitted from persisted reports and logs.
- Reports preserve revision, weights, analyzer and rubric versions, model parameters, and token provenance; estimates remain labeled.
- Project-scoped exclusion patterns apply uniformly to source-based evidence and remain visible in report provenance; test execution remains an independent evidence source.
- Git synchronization uses fetched commit identity and immutable source content; fetch-and-compare determines the current revision. Hooks are optional notifications.
- Revision-scoped code retrieval never falls back to another revision; incomplete indexes remain unavailable.
- Assessment remains separate from per-response RAG evaluation and Runtime request analysis.

## Failure Behavior

1. IF the caller lacks project access, THEN the API MUST reject the request before reading source files.
2. IF the selected revision has no retrievable source archive, THEN the API MUST report the assessment as unavailable.
3. IF an analyzer fails or times out, THEN the runner MUST record its failure and continue eligible analyzers.
4. IF an analyzer emits malformed or oversized output, THEN the runner MUST reject that output and record a bounded error.
5. IF the evaluator returns invalid structured output, THEN the scoring service MUST mark affected scores unavailable and omit the overall score.
6. IF saved weights do not total 100 percent, THEN the scoring service MUST reject the preset without starting an assessment.
7. IF an assessment fails before producing a report, THEN the API MUST return a retryable error without saving a successful report.
8. IF a caller cannot access a report's project, THEN the API MUST reject report retrieval without revealing report contents.
9. IF a saved report cannot be loaded, THEN the dashboard MUST show a recoverable error without displaying stale comparison data.
10. IF no registered static analyzer applies to a detected language and category, THEN the report MUST mark that analyzer coverage unavailable with a reason.
11. IF the OS credential store cannot provide a configured Bearer token, THEN the runner MUST stop the affected scenario and show an actionable error.
12. IF HTTP login or its CSRF bootstrap fails, THEN the runner MUST stop protected-route execution.
13. IF HTTP login or its CSRF bootstrap fails, THEN the view MUST show the failure reason.
14. IF the configured target cannot be reached, THEN the runner MUST report the target failure without presenting route measurements as complete.
15. IF Git credentials are missing or rejected, THEN the source service MUST report an actionable synchronization error without logging credentials.
16. IF webhook delivery is missed or invalid, THEN a user-triggered or scheduled fetch MUST still be able to reconcile the selected branch.
17. IF indexing for a fetched revision is incomplete, THEN the source service MUST keep it unavailable for assessment and retrieval.
18. IF revision-scoped retrieval returns no relevant evidence, THEN the evaluator MUST report the gap without querying another revision.
19. IF exclusions are malformed or unsafe, THEN the API MUST reject the update without changing saved patterns.
20. IF exclusions remove all supported business-source files, THEN the assessment MUST return an unassessed report with an actionable explanation.
21. IF a category requires runtime behavior to establish a claim, THEN the evaluator MUST leave that claim unassessed when no trusted runtime evidence is supplied.
22. IF a model-generated Clean Code observation cites an unknown or non-source evidence identifier, THEN the service MUST discard that observation.

## Conformance

An implementation conforms when it enforces project and revision access, framework-agnostic business-source discovery, built-in and user-configured exclusions, generated-artifact exclusion, language-aware static analysis, evidence-cited scores, explicit analyzer coverage gaps, weighted aggregation, token usage provenance, immutable static report history, separate saved benchmark history, benchmark credential protections, comparison provenance, and the failure behavior above.