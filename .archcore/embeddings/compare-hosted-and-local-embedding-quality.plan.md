---
title: "Measure the Quality Gap Between Hosted and Local Embeddings"
status: draft
tags:
  - "api"
  - "embeddings"
---

## Goal

Measure the difference between the hosted embedding setup used in production and local Ollama BGE-M3 on the same source revisions and questions. Report where local retrieval and answers are better, equivalent within measured uncertainty, or worse. This plan collects evidence for choosing a default; it does not change providers or product behavior.

## Tasks

### 1. Freeze the comparison

1. Pin the application build, corpus revisions, embedding profiles, and matched index settings. — @api/Python-Service/app/index/embeddings.py, @api/Python-Service/app/index/llamaindex_layer.py.
2. Label expected source evidence and answer facts before tuning or running either arm. — Seed from @api/Python-Service/scripts/agent_eval.py.
3. Build separate complete vector indexes over identical chunks and verify coverage and revision identity. — @api/Python-Service/app/index/llamaindex_layer.py.

- Primary comparison: the actual deployed hosted provider and models versus local Ollama `bge-m3`. If hosted is Cohere, pin the app's Pro document and Fast query models and record both. Record provider, model version or digest where available, document and query preprocessing, truncation, dimensions, and per-arm thresholds. Tune any thresholds only on a separate development set; freeze them before testing.
- Optional diagnostic: compare hosted BGE-M3 against local BGE-M3 to separate the model choice from the serving path. Confirm equivalent model weights and input conventions where possible; disclose unknowns. Use a full index and matching query model for each arm. Do not search one arm's index with the other arm's vectors.
- Target at least 60 held-out questions across at least three representative projects or revisions; use multiple languages where available. Include exact symbols, paraphrased concepts, cross-file flows, business rules, and unanswerable questions. Mark critical cases up front. Label the expected revision, relevant file and line spans, required facts, and when to abstain. Fix the question mix before running.

### 2. Run paired comparisons

1. Replay held-out queries through both retrievers with fixed filters, ranking settings, and result limits. — @api/Python-Service/app/index/llamaindex_layer.py.
2. Generate answers from both results using the same chat model, prompt, tools, and budgets. — Adapt @api/Python-Service/scripts/agent_eval.py.
3. Blind-score evidence, supported claims, and abstention; log operational metrics separately. — Adapt @api/Python-Service/scripts/agent_eval.py.

- Retrieval: for answerable questions, score evidence recall at 5 and 10 and first relevant result rank; a fully retrieved cross-file question must cover all required evidence spans. Inspect vector candidates and final results so graph expansion does not hide a retrieval change. Keep graph snapshot, revision filters, context budget, and top-k fixed.
- Answers: pin the route for each paired comparison, hold tool availability fixed, and disable response caches between arms. Repeat generation three times when the model is nondeterministic; average by question before computing uncertainty. Blind and randomize answer order. Score required facts, whether each citation actually supports the claim, unsupported claims, and appropriate abstention. A real file and line number alone is not a correct citation.
- Operations: record indexing time, queries per second where applicable, p50/p95 query latency, peak local RAM, vector disk, hosted request cost and retries/429s, and chat tokens. Keep quota and transport failures in a separate availability tally; report all attempted and completed pairs so missing results are visible.

### 3. Quantify the gap

1. Calculate paired local-minus-hosted deltas and 95% confidence intervals for primary metrics. — Extend the result format of @api/Python-Service/scripts/agent_eval.py.
2. Summarize misses by question type and project; document critical regressions and their causes. — Use citations and traces from @api/Python-Service/app/index/llamaindex_layer.py.
3. Compare measured gaps with the agreed quality margin and report whether the evidence supports switching. — No existing report target identified.

Report absolute scores, percentage-point deltas for rates, and sample counts overall and by question type. Use a paired bootstrap by question stratified by corpus and question type; keep repeats grouped with their question and show per-project results. Flag sparse slices and intervals that do not support a conclusion. For a potential default switch, agree the tolerance before seeing results (proposal: no more than 3 percentage points worse on evidence recall@10, required-fact coverage, or citation support; no new misses in critical cases). If the confidence interval crosses that margin, call the evidence inconclusive and expand the evaluation.

## Acceptance Criteria

- The benchmark isolates the embedding configuration using identical source snapshots, chunks, graph, paired queries, and answer settings. Each arm has its own compatible, complete index.
- A report quantifies retrieval and answer quality differences with uncertainty, shows critical failures and worked examples, and separates availability and cost from quality.
- The report distinguishes an inconclusive result from measured parity and says which use cases, if any, need the hosted option.
- The benchmark runs with approved test repositories and hosted access. No production configuration or active index is changed.

## Dependencies

- Local Ollama with pinned BGE-M3, verified hosted embedding credentials and quota, the same chat model for both arms, and a staging environment with separate collections.
- Project authorization before source or questions go to hosted providers. Preserve the remote-model consent check if the chat model is hosted.
- The existing evaluation harness compares tools on/off. Keep its fixtures and fact scoring where useful; add paired embedding arms and independently checked evidence and citation labels.
- Preserve revision identity and embedding profile isolation from @.archcore/knowledge-base/sync-creates-new-revisions-and-reuses-unchanged-vectors.adr.md.

## Declared Delta

- `add` — Benchmark protocol for quantifying local versus hosted embedding quality.
- `preserve` — Existing retrieval, provider selection, and production indexes while the benchmark gathers evidence.
