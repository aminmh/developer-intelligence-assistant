---
title: "Deterministic Clean Code and SOLID Assessment from Indexed Chunks"
status: draft
tags:
  - "api"
  - "code-quality"
  - "component:code-quality"
---

## Purpose & Scope

This specification makes the official Clean Code category score a versioned, reproducible function of metrics computed from one immutable revision's indexed code chunks. The model never produces a numeric Clean Code score. It only returns a closed verdict (`violation`, `acceptable`, `uncertain`) for a fixed, ranked set of candidate design violations, and those verdicts are cached by content. Assessing the same revision twice must yield an identical Clean Code result. Re-assessing a later revision must reuse verdicts for unchanged code.

In scope: PHP chunk metrics, Qdrant payload indexes, the metrics stage, candidate selection, compact candidate context, model-as-judge verdicts and their cache, Clean Code scoring, and report provenance. The metric format is language-neutral; PHP is the first producer.

Out of scope: Security, Maintainability, Reliability, and Performance keep their current evaluator flow until each gets its own metrics stage. Business-rule review keeps vector retrieval.

This document amends @.archcore/code-quality/codebase-quality-assessment.spec.md for the `clean_code` category only: normative rules 7, 22, and 92–95 of that spec no longer govern the official Clean Code score when this stage produces it.

## Surface

- PHP chunk metrics: @api/Python-Service/php/src/CodeParser/Models/ChunkCollector.php and @api/Python-Service/php/src/CodeParser/Models/CodeChunk.php.
- Indexed payload and payload indexes: @api/Python-Service/app/index/llamaindex_layer.py.
- Metrics stage, rubric, candidate selection, class cards: @api/Python-Service/app/code_quality/design_metrics.py and @api/Python-Service/app/code_quality/rubrics/clean-code-rubric-2.yml (rubric 1 kept for earlier reports).
- Model judge and verdict cache: @api/Python-Service/app/code_quality/design_judge.py.
- Scoring integration and report persistence: @api/Python-Service/app/code_quality/service.py.
- Report display and comparison equivalence: @web/src/components/CodeQualityView.vue.

## Definitions

- **Unit**: one indexed class, trait, or enum, together with its method chunks (methods are grouped by `parent_symbol`). Interfaces are units for ISP only.
- **Metric schema `code-metrics-1`**: the per-chunk payload fields listed in rules 1–2 and their exact computation.
- **Rubric**: a versioned file (`clean-code-rubric-2`) holding every threshold, penalty, principle mapping, candidate limit, and context limit. Code holds no thresholds.
- **Rule**: one rubric entry. A rule is `judge: none` (penalty applies when the threshold is exceeded) or `judge: model` (penalty applies only on a `violation` verdict).
- **Candidate**: one (unit, rule) pair exceeding a `judge: model` rule's threshold and selected for judgment.
- **Class card**: the bounded, deterministic context sent to the model for one candidate.
- **Card hash**: SHA-256 over the canonical JSON of a class card. It changes when the unit's code, the code of its related chunks, or its triggering metrics change.

## Normative Behavior

### Indexing

