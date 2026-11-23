---
title: "Evidence for Process, Testing, Documentation, Localization and Accessibility"
status: draft
tags:
  - "api"
  - "code-quality"
---

## Purpose & Scope

Normative for the evidence that lets an assessment score the five categories added by @.archcore/code-quality/assessment-scores-ten-categories.adr.md: process, testing, documentation, localization and accessibility. It defines a bounded non-source evidence inventory and one deterministic collector per category. Not covered: scoring weights, the exploratory review loop, test execution (never done; @.archcore/code-quality/codebase-quality-assessment.spec.md clause 26).

## Surface

- Evidence inventory and collectors: @api/Python-Service/app/code_quality/ (new modules, no existing target).
- Existing pieces reused: the archive inventory in @api/Python-Service/app/code_quality/service.py (`inventory_archive`), front-end checks in @api/Python-Service/app/code_quality/frontend_checks.py, the symbol graph for test-to-code mapping.

## Normative Behavior

1. WHEN an assessment inventories a revision, the runner MUST also list bounded non-source evidence files: build, CI, container, dependency manifests, configuration, documentation, translations and templates.
2. WHEN the runner reads an evidence file, it MUST apply the size limits, exclusions and secret-file rules that source reads use.
2a. Documentation folders (`docs/`) that source scoring leaves out MUST stay listable and readable as evidence.
3. WHEN process evidence is collected, the collector MUST report CI and container files, toolchain version conflicts between manifests, configuration and docs, and analysis steps whose failures are ignored.
4. WHEN testing evidence is collected, the collector MUST report test files and test methods, which controllers, routes or classes tests reference, and test configuration that does not match the installed framework version.
5. WHEN testing is scored, the report MUST state that tests were not executed.
6. WHEN documentation evidence is collected, the collector MUST check that commands, scripts, files and versions named in project docs exist in the revision.
7. WHEN localization evidence is collected, the collector MUST report missing keys, placeholder mismatches and untranslated duplicates across translation files.
8. WHEN localization evidence is collected, the collector MUST report user-facing strings hardcoded outside translation files.
9. WHEN accessibility evidence is collected, the collector MUST check HTML and template files for language attributes, image alternative text, table semantics and computable colour contrast.
10. WHEN a category has no applicable evidence files, the assessment MUST leave it unassessed with a reason.
11. WHEN a collector emits a finding, the runner MUST normalize it with rule, category, severity, path, line and stable evidence id.

## Constraints & Invariants

- Collectors are read-only, offline and bounded; they never run project commands, builds or tests.
- Secret-bearing files (`.env*` except examples, keys, `auth.json`) are listed by name only, never read into evidence or model context.
- Zero findings never prove a category clean.

## Failure Behavior

1. IF a collector fails or times out, THEN the runner MUST record the failure and mark its category partial.
2. IF an evidence file is malformed, THEN the collector MUST report it as skipped with a reason.

## Conformance

Conforming when each of the five categories is scored only from bounded, read-only evidence of its own kind, and a category without such evidence is reported unassessed.
