## Coding-Agent Prompt — Add Misspelling Correction to `normalize_query`

**Context**: Extend the existing `normalize_query(text: str)` function (NFKC + case fold + whitespace/punctuation cleanup + jargon expansion) with a **domain-constrained typo correction** step. Do not use general-purpose spellcheckers (e.g. `pyspellchecker` with English dictionaries) — they will "correct" valid domain terms (instructor names, ski jargon, SKU codes) into wrong words.

### Requirements

1. **Library**: Use `symspellpy` (pure Python, fast, edit-distance based). Add as dependency only if approved.

2. **Domain dictionary**: Build a frequency dictionary from:
   - Known vocabulary already used for jargon expansion (reuse that list).
   - Distinct terms extracted from the indexed corpus (course names, instructor names, equipment terms) — generate this file offline, not at runtime.
   - Do **not** use a generic English word list as the base dictionary; it must be domain-only, so out-of-vocabulary correction stays conservative.

3. **Correction scope**:
   - Only apply Symspell lookup to **tokens not already in the domain dictionary** (skip known-good words entirely — untouched pass-through).
   - Max edit distance = 2.
   - Only accept a correction if it's the **single best match with count above a minimum frequency threshold** (avoid ambiguous multi-candidate corrections silently picking one).
   - If no confident correction found, leave the token untouched (never guess).

4. **Placement in pipeline**:
      NFKC → case fold → whitespace/punct cleanup → jargon expansion → typo correction → final normalized string
   ```
   Typo correction goes **last**, after jargon expansion, so corrected tokens can still match expanded domain terms.

5. **Logging**: Every correction applied (`original_token → corrected_token`) must be logged at `debug` level, tagged with the query id, for manual audit before enabling in production.

6. **Feature flag**: Wrap the correction step behind a config flag `ENABLE_TYPO_CORRECTION` (default `false`). Ship disabled first, review logs on a sample of live queries, then enable.

7. **Testing**:
   - Unit tests with known domain terms that must **not** be altered (e.g. instructor names, jargon after expansion).
   - Unit tests with deliberate typos of domain terms that **must** be corrected.
   - Regression test: run the full eval set (Ragas) with the flag on vs off, confirm no drop in `context_precision`/`context_recall`.

### Out of scope
- No correction of multi-word phrases or n-gram context correction (single-token only, this phase).
- No online/adaptive dictionary updates from live queries (future enhancement, same feedback-loop pattern as router).