---
title: "Compute Clean Code scores from versioned metrics"
status: draft
tags:
  - "api"
  - "code-quality"
---

## Context

Clean Code scores need reproducible revision-scoped evidence and consistent scoring across assessments. The evaluator's free-form numeric scores can vary between runs.

## Decision

The application computes the official Clean Code score from indexed metrics and a versioned rubric. A model provides closed verdicts for bounded candidates, cached by project, card content, rule, rubric, prompt, and model. Older revisions use the legacy evaluator until reindexed.

Amended by the user on 2026-11-19: the scoring model scores Clean Code (shown as Architecture and design) like the other categories, with score method `model-evaluator-with-design-metrics`. The metric result (score, judged verdicts, estimated classes) enters the scoring evidence as a summary finding beside the judged violations and the category review's findings, and stays on the category as `design_metric_score`. Reason: on skiplanner `09c17755` the metric score was 95 against a ten-reviewer panel's 45, because class-level metrics do not see layering or module boundaries (82% of classes had no metric over a threshold) while the panel's decisive issues were architectural.

## Alternatives

- Continue using model-generated numeric scores for Clean Code.
- Apply heuristic penalties without a model review of design candidates.

## Consequences

Scores include metric and rubric provenance, and comparisons across scoring methods are non-equivalent. PHP is the initial metric producer; unavailable metrics require reindexing or legacy scoring. Uncertain verdicts carry no penalty and reduce confidence.