---
title: "Use Cloudflare Workers AI as an embedding provider"
status: draft
tags:
  - "api"
  - "embeddings"
  - "frontend"
---

## Context

Cohere embedding requests can be rejected by account quota or rate limits. Project indexing and revision-scoped retrieval require a provider with stable document and query vectors.

## Decision

The application adds Cloudflare Workers AI as a selectable embedding provider. It uses the account REST endpoint with a user-provided account ID, API token, and model. The default model is `@cf/baai/bge-base-en-v1.5`.

Provider credentials remain write-only settings. Indexing and retrieval continue through the existing embedding interface, Qdrant, and Neo4j. Switching providers requires reindexing a revision when the stored vector dimension differs.

## Alternatives

- Continue using Cohere and resolve account quota or rate limits.
- Use the existing local Ollama or OpenAI-compatible embedding paths.
- Add a separate vector-only indexing path.

## Consequences

Cloudflare Workers AI provides a hosted fallback without changing retrieval callers. The provider's account access, model availability, and request limits remain external dependencies. Existing vectors are not silently reused across incompatible dimensions; the indexer detects a dimension change and reindexes the revision.