You are working inside an existing Python-based code RAG orchestrator project with these available files:

- orchestrator.py
- router.py

Goal:
Determine whether the final LLM response is being influenced by the graph-backed retrieval layer, without changing the public HTTP contract, job schema, or existing orchestration behavior.

Important constraints:
- Do NOT propose a single hardcoded answer or assume a specific graph influence mechanism.
- Do NOT break backward compatibility.
- Do NOT change existing request/response formats.
- Do NOT rely on the LLM to self-report whether it used graph context.
- Keep changes minimal and localized.
- Preserve fallback behavior if any tracing or retrieval inspection fails.

What you need to do:
1. Inspect the current flow from routing to retrieval to synthesis.
2. Identify where observability can be added so that graph-derived context and non-graph context can be distinguished.
3. Design a way to compare runs with graph-related retrieval enabled versus runs where only non-graph retrieval is used.
4. Add whatever lightweight tracing or logging is necessary to make this comparison possible.
5. Make sure the trace can be reviewed later from the existing job artifacts or logs.
6. Provide a clear report explaining:
   - where the graph can affect the final answer,
   - how to detect that influence,
   - how to verify it experimentally,
   - and what edge cases could make the result ambiguous.

What to return:
- A concise implementation plan
- The minimal code changes needed
- A validation strategy
- Any risks or limitations in measuring graph influence

Do not directly solve the problem in the response itself; instead, produce a practical approach that can be implemented in this codebase.
