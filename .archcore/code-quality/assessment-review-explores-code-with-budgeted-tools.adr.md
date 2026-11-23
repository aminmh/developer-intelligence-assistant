---
title: "Assessment Review Explores Code with Budgeted Read-Only Tools"
status: accepted
tags:
  - "api"
  - "code-quality"
---

## Context

Today the assessment's model review gets one bounded context: analyzer findings plus about 12 KB of pre-selected code (10 retrieval queries, one 1,400-character snippet each; @api/Python-Service/app/code_quality/semantic_context.py). On skiplanner-api (1,822 files) that context could not reach the panel's decisive findings: a public support route, refresh tokens accepted without a purpose check, an `rm -rf` whose path is emptied ten lines earlier, an unbounded `itemsPerPage`. The panel found them by reading files and following leads. The app already has read-only, revision-scoped navigation tools for the chat agent (@.archcore/agent-tools/source-code-navigation-tools.spec.md): text and structural search, file read, outline, symbol lookup, repository map.

## Decision

- Each category's model review runs as a bounded tool-using loop over the assessed revision, using the existing read-only navigation tools and the new non-source evidence reader. It starts from that category's deterministic findings and coverage as leads.
- Budgets per category: tool calls, model tokens and wall time (defaults set in configuration and recorded in the report); hitting a budget makes the category's review partial, never silently complete.
- A finding the model reports must cite `path:line` ranges that a tool call in that run returned; the service re-reads each cited range from the revision and drops findings whose citation does not match.
- Scores stay on the versioned rubric at temperature 0; the tool-call trace (tool, arguments, result size, cited ranges) is stored with the report.
- Source goes to the remote model only under the existing per-project consent (@.archcore/agent-tools/remote-model-source-access-requires-project-consent.adr.md).

Decided by the user on 2026-11-16.

## Alternatives

- **Keep the fixed context and improve only deterministic checks.** Rejected by the user: cheaper and fully reproducible, but deep, cross-file issues stay out of reach.

## Consequences

- The accepted spec's constraint "the evaluator receives bounded, cited evidence" holds through the budgets and the citation check, but its wording needs amending to cover tool-gathered evidence.
- Token use per assessment rises (ten categories × budget); the report shows it per category, and the budget caps it.
- Runs are no longer identical: tool choices can vary between models and provider versions. The deterministic findings and metric scores stay reproducible; comparisons mark model or budget changes non-equivalent.
