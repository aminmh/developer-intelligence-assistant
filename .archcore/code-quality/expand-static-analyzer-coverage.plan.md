---
title: "Expand Static Analyzer Coverage"
status: draft
tags:
  - "api"
  - "code-quality"
  - "frontend"
---

## Declared Delta

- Creates: a bounded analyzer registry, pinned built-in rules, per-user custom-rule management, and coverage reporting.
- Modifies: static evidence collection, assessment scoring inputs, and Code Quality report coverage details.
- Retires: duplicate heuristic-only checks after equivalent rules move into named analyzer rules.
- Decision: enrich static Reliability and Performance evidence; keep live HTTP benchmarking in Setup Performance.
- Intent gap: resolved by the accepted Codebase Quality Assessment specification.

## Goal

Increase concrete, explainable static evidence for Reliability and Performance across detected languages and frameworks while keeping unsupported analyzer coverage visible.

Aegis Visibility: this plan adds analyzer execution and coverage contracts across the API evidence producer and report UI consumer.

## Architecture

The Code Quality service remains the assessment coordinator. A focused analyzer registry owns tool metadata, applicability, bounded execution, and normalized results. Semgrep provides a pinned cross-language baseline through immutable built-in rules and validated user-authored rules; registered language and framework adapters add deeper checks. Users manage only their own rules through a dedicated Code Quality view. The evaluator consumes findings and per-language coverage. Static Performance uses source evidence; live measurements remain separate benchmark records.

The runner analyzes only inventoried business source files from the immutable archive. It does not execute project builds or project-defined commands. Analyzer execution is read-only, offline, time-bounded, memory-bounded, and output-bounded.

A category is covered when all applicable registered analyzers finish over their supported inventoried files. It is partial when supported files are skipped, parsing fails, or an applicable analyzer fails. It is unavailable when no registered analyzer applies. A zero-finding run reports completed checks but does not prove the absence of defects; the evaluator leaves a category unassessed when coverage metadata is its only evidence.

## Tech Stack

Python analyzer registry and normalizer, pinned Semgrep CLI with built-in and user-authored YAML rules, SQLite persistence, FastAPI API, and Vue rule-management and report views.

## Baseline/Authority Refs

- Codebase Quality Assessment specification (linked as the implemented contract).
- @api/Python-Service/app/code_quality/service.py
- @api/Python-Service/app/code_quality/http.py
- @api/Python-Service/Dockerfile
- @api/Python-Service/requirements.txt
- @api/Python-Service/app/tests/test_code_quality.py
- @web/src/components/CodeQualityView.vue
- @README.md
- Existing Code Quality implementation plan (linked as the extended baseline).

## Compatibility Boundary

Existing reports remain readable. New reports add analyzer outcomes, per-language coverage, and immutable snapshots of custom rules used. Built-in rules stay read-only; custom rules are scoped per user. Setup Performance history remains separate.

## Change Necessity

A documentation-only or prompt-only change cannot produce broader static evidence or distinguish completed scans from missing language coverage. The minimum code boundary is a registry and bounded runner, normalized coverage in the assessment report, and presentation of that coverage in the existing report view.

## TDD Route

Mode: off. Decision: skipped. Authority: current Aegis TDD Mode. Test posture: add focused analyzer, coverage, and report regressions before final verification, without a required RED/GREEN sequence. Reason: strict TDD was not requested. Verification: focused API tests, Web lint/build, and an offline assessment against a multi-language fixture archive.

## Tasks

