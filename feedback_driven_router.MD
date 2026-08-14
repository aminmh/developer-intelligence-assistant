**Agent Task: Implement Feedback-Driven Semantic Router with Hybrid LLM Fallback**

**Context:** This is a LlamaIndex + Qdrant semantic routing system. Some queries get misclassified. Goal: add a feedback loop so corrections improve future routing, plus a hybrid fallback to LLM-based selection for low-confidence cases.

**Step 1 — Discover current implementation**
Search the codebase and report back:
1. Route definitions (file/location, how routes + their example utterances are structured)
2. Qdrant collection schema — vector size, embedding model used, existing payload/metadata fields
3. Embedding pipeline — where text is embedded before upsert/query
4. Current routing/query logic — how similarity threshold is applied, where the top-match decision is made
5. Any existing logging/feedback storage (DB tables, files) that could hold corrections
6. Whether an LLM client (Ollama, etc.) is already wired up anywhere in this flow

**Step 2 — Implement Feedback Loop**
1. Add a `submit_feedback(query: str, correct_route: str)` function/endpoint that:
   - Embeds `query` using the existing embedding pipeline
   - Upserts into Qdrant with payload `{route: correct_route, source: "feedback", original_query: query, timestamp: now}`
2. Modify the routing query logic:
   - If a feedback-tagged point matches with cosine similarity > 0.95, force-select that route (override normal top-k voting)
   - Otherwise, proceed with normal embedding-based routing

**Step 3 — Implement Hybrid Fallback**
1. Define a confidence threshold (e.g. top similarity < 0.75, or top-2 routes within small margin of each other = ambiguous)
2. When ambiguous, call an LLM-based selector (use whatever LLM client already exists — Ollama, etc.) with:
   - The query
   - List of route names + their descriptions
   - Ask it to pick the single best route (structured output — force a valid route name)
3. Log every fallback invocation (query, embedding result, LLM result, final choice) for later analysis

**Step 4 — Testing**
- Write unit tests for: feedback upsert, override triggering at >0.95, fallback triggering below threshold, LLM selector returning a valid route
- Do NOT break existing routing behavior for high-confidence, non-ambiguous queries

**Constraints:**
- Keep embedding-based routing as the default fast path
- LLM fallback should only fire on ambiguous cases
- Match existing code style and conventions in the repo
- Ask before adding new dependencies