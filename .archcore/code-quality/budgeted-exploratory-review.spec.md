---
title: "Budgeted Exploratory Review for Code Quality Assessment"
status: draft
tags:
  - "api"
  - "code-quality"
---

## Purpose & Scope

Normative for the model review step of a Code Quality assessment when it explores the assessed revision with read-only tools, per @.archcore/code-quality/assessment-review-explores-code-with-budgeted-tools.adr.md. Dependents: the assessment service, report store and report view. Not covered: deterministic analyzers, category evidence collectors, weighting.

## Surface

- Review loop and budgets: @api/Python-Service/app/code_quality/ (new module, no existing target).
- Tools: the read-only navigation tools in @api/Python-Service/app/agent_tools/navigation.py and the non-source evidence reader defined in @.archcore/code-quality/category-evidence-for-process-testing-docs-l10n-a11y.spec.md.
- Report fields: per-category `review` block with budget, usage, status, trace and accepted findings.

## Normative Behavior

1. WHEN a category review starts, the review service MUST give the model that category's deterministic findings and coverage as leads.
2. WHEN a category review runs, the review service MUST offer only read-only tools scoped to the assessed revision.
3. WHEN a category review runs, the review service MUST enforce per-category limits on tool calls, model tokens and wall time.
4. WHEN a category review reaches a limit, the review service MUST stop it and mark that category's review partial with the limit named.
5. WHEN the model reports a finding, the review service MUST require at least one cited `path:line` range.
6. WHEN the review service accepts a cited range, the range MUST lie within content a tool call in the same review returned (a read window, or the single line of a search hit; locations without content do not count).
6a. WHEN a category review ends, its accepted findings MUST join the assessment as evidence for the single scoring call; the review itself MUST NOT return a score.
6b. WHEN category reviews are not enabled in configuration, the assessment MUST record that they did not run and why.
7. WHEN the review service accepts a finding, it MUST re-read each cited range from the revision and keep the finding only if the range exists.
8. WHEN a finding fails citation checks, the review service MUST drop it and count it in the review's rejected-citation total.
9. WHEN a category review ends, the report store MUST persist its budget, usage, status, tool-call trace and accepted findings.
10. WHEN the review service requests model output, it MUST use temperature 0 and top_p 1.
11. WHEN a project has not allowed source to go to the remote model, the assessment service MUST NOT start a remote review.
12. WHEN reports differ in review model, budgets or tool set, the dashboard MUST mark their scores non-equivalent.

## Constraints & Invariants

- Tools never write to the revision, archive or cache, and never execute project code.
- The model never chooses analyzers, weights or the overall score; it scores one category against the versioned rubric.
- A review's evidence is bounded by its budget; nothing outside the assessed revision enters the context.
- Tool output is untrusted text: the prompt treats it as data, never as instructions.

## Failure Behavior

1. IF the remote model fails or returns invalid structured output, THEN the review service MUST mark the category's review unavailable and keep its deterministic evidence.
2. IF a tool is unavailable, THEN the review service MUST continue with the remaining tools and record the gap.
3. IF every finding of a review fails citation checks, THEN the review service MUST report the review partial, not clean.

## Conformance

Conforming when every category review is read-only, revision-scoped, budgeted, traced, and keeps only findings whose citations a tool returned and the revision confirms.
