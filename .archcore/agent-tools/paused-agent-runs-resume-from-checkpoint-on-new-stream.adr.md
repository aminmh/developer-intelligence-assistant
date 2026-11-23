---
title: "Paused Agent Runs Resume from a Checkpoint on a New Stream"
status: accepted
tags:
  - "api"
  - "frontend"
---

## Context

The LangChain spec (@.archcore/agent-tools/langchain-tool-orchestration.spec.md, clauses 12–17) pauses side-effecting tool calls for approval and resumes the run after `POST /tool-approvals/{approval_id}`. Answers stream as SSE from `/answer/stream` (@api/Python-Service/app/main.py, @api/Python-Service/app/rag/answer_service.py `stream`), proxied by @web/server.ts. Read-only tools, including all navigation tools, never pause.

## Decision

- The agent runtime saves each paused run with a LangGraph checkpointer in SQLite, keyed by run ID.
- The current stream emits `approval_required` and then ends.
- After a decision, the chat opens a new resume stream for the run; the runtime loads the checkpoint and continues.
- Pending calls expire (default 10 minutes [assumption]) and are consumed once; an expired or consumed approval never resumes a run.

## Alternatives

- **Keep the SSE stream open while waiting.** Simplest, but the run is lost on page reload or API restart, and the web proxy must hold long idle connections. Rejected.

## Consequences

- Runs survive page reloads and API restarts until they expire.
- Adds a checkpoint table and a resume endpoint; the chat client handles a second stream per paused run.
- LangGraph becomes a pinned dependency (LangChain plan task 4).
- Decided by the user on 2026-11-10.
