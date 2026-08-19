You are a coding agent with filesystem access to the SkiSchool project (Laravel + decoupled Python RAG service using LlamaIndex, Qdrant, Ollama, and a Semantic Router).

GOAL: Add a query normalization step that is applied identically at (a) route-example indexing time and (b) incoming-query routing time, to reduce spurious mismatches caused by surface-level text variation and domain jargon.

## Phase 1 — Discovery (do this first, report back before writing code)
1. Locate where route/intent seed examples are embedded and upserted into Qdrant.
2. Locate where incoming user queries are embedded before being routed (the live query path).
3. Confirm whether these two paths currently share any preprocessing function, or diverge.
4. Identify the embedding model/client in use (Ollama model name, LlamaIndex embedding wrapper, etc.) and note whether it's case-sensitive or punctuation-sensitive (check existing tests/docs if any).
5. Check for any existing text-cleaning utilities in the codebase (Laravel or Python) that might already do partial normalization — do not duplicate them.
6. Identify where SkiSchool-specific domain terms/abbreviations would naturally live (e.g., a config file, constants module) — check if one already exists.

Report findings before proceeding to Phase 2.

## Phase 2 — Implementation
1. Create a single shared function, e.g. `normalize_query(text: str) -> str`, in a location consistent with the project's existing module structure (ask if unclear rather than guessing a new location).
2. Implement, in this order:
   - Unicode normalization (NFKC).
   - Case folding (lowercase), unless project conventions indicate case matters for routing — verify via discovery, don't assume.
   - Whitespace/punctuation cleanup (collapse repeated whitespace, strip leading/trailing punctuation; preserve `?`/`!` unless testing shows the embedding model is punctuation-sensitive in a way that hurts routing).
   - Domain jargon expansion via a dict-based lookup (external config/JSON file, not hardcoded inline) for SkiSchool-specific abbreviations and shorthand. Seed this dict with placeholder examples and ask the user to supply the real term list — do not invent domain terminology.
3. Do NOT add stemming, lemmatization, or stopword removal — these are intentionally excluded as they degrade sentence-transformer-style embeddings.
4. Wire `normalize_query()` into BOTH the route-example indexing path and the live query-routing path. This is the critical correctness requirement — flag prominently in your summary if either path cannot be modified without touching unrelated logic, and ask before proceeding.
5. Do NOT add a typo-correction/spellcheck step in this pass — leave a clear TODO comment noting it was considered but deferred pending evidence of need.

## Constraints
- Preserve existing code style, project structure, and dependency choices.
- Do not add new dependencies without asking first (e.g., if Unicode/whitespace handling requires a library not already present).
- Do not change embedding model, Qdrant schema, or routing/threshold logic — this task is scoped to text normalization only.
- Do not modify or regenerate existing Qdrant-stored route embeddings as a side effect; if re-indexing existing examples through the new normalization is needed, surface this as an explicit follow-up step requiring approval, not an automatic action.

## Testing
- Add unit tests for `normalize_query()` covering: unicode variants, mixed case, extra whitespace/punctuation, and at least 2-3 domain jargon expansions (using placeholder terms if the real dict isn't supplied yet).
- Add a test (or test hook) confirming the same normalization function is invoked in both the indexing path and the query path (e.g., via a shared import, not duplicated logic).
- Do not run this against the live regression/eval set automatically — leave that as a manual before/after comparison step for the user.

## Output
Summarize:
- Where `normalize_query()` was placed and why.
- The two call sites wired in.
- Any cases where case-sensitivity or punctuation-sensitivity assumptions need user confirmation.
- The jargon dict file location and format, with a note that real terms are pending user input.
