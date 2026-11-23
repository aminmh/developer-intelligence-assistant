---
title: "External Static Analyzer Integrations"
status: draft
tags:
  - "api"
  - "code-quality"
  - "frontend"
---

## Purpose & Scope

Defines optional SonarQube, Joern, SCIP, LSP indexer, and Rector analyzer integrations for Code Quality assessments and the symbol graph. Integrations enrich revision-scoped evidence; they do not independently assign category scores or prove that a codebase is defect-free. SonarQube findings may depend on server edition, analyzer version, language, and analysis mode. Joern findings depend on language frontend and graph coverage. This specification does not imply either integration is currently implemented.

## Surface

- Analyzer registration, orchestration, normalization, and assessment provenance: @api/Python-Service/app/code_quality/.
- Project-scoped integration settings and authenticated Web proxy: @api/Python-Service/app/code_quality/http.py and @web/src/server/codeQualityRoutes.ts.
- Integration setup, coverage, and finding provenance: @web/src/components/CodeQualityView.vue.
- Existing assessment and analyzer contracts: @.archcore/code-quality/codebase-quality-assessment.spec.md and @.archcore/code-quality/expand-static-analyzer-coverage.plan.md.

## Normative Behavior

1. WHEN an administrator enables an integration, the settings service MUST scope its configuration to an authorized project.
2. WHEN an analyzer is registered, the registry MUST identify its name, version, supported languages, evidence types, categories, and execution limits.
3. WHEN an assessment selects an external analyzer, the orchestrator MUST require an explicit project-level enablement.
4. WHEN an external analyzer produces results, the normalizer MUST retain finding identity, category, severity, language, path, line range, and tool provenance.
5. WHEN an analyzer reports coverage, the report store MUST retain scanned files, unsupported files, skipped files, and completion status.
6. WHEN an analyzer emits findings, the assessment service MUST provide bounded normalized evidence to the existing evaluator.
7. WHEN the evaluator receives analyzer evidence, it MUST cite supplied evidence identifiers and apply the existing assessment rubric.
8. WHEN an analyzer produces no findings, the evaluator MUST NOT treat that result alone as proof that a category has no defects.
9. WHEN an analyzer is unavailable or fails, the assessment service MUST preserve other analyzer results and report the affected coverage as partial or unavailable.
10. WHEN a SonarQube integration is configured, the settings service MUST record server identity, edition when available, scanner version, and enabled language capabilities.
11. WHEN SonarQube analysis requires project source transmission, the setup view MUST disclose the destination and require explicit user consent before upload.
12. WHEN a SonarQube token is configured, the credential manager MUST store it in the OS credential store.
24. WHEN a SonarQube token is used, each service MUST omit it from reports, logs, and model context.
13. WHEN a SonarQube server is contacted, the connector MUST use the configured server and enforce the approved network and timeout limits.
14. WHEN a Joern integration is configured, the registry MUST report frontend availability and graph coverage for each detected language.
15. WHEN Joern builds a code property graph, the runner MUST use only the immutable assessed source revision and enforce resource and output limits.
16. WHEN a Joern query returns graph evidence, the normalizer MUST include source locations and unresolved or unsupported graph scope.
17. WHEN an assessment completes, the report store MUST snapshot enabled integrations, versions, capabilities, and outcomes.
18. WHEN saved reports use different analyzer integrations or capabilities, the comparison view MUST mark their scores non-equivalent.
19. WHEN an integration is disabled or unavailable, the assessment service MUST continue with other eligible analyzers.
20. WHEN an integration configuration changes, the settings service MUST validate it before replacing the saved configuration.
21. WHEN a user tests SonarQube access, the connector MUST use the saved OS credential to query project analysis and issue APIs.
22. WHEN a SonarQube access check runs, the connector MUST NOT upload source or start an assessment.
23. WHEN the SonarQube access check succeeds, the setup view MUST state that project-result read access was verified and analysis-write permission was not tested.
25. WHEN a SCIP indexer runs, the worker MUST index only the immutable assessed revision without running project builds, dependency installs, or package scripts.
26. WHEN SCIP output is loaded, the graph loader MUST bound occurrences per revision and store definitions and references as symbol-graph edges with origin `scip`.
27. WHEN SCIP and parser edges describe the same reference, symbol lookup and flow tracing MUST prefer the `scip` edge.
27a. WHEN no `scip` edge exists for a reference, symbol lookup and flow tracing MUST use the parser edge.
28. WHEN a language has no supported SCIP indexer or indexing fails, the registry MUST report SCIP coverage for that language as unavailable with a reason.
29. WHEN Rector runs, the worker MUST use dry-run mode on the assessed source without the project's autoloader or dependencies.
29a. WHEN Rector runs, the worker MUST NOT change any file.
30. WHEN Rector reports a change, the normalizer MUST record the rule, path, and line range as an upgrade finding, without applying the diff.
31. WHEN Joern traces PHP data flow, the normalizer MUST report each request-to-sink path with its source, sink, and intermediate locations, bounded per revision.
32. WHEN an LSP indexer runs, the driver MUST start the language server on one immutable revision's extracted source.
32a. WHEN a project has a consented analyzer worker, the indexing service MUST run the LSP indexer on that worker.
32b. WHEN a project has no consented analyzer worker, the indexing service MUST run the LSP indexer in the API process.
32c. WHEN the LSP indexer is unavailable or fails, the indexing service MUST keep the parser's symbol export.
33. WHEN an LSP indexer starts a language server, the adapter MUST disable project builds, build scripts, procedural macros, editor plugins, and dependency downloads.
33a. WHEN an LSP indexer starts a language server, the driver MUST give it empty home and configuration directories.
34. WHEN an LSP indexer collects symbols, the driver MUST discard locations outside the revision.
34b. WHEN a reference's source text names another symbol, the driver MUST discard that reference.
34a. WHEN a language server lacks call hierarchy, the driver MUST derive call edges from each reference's enclosing definition.
35. WHEN LSP output is loaded, the graph loader MUST bound requests and occurrences per revision and store edges with origin `lsp`.
36. WHEN `scip`, `lsp`, and parser edges describe the same reference, symbol lookup MUST prefer `scip`, then `lsp`, then the parser edge.
37. WHEN an LSP indexer reaches its request, time, or output limit, the registry MUST report that language's LSP coverage as partial.

