---
title: "API Contract Sources and Versions"
status: draft
tags:
  - "api"
  - "frontend"
---

## Purpose & Scope

This spec is normative for the **contract store**: how a Swagger or OpenAPI document enters the app, becomes an immutable contract version, and is attached to projects. Front-end and back-end projects live in separate repositories and agree only through this document, so every later front-end feature (conformance checks, change impact, code and mock generation, chat) reads contract versions from this store. The user set the three sources on 2026-11-13.

Out of scope: comparing code or runtime responses with a contract, generating code, and the front-end code index. Each gets its own spec.

Terms: a **contract source** is a configured origin of one document. A **contract version** is one validated, immutable snapshot of it. An **attachment** links a source to a project.

## Surface

- Source kinds: `upload`, `repo_path`, `url`.
- Version fields: source ID, content hash, dialect (`swagger-2.0`, `openapi-3.0`, `openapi-3.1`), ingestion time, state (`valid`, `failed`), and for `repo_path` the back-end revision ID.
- Attachment fields: project ID, role (`provider` for the project that implements the API, `consumer` for a project that calls it), mode (`follow_latest`, `pinned`), pinned version ID.
- Revision identity this store builds on: @api/Python-Service/app/telemetry/registry.py and @api/Python-Service/app/telemetry/snapshot.py.
- Revision producers that trigger `repo_path` ingestion: @api/Python-Service/app/index/upload_service.py, @api/Python-Service/app/telemetry/git_source.py, @api/Python-Service/app/telemetry/working_tree.py.
- Remote-model consent check: @api/Python-Service/app/infra/remote_consent.py.
- The store, its HTTP API, and its web view do not exist yet; the plan names where they go.

## Normative Behavior

1. WHEN a user creates a contract source, the API MUST require one of the kinds `upload`, `repo_path`, or `url`.
2. WHEN a user creates a contract source, the API MUST require a project the user has operator or administrator access to.
3. WHEN the ingester receives a document, the ingester MUST parse it as JSON or YAML.
4. WHEN the ingester validates a document, the ingester MUST accept Swagger 2.0, OpenAPI 3.0, and OpenAPI 3.1.
5. WHEN a document passes validation, the ingester MUST convert it to one internal contract model shared by all dialects.
6. WHEN a document passes validation, the ingester MUST compute SHA-256 over the canonical form of that internal model.
7. WHEN the hash equals the hash of the source's newest valid version, the store MUST reuse that version.
8. WHEN the hash differs from the source's newest valid version, the store MUST create a new immutable contract version.
9. WHEN the store creates a version after an earlier one, the store MUST record the added, removed, and changed operations and schemas.
10. WHEN a back-end project registers a new revision, the ingester MUST read each `repo_path` source's configured path from that revision's archive.
11. WHEN a `repo_path` document holds relative file references, the ingester MUST resolve them inside the same revision archive.
12. WHEN a user requests a refresh of a `url` source, the fetcher MUST download the document through the controlled outbound path.
13. WHERE a `url` source has a refresh interval, the scheduler MUST refresh that source on the interval. [assumption]
14. WHEN a user attaches a source to a project, the API MUST record the role `provider` or `consumer`.
15. The store MUST allow several attachments per source and several attachments per project.
16. WHILE an attachment is in `follow_latest` mode, the resolver MUST return the source's newest valid version.
17. WHILE an attachment is in `pinned` mode, the resolver MUST return the pinned version.
18. WHEN a `repo_path` source belongs to a `provider` project, the resolver MUST offer the version ingested from a named back-end revision.
19. WHEN the contracts view opens, the view MUST show each source's kind, versions, validation state, and attachments.
20. WHEN any service sends contract text to the remote model, the service MUST first pass the attached project's remote consent check.

## Constraints & Invariants

- Constraint: a document MUST NOT exceed 10 MB. Larger files are almost always bundles of unrelated APIs and would dominate ingestion time. [assumption]
- Constraint: `url` sources MUST use HTTPS and MUST NOT carry credentials. The user asked for public URLs only.
- Constraint: the fetcher MUST refuse loopback, private, link-local, and cloud-metadata addresses, including after each redirect. The server fetches a user-supplied address.
- Constraint: the fetcher MUST limit a fetch to 15 seconds and 3 redirects. [assumption]
- Constraint: `upload` and `url` documents MUST be self-contained. Only `repo_path` has an archive to resolve file references against.
- Invariant: a contract version never changes after creation.
- Invariant: a failed ingestion never replaces a source's newest valid version.
- Invariant: a user sees a source only through a project that user can access.
- Invariant: a `repo_path` version always names the exact back-end revision it was read from.

## Failure Behavior

1. IF a document fails parsing or validation, THEN the ingester MUST record a `failed` attempt with the error list.
2. IF a `url` resolves to a refused address, THEN the fetcher MUST reject the request and name the reason.
3. IF a fetch exceeds its time or size limit, THEN the fetcher MUST report a retriable failure.
4. IF the configured path is absent from a new back-end revision, THEN the ingester MUST mark the source `missing` for that revision.
5. IF an `upload` or `url` document references another file, THEN the ingester MUST reject it and name the reference.
6. IF a user removes a version that an attachment pins, THEN the API MUST refuse and list those attachments.
7. IF a user attaches a source to a project without operator access, THEN the API MUST reject the attachment.
8. IF an attachment has no valid version to resolve, THEN the resolver MUST return an explicit unavailable state.

## Conformance

An implementation conforms when it satisfies behaviors 1–20, holds the four invariants, enforces the constraints, and follows failure rules 1–8.

- Given a `repo_path` source on a back-end project and a `consumer` attachment in `follow_latest` mode,
- When a Git sync registers a back-end revision whose contract file gained one operation,
- Then the store holds a new version naming that revision, with one added operation recorded,
- And the front-end project's attachment resolves to that version.

## Open Questions

- When a back-end revision is removed (@.archcore/code-quality/revision-removal.spec.md), do the contract versions read from it go too, or stay while an attachment pins them?
- Do `url` sources later need authenticated or private-network documents? Both are excluded today.
- Which default refresh interval suits `url` sources, if any?
