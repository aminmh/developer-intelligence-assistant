---
title: "Route feedback rebuild empty state"
status: draft
tags:
  - "api"
  - "frontend"
---

## Purpose & Scope

Cohere route-feedback rebuilding converts saved Chat routing corrections into a fresh query embedding collection. Project source indexing uses a separate rebuild flow.

## Surface

- @api/Python-Service/app/rag/router.py: `rebuild_feedback_collection` reads the active Cohere and legacy feedback collections.
- @api/Python-Service/app/routers/embedding_jobs.py: `POST /api/embeddings/rebuild-route-feedback` queues a job; `GET /api/embeddings/rebuild/{job_id}` reports progress.
- @web/src/components/config/ExternalModelCard.vue: embedding settings show progress, completion, empty results, and failures.

## Normative Behavior

1. WHEN no routing corrections have been saved, the rebuilder MUST return an empty result without calling the embedding provider.
2. WHEN the rebuilder returns an empty result, the job worker MUST persist `status=done`, `stage=empty`, and zero counts.
3. WHEN an empty result arrives, the settings UI MUST explain that no routing feedback is saved.
4. WHEN a feedback rebuild completes, the settings UI MUST display the rebuilt correction count.
5. WHEN rendering the rebuild action, the settings UI MUST explain that it rebuilds saved Chat routing corrections.

## Constraints & Invariants

Empty rebuilds leave the active collection and generation registry unchanged. Successful nonempty rebuilds activate a fresh collection after persistence. The existing administrator access check remains on the web endpoint.

## Failure Behavior

1. IF Qdrant access fails, THEN the job worker MUST report an error.
2. IF saved points contain no usable queries or routes, THEN the rebuilder MUST report an actionable error.
3. IF embedding or persistence fails, THEN the job worker MUST report an error.
4. IF the registered active collection is missing, THEN the rebuilder MUST report an actionable error.

## Conformance

Observe empty state for initially absent collections and zero-point collections. Observe a missing registered active collection as an error. Observe provider, storage, and unusable feedback failures as errors. Confirm nonempty completion reports the saved count and activates the new generation. Runtime checks require the configured API and Qdrant services.