## Constraints & Invariants

- SonarQube, Joern, SCIP, and Rector are optional evidence providers and do not replace the existing analyzer registry, Semgrep baseline, or evaluator.
- Neither the analyzer worker nor the API container blocks outbound network at the process level today (the worker joins the default network for its published port); LSP isolation therefore rests on clause 33: every server feature that runs project code, loads dependencies, or downloads is off.
- LSP indexing is configured per language in @api/Python-Service/app/config/lsps.yml; PHP (phpactor) is the first language (@.archcore/agent-tools/language-servers-feed-the-symbol-graph-at-index-time.adr.md).
- SCIP indexing covers TypeScript/JavaScript and Python only; Java and PHP indexers need project builds or dependency installs and are out of scope (2026-11-11).
- Tool support is declared per installed version and capability; the system does not infer universal language coverage.
- The assessed source remains tied to one immutable project revision and effective exclusions.
- Analyzer execution remains bounded, read-only, and separate from project builds, tests, and user-defined commands.
- SonarQube source transfer requires explicit opt-in; only the configured server may receive source or analysis data.
- Joern graph completeness is limited to its declared frontends, edge types, and resource limits.
- Provider output is untrusted input and is size-limited, schema-validated, and normalized before entering reports or model context.
- Existing reports remain readable when these integrations are absent.

## Failure Behavior

1. IF a SonarQube server is unreachable or rejects credentials, THEN the connector MUST report an actionable integration failure without exposing secrets.
2. IF a SonarQube edition or analyzer lacks a detected language, THEN the report MUST identify that language as unsupported by the integration.
3. IF a provider returns malformed, oversized, or out-of-revision results, THEN the normalizer MUST reject them and record a bounded failure.
4. IF Joern cannot parse a language or construct the requested graph, THEN the report MUST mark the affected coverage partial or unavailable.
5. IF an integration exceeds its time, memory, or output limit, THEN the runner MUST stop that integration and preserve other analyzer outcomes.
6. IF credentials are missing or invalid, THEN the integration MUST stop before transmitting source or analysis requests.
7. IF a SonarQube access check is denied, THEN the setup view MUST explain the required project-result read permission separately from Execute Analysis permission.
8. IF a language server is missing, crashes, or needs a disabled feature, THEN the registry MUST report that language's LSP coverage unavailable with a reason.

## Conformance

A conforming implementation offers explicit project-scoped optional integrations for SonarQube and Joern, reports versioned language capabilities and coverage, normalizes bounded revision-scoped findings, protects credentials and source transmission, provides a source-free SonarQube read-access check, and preserves existing assessment behavior when integrations are disabled or unavailable.