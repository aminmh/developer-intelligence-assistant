---
title: "Implement Deterministic Clean Code and SOLID Assessment"
status: draft
tags:
  - "api"
  - "code-quality"
  - "component:code-quality"
---

## Goal

Implement @.archcore/code-quality/deterministic-clean-code-assessment.spec.md: compute the official Clean Code score in code from versioned metrics over a revision's indexed chunks, and use the model only as a cached yes/no/uncertain judge on a fixed, ranked candidate set.

## Decisions

- The Clean Code score is computed in code (`score_method: design-metrics-1`); the model never returns a numeric Clean Code score.
- Thresholds and penalties live in one versioned rubric file, not in code.
- Candidates are ranked by excess ratio with path/line/symbol tie-breaks; vector search is no longer used for Clean Code.
- Verdicts are cached in the existing code-quality SQLite store, keyed by `(project_id, card_hash, principle, rule_id, rubric_version, prompt_hash, model)`.
- `uncertain` costs no points but lowers confidence.
- Unit scores are combined by LOC-weighted mean.
- Revisions indexed before `code-metrics-1` fall back to the legacy evaluator path, labelled `model-evaluator-legacy`, until re-indexed.
- Pre-change reports are non-comparable with `design-metrics-1` reports.

## Tasks

### 1. Indexing (PHP parser and payload)

1. Add a metrics visitor in `ChunkCollector.php` computing `cyclomatic`, `max_nesting`, `param_count`, `reads_properties`, `writes_properties`, `type_checks`, `instantiates`, and `throws` for each method and function.
2. Add `content_hash` to every chunk and `constructor_dependencies` to class chunks; add `metric_schema: "code-metrics-1"`.
3. Extend `CodeChunk` and `toMetadata()` with the new fields.
4. Extend `_payload()` in `index/llamaindex_layer.py` to persist the fields unchanged.
5. Ensure keyword payload indexes on `revision_id`, `node_type`, and `file_path` after collection creation, tolerating existing indexes.
6. Add a PHP fixture check that the metrics match hand-computed values.

### 2. Metrics stage

7. Add `rubrics/clean-code-rubric-1.yml` with rules, principles, thresholds, penalties, `judge` mode, candidate limits, and body limit; add a loader that validates it and returns its content hash.
8. Add `design_metrics.py`: filtered chunk read (reuse the identity filter and scroll loop from `flow_tracing.py`), unit grouping, LCOM4, fan-out, interface size, type switches, concrete constructor dependencies, unsupported overrides, and LOC.
9. Replace the method-count and regex checks in `php_design.py` with metric-based rules; keep its archive fallback feeding the same metrics stage.

### 3. Candidates and class cards

10. Rank exceeding units per rule, apply per-principle and total limits, and record unjudged counts.
11. Build class cards with signature skeleton, properties, constructor dependencies, triggering metrics, flagged method body capped at 1,400 characters, and related contract signatures fetched by symbol identity.
12. Derive evidence IDs from project, revision, rubric version, rule, and symbol; derive card hashes from canonical JSON.

### 4. Model as judge

13. Add `design_judge.py` with one closed question per rule, the verdict JSON schema, prompt hashing, deterministic batching by token budget, and validation that discards unsupplied IDs or mismatched principles.
14. Add the verdict cache table to `ReportStore`'s SQLite file and read/write it around model calls.
15. Remove Clean Code evidence from the overflow-summary path and from the evaluator's scoring request; keep evaluator narrative as explanation only.

### 5. Scoring and report

16. Score units from rubric penalties and verdicts; compute the LOC-weighted Clean Code score and confidence.
17. Set `categories.clean_code` from this result with `score_method: design-metrics-1`; keep the overall weighted score unchanged otherwise.
18. Persist metric and rubric versions, rubric hash, thresholds, candidates, verdicts with cache-hit flags, per-principle counts, unit count and LOC, and `clean_code_hash`.
19. Mark Clean Code comparisons non-equivalent in `CodeQualityView.vue` when `score_method`, metric schema, or rubric version differ.

### 6. Documentation

20. Amend @.archcore/code-quality/codebase-quality-assessment.spec.md to reference this spec for the Clean Code score.
21. Record the "Clean Code score computed in code" decision as an ADR and relate it to both specs.

## Test Plan

- **Labelled fixture**: a PHP fixture repo with labelled violations per principle; assert per-rule precision and recall against recorded baselines and fail when any principle drops below its baseline.
- **Determinism**: assess one revision twice with a fake model; assert identical scores, candidate order, evidence IDs, and `clean_code_hash`. The second run makes zero model calls.
- **Revision delta**: change one class; only that class's candidates call the model, and unchanged classes keep cached verdicts.
- **Size normalization**: duplicating clean classes does not lower the score.
- **Budget**: a 25-candidate request fits the configured model limit, and no candidate is dropped silently; oversized cards become `uncertain` with `context_limit`.
- **Failure paths**: missing metric schema falls back to legacy with a reindex reason; invalid rubric fails the assessment; model failure yields `uncertain` without caching.
- **Regression**: the existing `test_code_quality*.py` suites still pass, updated only where Clean Code scoring intentionally changed.

## Acceptance Criteria

- Re-running an assessment on the same revision gives a byte-identical `clean_code_hash` and 100% verdict cache hits.
- The official Clean Code score carries `score_method: design-metrics-1` and is never taken from model output.
- Every exceeding unit is judged, counted unjudged, or recorded `uncertain` with a reason.
- Reports persist full provenance and are marked non-comparable with pre-change reports.
- Other categories behave as before.

## Dependencies

- @.archcore/code-quality/deterministic-clean-code-assessment.spec.md
- @.archcore/code-quality/codebase-quality-assessment.spec.md
- @.archcore/code-quality/semantic-flow-tracing.spec.md (shared revision-scoped chunk reads)
- Re-indexing of revisions to be assessed with `design-metrics-1`.
