ROLE: You are a coding agent working in an existing LlamaIndex + Qdrant + Ollama RAG application. Implement a Ragas-based evaluation system. Follow repo conventions. Ask before adding new dependencies beyond ragas/datasets/langchain-community.

## 1. DISCOVERY (do this first, report back before coding)
- Locate the existing LlamaIndex query engine / index construction code.
- Locate Qdrant collection config: name, vector size, embedding model used.
- Locate the Ollama client setup (model names currently in use for generation).
- Check if any evaluation/test data already exists (questions, ground truths, fixtures).
- Identify where config/env values live (for judge model name, thresholds, etc).
- Identify test framework and CI setup already in the repo.

## 2. INSTALL & CONFIG
- Add `ragas`, `datasets`, `langchain-community` to the appropriate dependency file (ask before modifying if ambiguous).
- Add config for:
  - `RAGAS_JUDGE_LLM_MODEL` (default: whatever chat model is already used, e.g. llama3.1)
  - `RAGAS_JUDGE_EMBED_MODEL` (default: nomic-embed-text)
  - Path to regression eval set file (JSON/YAML — questions + ground_truths)

## 3. CORE EVAL MODULE
Create an eval module (e.g. `evaluation/ragas_eval.py`) that:
- Wraps Ollama chat + embeddings via `LangchainLLMWrapper` / `LangchainEmbeddingsWrapper` for use as judge.
- Exposes a function `run_evaluation(questions, ground_truths, query_engine=None) -> pandas.DataFrame`
  using `ragas.integrations.llama_index.evaluate` with metrics:
  `faithfulness, answer_relevancy, context_precision, context_recall`.
- Returns/saves both the aggregate scores and the per-question dataframe (CSV or JSON) to a results directory with a timestamp.
- Defaults `query_engine` to the app's existing production query engine if not passed.

## 4. REGRESSION EVAL SET
- Create a fixture file with 20–50 representative Q&A pairs (question + ground_truth) drawn from real usage/docs, format matching whatever the discovery step reveals is easiest to load (JSON preferred unless repo convention differs).
- Add a CLI entrypoint or Makefile target (`make eval` or similar) that runs `run_evaluation` against this fixture set and prints/saves the aggregate scores.

## 5. THRESHOLD-BINNED EVAL (router integration)
Only implement if the semantic router + feedback system (from prior work) is present in the repo:
- Add a function `run_binned_evaluation()` that:
  - Runs the regression set through the router, capturing each query's top similarity score.
  - Bins results into `>0.95`, `0.75–0.95`, `<0.75`.
  - Runs Ragas metrics per bin.
  - Outputs a comparison table so thresholds can be tuned from real data instead of assumption.

## 6. BEFORE/AFTER FEEDBACK COMPARISON
- Add a function `compare_before_after_feedback(fixture_path, feedback_batch)` that:
  - Runs eval, applies a batch of feedback corrections (using existing `submit_feedback`), re-runs eval on the same fixture set, and diffs context_precision/context_recall.
  - Output should clearly show delta per metric, not just two separate reports.

## 7. TEST-SET GENERATION (optional utility, not part of CI)
- Add a standalone script `scripts/generate_testset.py` using `ragas.testset.TestsetGenerator.from_langchain(...)` with the local judge LLM/embeddings, pointed at the app's existing document source.
- Output should be saved in the same format the regression fixture uses, NOT auto-merged into the regression set — require manual review before promotion.

## 8. TESTS
- Unit test that `run_evaluation` returns expected dataframe columns given a mocked query engine and small fixture.
- Unit test that binned evaluation correctly buckets by similarity score.
- Unit test that before/after comparison computes correct deltas.
- Mock/skip actual Ollama calls in CI (use a fake LLM/embeddings wrapper) unless the repo already has a pattern for live-model integration tests.

## 9. CONSTRAINTS
- Do not change production routing/generation behavior — this is an evaluation layer only.
- Keep judge model configurable, not hardcoded.
- Flag clearly in output/docs that a weak local judge model produces noisier scores than GPT-4-class judges — scores should be sanity-checked, not treated as ground truth.
- Ask before adding CI jobs; propose the job config but don't wire it into the pipeline without confirmation.

## OUTPUT AT END
Summarize: files created/modified, how to run the eval locally, current baseline scores (if the fixture set exists), and any open questions from discovery that need my input (e.g. missing ground truths, ambiguous query engine entrypoint).
