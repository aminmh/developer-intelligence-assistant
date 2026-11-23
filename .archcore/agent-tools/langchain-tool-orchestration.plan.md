---
title: "Implement LangChain Tool Orchestration"
status: draft
tags:
  - "api"
  - "frontend"
---

## Goal

Implement the accepted LangChain tool-orchestration contract (@.archcore/agent-tools/langchain-tool-orchestration.spec.md) for the remote OpenAI-compatible model, while LlamaIndex keeps indexing and retrieval. Local models are out of scope (user, 2026-11-10).

## Tasks

### Phase 0 — Decisions (done 2026-11-10)

1. Resume paused runs from a LangGraph checkpoint on a new stream. — @.archcore/agent-tools/paused-agent-runs-resume-from-checkpoint-on-new-stream.adr.md (done: accepted; implemented in Phase 4)
2. Gate remote source access with a per-project consent setting. — @.archcore/agent-tools/remote-model-source-access-requires-project-consent.adr.md (done: accepted; implemented as task 12b)
3. Route MCP egress through an allowlist proxy. — @.archcore/agent-tools/mcp-servers-reach-only-allowlisted-hosts-through-egress-proxy.adr.md (decided: accepted; the proxy itself is task 12a, still open)

### Phase 1 — Dependencies and provider capability

4. Pin LangChain 1.x with `create_agent`, LangGraph, and the MCP adapter package. — @api/Python-Service/requirements.txt (done 2026-11-10: langchain 1.4.9, core 1.7.7, openai adapter 1.1.9, community 0.3.31, langgraph 1.2.20)
5. Add a stub-tool round-trip probe to the remote connection test. — @api/Python-Service/app/runtime/llm.py (`test_remote`) (done: returns `tool_calling`)
6. Store the probe result per remote base URL and model. — @api/Python-Service/app/runtime/tool_calling_probe.py (done: settings key `llmRemoteToolCallingProbes`, not `settings_store.py`)

### Phase 2 — Tool registry and execution

7. Add a built-in tool registry with names, JSON schemas, and effect classes. — @api/Python-Service/app/agent_tools/registry.py (done)
8. Expose only read operations of built-in PHP tools to the model. — @api/Python-Service/app/agent_tools/builtins.py (done: `project_structure`; `chunk_code` stays orchestrator-only)
9. Extend managed tool records with JSON schema, effect class, and host allowlist. — @api/Python-Service/app/infra/tool_store.py (done)
10. Validate tool names against workflow allowlists before every execution. — @api/Python-Service/app/agent_tools/registry.py (done)
11. Validate tool arguments against registered JSON schemas before every execution. — @api/Python-Service/app/agent_tools/validation.py (done)
12. Implement MCP schema discovery and `tools/call` invocation. — @api/Python-Service/app/infra/tool_executor.py (deferred 2026-11-11 by the user: MCP and shell are dropped for now; REST tools cover managed tools)
12a. Add the MCP egress proxy service and internal network to Compose. — @api/docker-compose.yml (deferred with task 12)
12b. Add the per-project remote-source consent setting and check it before remote calls. — @api/Python-Service/app/infra/remote_consent.py, @api/Python-Service/app/rag/answer_service.py, @web/src/App.vue (done: toggle in Project access; also gates Code Quality assessment and design judge, business-rule assist, and Runtime analysis and diff preview; schema migrations have no project identity and send only a schema diff)
13. Implement the shell tool adapter in the constrained worker. — @api/Python-Service/app/infra/tool_executor.py (deferred with task 12)
14. Resolve REST hostnames and block private, loopback, and link-local addresses after resolution. — @api/Python-Service/app/infra/tool_executor.py (done; redirects are not followed)
15. Enforce per-call timeout, output-size, and per-run tool-call limits with configured defaults. — @api/Python-Service/app/agent_tools/limits.py, @api/Python-Service/app/agent_tools/executor.py, @api/Python-Service/app/config/orchestrator.yml (done)

### Phase 3 — Agent runtime

16. Wrap only the generation step of each workflow in the LangChain agent. — @api/Python-Service/app/agent_tools/runtime.py, @api/Python-Service/app/rag/answer.py (done; read-only tools only until Phase 4)
17. Keep routing, retrieval, graph expansion, and context planning upstream of the agent. — @api/Python-Service/app/rag/answer_service.py (done; unchanged upstream)
18. Fall back to retrieval-only generation when the probe reports no tool calling. — @api/Python-Service/app/agent_tools/runtime.py (`agent_workflow`; done, also when the agent fails before output)
19. Emit `tool_started` and `tool_completed` events in the answer stream. — @api/Python-Service/app/rag/answer.py (done; `done` and `/answer` also carry `tool_calls`)
20. Emit the same events through the orchestration progress channel. — not applicable: the orchestration pipeline never generates answers, so no agent runs there

### Phase 4 — Approvals

21. Persist pending calls and run checkpoints in SQLite with expiry and one-use state. — @api/Python-Service/app/agent_tools/approvals.py (done; LangChain HumanInTheLoopMiddleware + LangGraph SqliteSaver)
22. Add `POST /tool-approvals/{approval_id}` with approve and deny decisions. — @api/Python-Service/app/routers/tool_approvals.py (done; the response is the resumed answer as SSE)
23. Add a resume stream that loads the checkpoint and continues the run. — @api/Python-Service/app/agent_tools/runtime.py (`resume_agent_answer`), @api/Python-Service/app/rag/answer.py (`resume_answer_stream`) (done)
24. Proxy approval and event traffic through the web server. — @web/server.ts (done; approving on a project needs operator or admin)