1. WHEN the PHP parser emits a method or function chunk, it MUST include `content_hash` (SHA-256 of the chunk code with line endings normalized to `\n`), `cyclomatic` (1 plus each `if`, `elseif`, `for`, `foreach`, `while`, `do`, `case`, `catch`, ternary, `??`, `&&`, `||`, `and`, `or`, and non-default `match` arm), `max_nesting` (deepest nesting of control-flow blocks), `param_count`, `reads_properties`, `writes_properties`, `type_checks` (count of `instanceof` expressions), `instantiates` (resolved class names in `new` expressions), and `throws` (resolved class names in `throw new` expressions).
2. WHEN the PHP parser emits a class, trait, enum, or interface chunk, it MUST include `content_hash` and `constructor_dependencies` (each constructor parameter's name and resolved type).
3. WHEN chunk metrics are emitted, every chunk carrying them MUST also carry `metric_schema: "code-metrics-1"`.
4. WHEN the indexer writes chunk payloads, it MUST persist every metric field from rules 1–3 unchanged.
5. WHEN the indexer creates or reuses a revision collection, it MUST ensure keyword payload indexes on `revision_id`, `node_type`, and `file_path`; creating an index that already exists MUST NOT fail indexing.

### Metrics stage

6. WHEN Clean Code is assessed, the metrics stage MUST read the revision's chunks by payload filter (project, revision, commit, archive checksum, collection, not dirty) without vector search.
7. WHEN the metrics stage reads chunks, it MUST use only chunks whose file is an inventoried, non-excluded business source file.
8. WHEN the metrics stage computes unit metrics, it MUST compute: LCOM4 (connected components among non-constructor methods, linking methods that share a read or written property or call one another through `$this`/`self`/`static`), fan-out (distinct non-self class types across calls, instantiations, and constructor dependencies), method count, public interface size, type-switch count (maximum `type_checks` in one method), concrete constructor dependencies (constructor types that resolve to an indexed concrete class rather than an interface or abstract class), unsupported overrides (methods implementing an indexed interface method or overriding an indexed parent method whose `throws` include an unsupported-operation exception type), and unit LOC.
9. WHEN the metrics stage computes method metrics, it MUST take cyclomatic complexity, nesting, parameter count, and LOC from the chunk payload.
10. WHEN the metrics stage loads the rubric, it MUST load thresholds, penalties, and limits only from the versioned rubric file.

### Candidate selection

11. WHEN selecting candidates, the service MUST rank the exceeding units of all `judge: model` rules together by severity (excess ratio times the rule's penalty), descending, then by file path, then by start line, then by symbol name, then by rule.
12. WHEN selecting candidates, the service MUST first give each principle up to the rubric's per-principle minimum (default 3), then add candidates in severity order while the judge's estimated input stays within the rubric's token budget (default 80,000) and the candidate maximum (default 150).
13. WHEN a candidate needs related code, the service MUST fetch it by symbol identity from the same revision's chunks, never by vector search.
14. WHEN an exceeding unit is not selected, the report MUST count it as unjudged for its principle.

### Class card

15. WHEN the service builds a class card, it MUST include the unit's signature skeleton (declaration line and method signatures), properties, constructor dependencies, the metric values that triggered the rule with their thresholds, and the body of the flagged method only.
16. WHEN a rule concerns a contract (ISP, LSP, DIP), the class card MUST include the signatures of the related interface, parent, or dependency type when indexed.
17. WHEN a class card includes a method body, it MUST cap that body at the rubric's body limit (default 1,400 characters) and mark the truncation.

### Model as judge

18. WHEN the service requests a verdict, it MUST ask one closed question tied to the candidate's principle and rule.
19. WHEN the service requests verdicts, it MUST request provider-constrained JSON with entries `{evidence_id, principle, verdict, reason}`, where `verdict` is `violation`, `acceptable`, or `uncertain` and `reason` is at most 240 characters; the schema MUST NOT contain any score.
20. WHEN the service requests verdicts, it MUST use temperature 0 and top_p 1.
21. WHEN a verdict cache entry exists for `(project_id, card_hash, principle, rule_id, rubric_version, prompt_hash, model)`, the service MUST reuse it and MUST NOT call the model for that candidate.
22. WHEN the model returns a valid verdict for a supplied candidate, the service MUST store it in the verdict cache under the key from rule 21.
23. WHEN candidates exceed one request's token budget, the service MUST split them into deterministic, order-preserving batches.
24. WHEN Clean Code is assessed through this stage, the service MUST give the evaluator the stage's result as evidence (clause 28) and MUST request the Clean Code score from it (amended 2026-11-19).

### Scoring

25. WHEN a unit is scored, the service MUST start it at 100, subtract each `judge: none` rule's penalty when exceeded, subtract each `judge: model` rule's penalty when its verdict is `violation`, and floor the result at 0.
26. WHEN a verdict is `uncertain`, missing, or invalid, the service MUST apply no penalty and MUST lower category confidence.
26a. WHEN an exceeding unit of a `judge: model` rule was not judged, the service MUST subtract the rule's penalty times the share of decisive verdicts that were violations for that rule (else its principle, else the rubric's default rate), mark the unit's estimated rules, and record the rates in the report.
27. WHEN the Clean Code score is calculated, the service MUST use the LOC-weighted mean of unit scores, rounded to the nearest integer.
28. WHEN this stage produces a score, the report MUST keep it as `clean_code.design_metric_score` and pass it to the scoring model as a summary finding; the official Clean Code score comes from the scoring model with `clean_code.score_method` `model-evaluator-with-design-metrics` (amended 2026-11-19).
28a. WHEN an SRP candidate's card is built, it MUST list the class's disconnected method groups with the properties each uses and a bounded code excerpt of each group's largest method.
29. WHEN the evaluator returns Clean Code narrative, the report MAY show it as explanation only; it MUST NOT change the score.

### Report

30. WHEN an assessment completes, the report MUST persist the metric schema version, rubric version and content hash, thresholds and penalties, the candidate list in selection order, each verdict with its cache-hit flag, per-principle counts (exceeding, judged, violations, acceptable, uncertain, unjudged), the number of metric-bearing units and their LOC.
31. WHEN an assessment completes, the report MUST persist a `clean_code_hash`: SHA-256 over the canonical JSON of the metric versions, candidates, verdicts, unit scores, and Clean Code score, excluding timestamps, report identifiers, token usage, and cache-hit flags.
32. WHEN reports differ in Clean Code `score_method`, metric schema, or rubric version, the comparison view MUST mark score deltas as non-equivalent; reports without a `score_method` MUST be treated as non-comparable with `design-metrics-1` reports.
33. WHEN Clean Code observations are reported, each MUST cite a candidate evidence identifier and inherit its file and line.

## Constraints & Invariants

- Assessing the same revision with the same rubric, prompt, and model produces the same candidates, candidate order, evidence identifiers, and `clean_code_hash`; a second run issues no Clean Code model calls.
- Evidence identifiers derive from project, revision, rubric version, rule, and symbol identity, never from list position or wall-clock time.
- Duplicating a unit whose score is at least the current Clean Code score never lowers the Clean Code score.
- No candidate is silently dropped: every exceeding unit is judged, counted as unjudged, or recorded `uncertain` with a reason.
- The model judges; the application selects candidates, applies penalties, and computes every number.
- Verdict cache entries are scoped by project and store verdicts and reasons only, never source text.
- Reports persist card hashes and locations, not source snippets.

## Failure Behavior

1. IF the revision has no chunks carrying `metric_schema: "code-metrics-1"`, THEN the report MUST mark the design-metrics stage unavailable with a reindex instruction, and Clean Code MUST use the prior evaluator path with `score_method` set to `model-evaluator-legacy`.
2. IF no inventoried language has a metric producer, THEN SOLID principles MUST be reported unavailable and Clean Code MUST follow failure rule 1.
3. IF the rubric file is missing or invalid, THEN the assessment MUST fail with a retryable error and save no report.
4. IF the model call fails or returns invalid JSON for a batch, THEN that batch's candidates MUST be recorded `uncertain` with the failure reason and left uncached, and the score MUST still be calculated.
5. IF a returned verdict cites an unsupplied evidence identifier or a mismatched principle, THEN the service MUST discard it and record that candidate `uncertain`.
6. IF a single class card exceeds the request budget after body truncation, THEN the candidate MUST be recorded `uncertain` with reason `context_limit`.
7. IF chunk reading exceeds the chunk limit, THEN the stage MUST mark itself partial and report the limit.

## Conformance

An implementation conforms when PHP chunks carry `code-metrics-1` metrics, payload indexes exist, Clean Code metrics and candidates come from filtered chunk reads, the model returns only cached closed verdicts, the Clean Code score is computed in code from the versioned rubric, reruns produce an identical `clean_code_hash` with full cache hits, and reports persist the provenance above.
