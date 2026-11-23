---
title: "Sending Source to the Remote Model Requires Per-Project Consent"
status: accepted
tags:
  - "api"
  - "frontend"
---

## Context

Chat already sends retrieved code chunks to the remote model: `use_remote` defaults to true in @api/Python-Service/app/rag/answer_service.py. Agent tool calls add more source excerpts (search hits, outlines, symbol references). Business-rule text already has a per-project `allow_remote` setting (@api/Python-Service/app/code_quality/http.py, @api/Python-Service/app/code_quality/rule_assist.py). Local models are out of scope, so the remote model is the only answer path.

## Decision

- Each project gets one setting: **Allow source to go to the remote model**.
- The setting covers retrieved chunks and tool results alike, in Development and Production Mode.
- When the setting is off, the project gets no remote answers and no tools; chat reports why.
- Only the configured remote endpoint receives source; tool results go nowhere else.
- [assumption] Existing projects start with the setting on, to keep current chat behavior; new projects start off.

## Alternatives

- **Always allowed.** No control per project. Rejected.
- **Block in Production Mode.** With local models out of scope, Production chat would have no model at all. Rejected.

## Consequences

- A single consent check runs before retrieval context or tool output leaves the host.
- The business-rule `allow_remote` setting stays separate; merging them is a later choice.
- Decided by the user on 2026-11-10.
