---
title: "IDE Tokens Are Personal, Read-Only Bearer Tokens Checked by the Web Server"
status: draft
tags:
  - "api"
  - "auth"
  - "frontend"
---

## Context

The VS Code extension must read contract change events outside a browser session. Sessions are OIDC cookies (@web/src/server/oidcAuth.ts); the Python service trusts only the web server's internal secret. Working-tree grants (@web/src/server/workingTreeRoutes.ts) are per project and upload-only.

## Decision

- A signed-in user creates personal IDE tokens in Settings → IDE extension (`ide_<id>.<secret>`). The web server stores only a SHA-256 of the token in its auth store, with owner, name, and a 90-day expiry; at most 20 per user.
- `/api/ide/*` is registered before session authentication and accepts `Authorization: Bearer ide_…` only. A token acts as its owner and re-checks project access on every request, so removing a membership stops the IDE at once.
- A token reads events, impact, and generated code, and acknowledges events for its owner. It cannot change contracts or anything else.
- Workspaces are matched to projects by their GitHub remote against configured Git sources, or chosen by hand.

## Alternatives

- Reuse OIDC in the IDE (device flow): no session refresh path exists in the web server yet; more work for the same reach.
- Per-project scoped tokens: stricter, but a developer would manage one token per project; access is already re-checked per request.
- Let the extension call the Python service directly: it has no per-user auth.

## Consequences

- One token works for all of a user's projects; revoking it in Settings stops every extension using it.
- Tokens live in the JSON auth store beside sessions; a store with many users may need a database later.
- The extension stores the token in VS Code secret storage.
