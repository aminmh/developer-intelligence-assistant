---
title: "Ten-Category Assessment Scoring Scenarios"
status: draft
tags:
  - "actor:user"
  - "component:code-quality"
---

## Subject

A project owner assesses one immutable indexed revision and verifies that the report scores every category when the fixture supplies sufficient evidence.

## Actors

| Actor | Description |
| --- | --- |
| Project owner | Starts and reviews a Code Quality assessment. |
| Code Quality system | Runs preflight, source analysis, retrieval, scoring, and report persistence. |

## Flows

### Successful assessment scores all ten categories

Anchors: @.archcore/code-quality/codebase-quality-assessment.spec.md

- Given the project owner selects an authorized immutable revision with a complete revision-scoped index.
- And the revision fixture contains citeable evidence for Security, Architecture and design, Maintainability, Reliability and Performance, and collected findings for Process, Testing, Documentation, Localization and Accessibility.
- And the scoring fixture returns a valid 0–100 score and evidence citations for each category.
- When the project owner starts the assessment.
- Then the Code Quality system inventories business source files and reports analyzer coverage.
- And the Code Quality system retrieves bounded evidence only from the selected project revision.
- And the Code Quality system records assessed scores and valid citations for all ten categories.
- And the Code Quality system calculates the equal-weight overall score.
- And the Code Quality system saves a report with revision, coverage, model, rubric, and token provenance.

### Missing semantic index is reported before scoring

Anchors: @.archcore/code-quality/codebase-quality-assessment.spec.md

- Given the project owner selects an authorized revision without a complete search index.
- When the project owner starts the assessment.
- Then the Code Quality system reports semantic context as unavailable for that revision.
- And the Code Quality system does not retrieve code from another revision.
- And the Code Quality system marks unsupported categories unassessed with specific evidence gaps.
- And the Code Quality system omits the overall score while any category remains unassessed.

### Retrieval budget preserves category coverage

Anchors: @.archcore/code-quality/codebase-quality-assessment.spec.md

- Given the revision fixture has relevant snippets for all five source categories.
- And early category queries return enough candidates to consume the configured retrieval budget.
- When the Code Quality system retrieves assessment evidence.
- Then the Code Quality system attempts retrieval for Security, Clean Code, Maintainability, Reliability, and Performance.
- And the Code Quality system includes bounded, revision-scoped evidence from each category in the scoring context.
- And the Code Quality system records attempted, accepted, and rejected evidence counts by category.
- And the Code Quality system does not report all categories assessed when any category lacks citeable evidence.

### Model request exceeds its configured budget

Anchors: @.archcore/code-quality/codebase-quality-assessment.spec.md

- Given retrieved evidence for every scored category exceeds the configured model request budget.
- When the Code Quality system prepares the scoring request.
- Then the Code Quality system reduces lower-priority context while preserving category representation.
- And the Code Quality system verifies the request fits the configured input and completion limits before sending it.
- And the Code Quality system reports the model limit and affected categories if the request still cannot fit.
- And the Code Quality system does not persist an all-category score unless every score has valid evidence citations.

### Analyzer or evaluator failure leaves an explicit gap

Anchors: @.archcore/code-quality/codebase-quality-assessment.spec.md

- Given an applicable analyzer fails or the evaluator returns invalid structured output for one category.
- When the Code Quality system completes the assessment attempt.
- Then the Code Quality system records the failed analyzer or scoring outcome and its bounded reason.
- And the Code Quality system continues eligible analyzer work when possible.
- And the Code Quality system marks unsupported categories unassessed instead of treating missing findings as zero defects.
- And the Code Quality system omits the overall score until all ten categories have assessed scores.

### Evidence category without evidence stays unassessed

Anchors: @.archcore/code-quality/codebase-quality-assessment.spec.md

- Given the revision has no translation files or other localization evidence.
- When the project owner starts the assessment.
- Then the Code Quality system does not ask the evaluator to score Localization.
- And the Code Quality system marks Localization unassessed with a reason naming the missing evidence.
- And the Code Quality system omits the overall score.

## Examples

- Ten valid scores of 90, 80, 70, 60, 50, 90, 80, 70, 60 and 50 produce an equal-weight overall score of 70.
- A saved five-category preset of 60/10/10/10/10 reads back as 30/5/5/5/5 plus 10 for each new category.
- A valid score without a citation to supplied evidence does not count as an assessed category.
- Zero-finding coverage metadata alone leaves the category unassessed.
- A missing Reliability or Performance score keeps the overall score unavailable.

## Open Questions

- Which evidence-count and relevance thresholds should qualify a category as sufficiently supported?
- Should a failed preflight automatically rebuild the revision index, or ask the project owner to retry after showing the blocker?
- Which production readiness signal should distinguish an index rebuild in progress from an unavailable index?