### Phase 5 — UI

25. Show schema, effect class, and host allowlist in the LLM Tools card. — @web/src/components/config/LlmToolsCard.vue, @api/Python-Service/app/agent_tools/catalog.py (done; also workflows and model-access status)
26. List built-in tools as read-only, non-deletable entries. — @web/src/components/config/LlmToolsCard.vue (done; pipeline tools listed as not offered to the model)
27. Show tool progress and pending-call review controls in chat. — @web/src/components/ToolActivity.vue, @web/src/components/ToolApprovalCard.vue (done)
27a. Make `path:line` citations open the cited source. — @web/src/components/CodeCitationModal.vue, @api/Python-Service/app/routers/code_tools.py (done)
28. Show "tools unavailable for this model" when the probe fails. — @web/src/components/ToolActivity.vue (done; `tools` status rides on the `done` event)

### Phase 6 — Verification and cleanup

29. Add tests with a fake tool-calling remote model for repeated calls and limits. — @api/Python-Service/app/tests/test_agent_runtime.py (done: fake `BaseChatModel` drives repeated calls, the run budget, approvals and empty-answer retry)
30. Add tests for approval expiry, duplicates, denial, and stale runs. — @api/Python-Service/app/tests/test_tool_approvals.py (done)
31. Add tests for MCP discovery failure, REST host blocking, and shell argument handling. — @api/Python-Service/app/tests/ (done for REST host blocking in @api/Python-Service/app/tests/test_agent_tools.py; MCP and shell tests deferred with tasks 12 and 13)
32. Run existing router, retrieval, hybrid-answer, and stream tests unchanged. — @api/Python-Service/app/tests/test_stream_answer.py, @api/Python-Service/app/tests/test_hybrid_answer_path.py (done 2026-11-10: full suite 1354 passed; the 20 failures are pre-existing, in code_quality, remote_worker, indexing_service and runtime_* tests)
33. Mark the superseded parts of the AI tool-use roadmap. — @docs/plans/2026-10-01-ai-tool-use-and-refactoring-roadmap.md (done 2026-11-11: a status table maps each roadmap phase to its successor; Semgrep and refactor preview/apply stay open there)

## Acceptance Criteria

- A remote model that passes the probe completes a multi-step chat using a read-only built-in tool and a managed tool.
- A remote model that fails the probe answers from retrieval and the chat shows tools as unavailable.
- Every Phase 6 test passes, and existing tests pass without edits to their assertions.
- A paused run resumes after an API restart within its expiry.
- A project with remote-source consent off sends no source to the remote model.

## Dependencies

- @.archcore/agent-tools/langchain-tool-orchestration.spec.md — behavior this plan implements.
- A remote OpenAI-compatible endpoint with native tool calls; vLLM needs `--enable-auto-tool-choice` and a matching `--tool-call-parser`.
- @.archcore/agent-tools/implement-source-code-navigation-tools.plan.md depends on tasks 7, 10, 16, and 19 here.
- Hazard: tool results add source excerpts to remote requests; task 12b enforces project consent.
- Hazard: `chunk_code` writes Qdrant and Neo4j; task 8 keeps it out of model-visible tools.

## Clarifications

- 2026-11-10: the user targets remote models only; local-model tool calling and non-tool fallbacks are out of scope.
- 2026-11-10: the user chose checkpoint resume, per-project remote-source consent, and an MCP allowlist proxy.
- 2026-11-10: Phases 2 (except 12, 12a, 12b, 13) and 3 done; the package lives at @api/Python-Service/app/agent_tools/ instead of `app/agent/tools/`, because `app/agent/` imports the plugin platform. Live check with Cloudflare Gemma 4 called `project_structure` and streamed tool events. Kill switch: `AGENT_TOOLS_ENABLED=false`.
- 2026-11-10: Phase 1 done. `langchain-openai` is held at 1.1.9 because `semantic-router==0.0.20` requires `openai<2`; `langchain-community` is held at 0.3.31 because `ragas` imports modules removed in 0.4. Probe results live under settings key `llmRemoteToolCallingProbes` (@api/Python-Service/app/runtime/tool_calling_probe.py).
- 2026-11-11: status. Phases 0–5 done, and Phase 6 except the MCP and shell tests. Open: tasks 12, 12a and 13 (blocked until it is decided where MCP servers and shell tools run; the API container cannot start tool processes), the MCP and shell part of task 31, and task 33. Code is committed on branch `feature/agent-code-tools` in the api and web repositories.
- 2026-11-11: the user dropped MCP and shell tools for now (tasks 12, 12a, 13 and their tests deferred); managed tools stay REST-only. The accepted egress-proxy ADR still applies if MCP returns. Every other task is done, so this plan's remaining scope is the deferred MCP and shell work.

## Declared Delta

- create code built-in tool registry, agent runtime, approval store, `POST /tool-approvals/{approval_id}`
- amend code `runtime/llm.py` remote probe, `infra/tool_store.py` schema fields, `infra/tool_executor.py` MCP, shell, REST adapters
- amend ui LlmToolsCard, RAGChat tool events and approvals
- create adr paused-agent-runs-resume-from-checkpoint-on-new-stream
- create adr remote-model-source-access-requires-project-consent
- create adr mcp-servers-reach-only-allowlisted-hosts-through-egress-proxy
- create code MCP egress proxy service, project remote-source consent setting
- none spec langchain-tool-orchestration — remote-only scope satisfies clauses 2–3 unchanged
- amend doc ai-tool-use-and-refactoring-roadmap — mark superseded sections (task 33)
