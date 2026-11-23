---
title: "LangChain Tool Orchestration Migration"
status: accepted
tags:
  - "api"
  - "frontend"
---

## Purpose & Scope

This specification defines LangChain as the orchestration layer for model-driven tools across user-facing workflows. It covers chat, analysis, explain, debug, refactor, knowledge, and search workflows.

The migration includes built-in PHP tools and enabled managed MCP, REST, and shell tools. LlamaIndex remains responsible for embeddings, indexing, vector and graph retrieval, and graph expansion. Structured extraction, classification, and RAG evaluation remain on their current paths.

## Surface

The LangChain agent runtime owns model-facing tool selection, repeated tool calls, and final response generation. The existing tool registry remains the source of truth for managed tools.

Built-in PHP tools expose application-defined JSON schemas. MCP schemas are discovered from each configured server. REST and shell tools carry explicit JSON argument schemas in their managed configuration.

Each tool definition carries an effect classification. Unknown classifications default to side-effecting. Tool results and model-visible outputs are bounded and exclude credentials.

The answer stream and orchestration progress channel add tool progress and approval events. `POST /tool-approvals/{approval_id}` accepts `{decision: "approve"|"deny"}` and resumes the associated run.

## Normative Behavior

1. WHEN a user-facing workflow starts, the LangChain agent MUST receive only enabled tools permitted for that workflow.
2. WHEN the agent prepares a request, the provider adapter MUST use native tool calling only.
3. WHEN the selected model lacks native tool calling, the application MUST report that tool use is unavailable and preserve ordinary chat.
4. WHEN a tool schema is loaded, the application MUST validate its name, effect classification, and argument schema before exposing it.
5. WHEN the agent returns a tool call, the application MUST reject names absent from the workflow allowlist.
6. WHEN the application receives tool arguments, the application MUST validate them against the registered JSON schema before execution.
7. WHEN a managed tool is disabled, the application MUST exclude it from model-visible tools and reject stale calls.
8. WHEN an MCP server starts, the application MUST discover its tool schemas and restrict its network access to configured hosts.
9. WHEN a REST or shell tool is registered, the application MUST require an explicit JSON argument schema.
10. WHEN a shell tool executes, the application MUST run its configured executable with validated arguments in a constrained worker.
11. WHEN a tool is classified as read-only, the application MUST execute a valid call without approval.
12. WHEN a tool is side-effecting or unclassified, the application MUST pause before execution and request user approval.
13. WHEN a user approves a pending call, the application MUST execute that exact validated call once.
14. WHEN a user denies a pending call, the application MUST skip execution and return a denial result to the agent.
15. WHEN a tool call starts or finishes, the server MUST emit `tool_started` or `tool_completed` with the tool name and call status.
16. WHEN approval is required, the server MUST emit `approval_required` with the pending call identifier, tool name, and validated arguments.
17. WHEN an approval decision arrives, the server MUST accept it only for an existing, unexpired pending call and consume it once.
18. WHEN an agent reaches its configured tool-call limit, the application MUST stop tool execution and return a bounded partial result.
19. WHEN a tool returns output, the application MUST enforce configured timeout and output limits before returning it to the model.
20. WHEN a user-facing workflow completes, the application MUST preserve its existing route-specific response and retrieval behavior.

## Constraints & Invariants

- The application, not the model, owns tool registration, workflow allowlists, validation, execution, and approval state.
- Tool credentials never appear in model-visible schemas, arguments, results, or approval prompts.
- Shell arguments never become unrestricted model-generated shell command text.
- LlamaIndex remains the owner of indexing, embeddings, Qdrant/Neo4j retrieval, and graph expansion.
- Native tool-call support is evaluated for the selected provider/model; endpoint compatibility alone does not establish support.

## Failure Behavior

1. IF MCP schema discovery fails, THEN the application MUST mark that server's tools unavailable and report the discovery error.
2. IF schema or argument validation fails, THEN the application MUST reject the call without executing the tool.
3. IF tool execution fails or times out, THEN the application MUST return a bounded error result to the agent and record the failed call.
4. IF an approval expires or its run is unavailable, THEN the application MUST reject the decision without executing the call.
5. IF a configured host is outside the allowlist, THEN the application MUST block the network request.

## Conformance

A conforming implementation supports native multi-step tool calling across the specified user-facing workflows; validates calls; gates side effects; and preserves retrieval, utility-call, and ordinary-chat behavior.

Automated verification covers provider capability, allowlists, schemas, repeated calls, approvals, network restrictions, tool failures, limits, streaming events, and existing workflow compatibility.