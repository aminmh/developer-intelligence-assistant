---
title: "Business Rule Knowledge for Developers"
status: draft
tags:
  - "api"
  - "code-quality"
  - "frontend"
---

## Purpose & Scope

Makes Outline business rules usable while developing: a synced rule index, rule ↔ code links, chat sources, diff impact checks, drift flags, runtime health per rule and test skeletons. Assessment review stays governed by `business-rule-semantic-review.spec.md`.

## Surface

- Python store `code_quality/rule_index.py`: tables `business_rule_documents`, `business_rule_sync_events`, `business_rule_links`, `business_rule_settings`.
- Python routes under `/code-quality/projects/{id}`: `outline/sync`, and under `rule-knowledge`: the index (GET), `settings`, `links` (GET by doc, symbol or file; PUT), `links/suggest`, `coverage`, `impact`, `runtime`, `{doc_id}/tests`. `/code-quality/outline/connections` lists connected projects for the scheduler.
- Node pass-throughs under `/api/code-quality/projects/:id/...` with the project access check; Node runs the 10-minute sync scheduler.
- Code: @api/Python-Service/app/code_quality/rule_index.py, @api/Python-Service/app/code_quality/rule_code.py, @api/Python-Service/app/code_quality/rule_search.py, @api/Python-Service/app/code_quality/rule_assist.py, @web/src/components/code-quality/RuleKnowledgePanel.vue, @web/src/components/code-quality/RuleLinksPanel.vue.
- Chat answer field `business_rules`.
- Link identity: canonical `symbol_id` from `telemetry/symbols.py` (methods, `route:` and `table:` ids).

## Normative Behavior

1. WHEN a sync runs, the rule index MUST upsert every fetched document with its revision, content hash and `synced_at`.
2. WHEN a synced collection no longer contains a document, the rule index MUST set its `deleted_at` and record a `removed` event.
3. WHEN a document's revision or content hash changes, the rule index MUST record a `changed` event with old and new revision.
4. WHEN an assessment fetches rules, the service MUST also sync them into the rule index.
5. The Node server MUST start every sync, because only it can obtain the Outline token.
6. IF the credential helper is unreachable during a scheduled sync, THEN the scheduler MUST skip that project without error.
7. WHEN suggestions are generated, the service MUST NOT change links whose state is confirmed, rejected or manual.
8. WHEN a user confirms a link, the service MUST store the current rule revision and code hash on it.
9. WHILE a confirmed link's rule revision is newer than its stored revision, the service MUST report it as `rule_changed`.
10. WHILE a confirmed link's code hash differs from the latest indexed code, the service MUST report it as `code_changed`.
11. WHEN chat answers a project question the web server verified access to, the answer service MUST add at most 3 matching rule excerpts and return them in `business_rules`.
18. WHEN suggesting links, the service MUST skip test files and hits below the minimum similarity score.
12. Every rule source shown to a user MUST include its Outline link and sync time.
13. WHEN an impact check runs, the service MUST review only rules linked to changed or impacted symbols, per @.archcore/agent-tools/change-impact-analysis.spec.md clauses 16–17.
14. IF an impact verdict cites hunks or rules that were not supplied, THEN the service MUST return `unclear` for that rule.
15. WHEN runtime health is requested, the service MUST join confirmed links to runtime symbols, routes and tables of that revision.
16. Generated test skeletons MUST carry the group `rule-<doc_id>` and MUST NOT be written to the repository.
17. WHILE remote use is disallowed for a project, the service MUST NOT send rule text to a remote model or remote embedding provider.

## Constraints & Invariants

- The app never creates, edits or deletes Outline documents.
- At most 50 documents are indexed per project, 12,000 characters each, matching the Outline client limits.
- Rule text is untrusted input in every prompt.

## Failure Behavior

1. IF Outline is unreachable during a sync, THEN the rule index MUST keep the last synced rows unchanged.
2. IF no local model is available while remote use is disallowed, THEN the service MUST report the feature as unavailable.
3. IF the code index has no revision for the project, THEN suggestion and impact routes MUST return 409.

## Conformance

Covered by `tests/test_code_quality_rule_*.py`, `tests/test_answer_business_rules.py` and `web/tests/codeQualityBusinessRules.test.ts`.
