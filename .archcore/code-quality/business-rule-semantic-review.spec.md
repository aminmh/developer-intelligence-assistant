---
title: "Evidence-Grounded Business Rule Review"
status: draft
tags:
  - "api"
  - "code-quality"
  - "frontend"
---

## Purpose & Scope

Defines Outline-backed plain-language business rules and their evidence-grounded review during Code Quality assessment. Outline is the canonical authoring source; the application reads rules from a project-mapped collection and snapshots them into assessments. The evaluator compares each explicit rule with bounded evidence from one immutable source revision. It does not invent requirements or treat missing evidence as failure or proof of compliance.

## Surface

- Outline connection, read-only synchronization, and assessment evidence: @api/Python-Service/app/code_quality/.
- Outline connection policy, collection mapping, and report snapshots: @api/Python-Service/app/code_quality/http.py and @web/src/server/codeQualityRoutes.ts.
- Outline connection and collection selection, plus review results: @web/src/components/CodeQualityView.vue and @web/src/components/code-quality/.
- Revision-scoped semantic retrieval and call tracing: @api/Python-Service/app/code_quality/semantic_context.py and @api/Python-Service/app/code_quality/flow_tracing.py.

## Normative Behavior

1. WHEN an administrator connects Outline, the API MUST scope the connection and collection mapping to an authorized project.
2. WHEN a user configures Outline, the API MUST require an instance URL and one business-rule collection identifier.
3. WHEN the credential manager stores an Outline API token, it MUST use the OS credential store.
4. WHEN the connector synchronizes rules, it MUST read documents only from the configured collection.
5. WHEN the connector imports a rule, it MUST require one nonempty document title and body.
6. WHEN the connector reads Outline documents, it MUST honor access granted to the token's Outline identity.
7. WHEN a user opens rule management, the Code Quality view MUST provide connection status and a collection-selection flow instead of local rule editing.
8. WHEN an assessment starts, the connector MUST refresh the mapped collection before selecting its rules.
9. WHEN the connector selects rules, it MUST order documents by stable document ID before applying assessment limits.
10. WHEN the service snapshots a rule, it MUST retain document ID, title, body, collection ID, update time, and content hash.
11. WHEN the evaluator reviews a rule, the service MUST retrieve code evidence only from the assessed immutable revision and effective source inventory.
12. WHEN the evaluator reviews a rule, it MUST receive the rule statement, bounded relevant excerpts, analyzer findings, and applicable flow traces.
13. WHEN the evaluator returns a rule result, it MUST classify it as supported, concern, or unknown and cite supplied evidence identifiers.
14. WHEN a rule result cites evidence, the report MUST retain source paths, line ranges, evidence IDs, and Outline document identity.
15. WHEN evidence is incomplete, unsupported, or absent, the evaluator MUST return unknown rather than infer compliance or violation.
16. WHEN a rule has no relevant evidence, the report MUST explain the retrieval or analyzer coverage gap.
17. WHEN an assessment report is saved, the report store MUST retain the exact rule snapshot, result, rationale, and citations without full code snippets.
18. WHEN a user opens an assessment report, the Code Quality view MUST show rule results separately from category scores.
19. WHEN an Outline document changes after an assessment, the saved report MUST retain the document version and content used by that assessment.
20. WHEN rule-derived model context exceeds the configured evidence budget, the service MUST apply deterministic limits and report omitted rules or evidence.
21. WHEN an assessment evaluates business rules, the service MUST not execute project commands, tests, or arbitrary tools.
22. WHEN a collection contains more than 50 rule documents, the connector MUST report the excess and use the first 50 in stable document-ID order.
23. WHEN more than 20 rules are selected for an assessment, the evaluator MUST mark rules beyond the deterministic review limit unknown.
24. WHEN a user starts the local Outline profile, Compose MUST run Outline, PostgreSQL, and Redis as separate services.
25. WHEN the standard development stack starts, its task MUST leave Outline services stopped unless users explicitly start the Outline profile.
26. WHEN local Outline publishes a host port, Compose MUST bind the port to loopback by default.
27. WHEN Outline stores database or document files, Compose MUST preserve them in named volumes across container replacement.
28. WHEN users stop the Outline profile, its lifecycle command MUST preserve persistent volumes.

## Constraints & Invariants

- Outline is the canonical business-rule authoring source; Code Quality does not create, edit, or delete Outline documents.
- Each configured collection is dedicated to one project's business rules, with one document representing one rule.
- Plain-language business rules remain distinct from executable Semgrep rules.
- The evaluator may reason only from evidence IDs and bounded context supplied by the service.
- A rule result is evidence-based guidance, not proof of whole-program behavior.
- Reports retain rule snapshots, citations, and summaries, not full source snippets or credentials.
- Rule order, evidence order, and truncation are deterministic for identical project and revision inputs.
- The connector reads at most 50 rule documents per project; each assessment reviews at most 20.
- The connector orders documents by stable Outline document ID before applying assessment limits.
- Existing reports remain readable after local rule editing is removed.

## Failure Behavior

1. IF Outline is not configured for a project, THEN assessment behavior MUST preserve category scoring and report that rule review is not configured.
2. IF Outline cannot be reached before assessment, THEN the service MUST preserve analyzer scoring and mark rule reviews unavailable without using stale rules.
3. IF the selected revision has no ready matching index, THEN rule review MUST report unknown with the revision-index reason.
4. IF model output omits a rule, cites unknown evidence, or violates the result schema, THEN the service MUST mark that rule unknown.
5. IF evidence retrieval fails, THEN the service MUST preserve ordinary analyzer scoring and mark affected rule reviews unknown.
6. IF Outline rejects the token or collection access, THEN the connector MUST show an actionable error without disclosing the token.
7. IF project authorization fails, THEN the API MUST reject Outline configuration and rule reads without disclosing project data.

## Conformance

A conforming implementation reads project-mapped business rules from Outline without writing to Outline, protects credentials, snapshots exact rule content into reports, and performs bounded revision-isolated evidence review with explicit unknown states.