1. Add a registry contract for analyzer versions, languages, categories, rulesets, limits, and evidence types in @api/Python-Service/app/code_quality/analyzers.py.
2. Pin the Semgrep CLI dependency in @api/Python-Service/Dockerfile and @api/Python-Service/requirements.txt.
3. Add built-in Clean Code, Reliability, and static Performance rules under @api/Python-Service/app/code_quality/rules.
4. Implement bounded offline analyzer execution and normalized findings in @api/Python-Service/app/code_quality/analyzers.py.
5. Add user-scoped custom-rule persistence, validation, CRUD, and test endpoints in @api/Python-Service/app/code_quality/rules.py and @api/Python-Service/app/code_quality/http.py.
6. Add a constrained YAML rule editor with validation, enable/disable, preview, and recovery states in @web/src/components/code-quality/RuleManager.vue and @web/src/server/codeQualityRoutes.ts.
7. Run enabled custom rules and snapshot their definitions or hashes in @api/Python-Service/app/code_quality/service.py.
8. Add citation-validated AI Clean Code observations from revision-scoped semantic snippets.
9. Replace duplicate PATTERNS execution and record analyzer outcomes and per-language coverage in @api/Python-Service/app/code_quality/service.py.
10. Keep zero-finding coverage unassessed and display coverage and language gaps in @web/src/components/CodeQualityView.vue.
11. Add API and Web tests for rule CRUD, isolation, validation, preview, report snapshots, and multi-language analyzer fixtures.
12. Document rule management and static analysis limits in @README.md.
13. Verify user-visible rule management, report provenance, and legacy report readability.

## Acceptance Criteria

- The analyzer registry declares tool and rule versions, categories, languages, limits, and normalized evidence shape.
- Semgrep uses a pinned version and offline built-in or validated user rules without runtime rule downloads.
- Users can create, edit, enable, disable, test, and delete custom rules in Code Quality; built-in rules are read-only.
- Custom rules are schema-validated, bounded, stored per user, and cannot include external rules or arbitrary commands.
- Reports preserve immutable custom-rule definitions or hashes so later edits do not rewrite history.
- Clean Code has concrete built-in Semgrep rules, and AI-generated Clean Code observations cite only supplied revision-scoped source snippets.
- Reliability and static Performance have concrete language-aware rules beyond the current small regex set.
- Analyzer output identifies rule, severity, file, line, category, stable evidence ID, and tool provenance.
- Coverage distinguishes covered, partial, unavailable, and failed analyzer outcomes by category and language.
- Reports separate scanned, skipped, unsupported, and excluded source files.
- A category with only zero-finding coverage remains unassessed and explains the evidence limitation.
- The evaluator cannot emit unsupported findings; AI-generated Clean Code observations must cite supplied revision-scoped snippets, while zero findings do not prove defect-free code.
- Static Performance evidence never includes live latency or throughput; those remain in Setup Performance history.
- The runner scans immutable business sources without executing builds, user commands, or downloaded rules.
- Existing assessment reports remain readable and existing benchmark behavior remains unchanged.

## Verification

- API: verify analyzer matches, clean scans, unsupported languages, malformed rules/output, timeouts, and failures across multi-language fixtures.
- API and Web: verify custom-rule CRUD, user isolation, schema rejection, preview, enabled selection, and immutable report snapshots.
- API: run existing Code Quality assessment tests and confirm zero-finding coverage alone remains unassessed.
- Security: inspect subprocess arguments and environment to confirm fixed commands, offline execution, read-only source, and bounded resources.
- Web: run `npm run lint` and `npm run build` from `web/`.
- User-visible: assess the Pizza revision and inspect Reliability and static Performance coverage, analyzer versions, findings, and gaps.
- Compatibility: load a pre-change saved report and compare static reports without mixing Setup Performance measurements.

## Dependencies

- Pinned Semgrep package, offline execution, version-controlled built-in rules, and a per-user custom-rule store.
- The existing business-source inventory and immutable revision archive.

## Risks

- Semgrep language support and rule depth vary; per-language coverage must expose those limits.
- Broad custom rules can produce noisy findings; validation, preview, fixtures, and severity guidance reduce but cannot eliminate this risk.
- Framework-specific performance issues require registered adapters and cannot be inferred from generic cross-language rules.
- Static analysis identifies likely inefficiencies but cannot measure live latency or throughput.
- Analyzer packaging increases API image size and startup/build cost; pin minimal dependencies and avoid running analyzers outside assessment requests.

## Retirement

Migrate overlapping heuristic patterns into named rules with equivalent fixtures, then remove the duplicate checks from `service.py`. Retain only structural measurements that have no equivalent analyzer rule, label them as application-owned metrics, and preserve their evidence IDs during report generation.