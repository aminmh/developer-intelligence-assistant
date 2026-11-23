---
title: "Global Administrators Are a List in the Auth Store"
status: draft
tags:
  - "api"
  - "auth"
---

## Context

Roles were per project only (viewer, operator, admin). The one global identity was the bootstrap administrator from `OIDC_BOOTSTRAP_ADMIN_SUB`. Data management needs full-access administrators who are not tied to any one project.

## Decision

- The auth store (@web/src/server/oidcAuth.ts) holds `global_admins: string[]`, a list of OIDC subjects.
- `authIsGlobalAdmin` is true for the bootstrap administrator, the development user, or a subject on that list.
- Global administrators get the admin role in every project. Every check in @web/server.ts that used to call `authIsBootstrapAdmin` now calls `authIsGlobalAdmin`.
- The list is managed through `GET/PUT/DELETE /api/auth/global-admins`. The bootstrap administrator cannot be removed, and neither can the last administrator when no bootstrap administrator is configured.

## Alternatives

- **Bootstrap administrator only:** only one person could ever administer data.
- **Keycloak realm roles:** this would need token-claim plumbing and a change to the realm. The app already keeps its roles in its own store.

## Consequences

- Global access is a superset of what the bootstrap administrator had before, so existing behaviour is unchanged.
- `/api/auth/me` now returns `globalAdmin`.
