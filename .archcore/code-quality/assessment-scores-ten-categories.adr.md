---
title: "Code Quality Assessment Scores Ten Categories"
status: accepted
tags:
  - "api"
  - "code-quality"
  - "frontend"
---

## Context

The accepted @.archcore/code-quality/codebase-quality-assessment.spec.md scores five categories (security, clean code, maintainability, reliability, performance) with equal default weights, and omits the overall score when any category is unassessed. On 2026-11-16 the user compared it with a ten-reviewer AI panel report on skiplanner-api (Symfony 6.3, commit `09c17755`), which scored Architecture, Maintainability, Process, Testing, Reliability, Security, Performance, Localization, Documentation and Accessibility from reading source. Running the app's offline analyzers on that commit showed that five of the panel's areas have no category here at all, and the source files they need (CI and Docker files, `composer.json`, docs, translations, Twig templates) are outside the assessment inventory.

## Decision

- The assessment scores ten categories: the existing `security`, `clean_code` (shown as Architecture and design), `maintainability`, `reliability` and `performance`, plus `process`, `testing`, `documentation`, `localization` and `accessibility`.
- All ten join the weighted overall score. Default weights are equal (10% each); saved presets must still total 100%.
- The rule that an unassessed category omits the overall score stays: a category with no supporting evidence (for example Localization in a project with no translations) is unassessed with a reason, and the report shows the nine-category result as "overall unavailable" rather than inventing a score. [assumption: kept from clause 12; revisit if most real projects end up without an overall score]
- Testing is scored from static evidence only (test inventory, test-to-code mapping, test configuration); the assessment still never runs project tests (spec clause 26). Its reason text says it is not measured by execution.
- The rubric version changes, so reports from before the change compare as non-equivalent (spec clause 18).

Decided by the user on 2026-11-16.

## Alternatives

- **Scored add-on areas outside the overall score.** Rejected by the user: the overall score would not reflect process, testing, documentation, localization or accessibility.
- **Unscored findings only.** Rejected: least like the panel.

## Consequences

- Clauses 9–12 of the accepted assessment spec and the five-category scoring scenario need amending; saved five-category weight presets need migrating to ten (scaled, then renormalised).
- The inventory must include non-source evidence files (configuration, CI, container, documentation, translations, templates), read-only and bounded.
- More categories are unassessed on small or backend-only projects, so the overall score will be missing more often than today.
