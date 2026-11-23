---
title: "Sync Creates New Immutable Revisions and Reuses Unchanged Vectors"
status: accepted
tags:
  - "api"
---

## Context

A project is indexed once per revision; when its code changes, every chunk of the next revision was re-embedded from scratch, though embedding is the slow, rate-limited step (Cloudflare free tier: 10,000 neurons per day). Reports, citations, traces, and assessments depend on revisions never changing (@.archcore/knowledge-base/knowledge-base-upload-identity.spec.md, @.archcore/code-quality/codebase-quality-assessment.spec.md).

## Decision

- Keeping the index in sync never edits an indexed revision. Every Git push, upload, or working-tree change becomes a new immutable revision.
- A new revision re-embeds only chunks whose embeddable text changed. Each Qdrant point stores `embed_hash` (SHA-256 of the embedded text) and `embed_profile` (provider, model, dimensions, input type, embeddable-text format). Matching vectors are copied from the same project's three newest ready revisions; for Cohere, only from each revision's active generation (@api/Python-Service/app/index/llamaindex_layer.py).
- Working-tree (uncommitted) revisions are dirty, have no `commit_sha`, and use the reference `wt_<archive sha256>`; their base commit is lineage metadata only (@api/Python-Service/app/telemetry/snapshot.py, @api/Python-Service/app/telemetry/registry.py).
- Decided by the user on 2026-11-12, with the sync sources (Git pushes and the local working tree), chat following the latest revision, and keeping the latest N revisions per project.

## Alternatives

- **Update a revision's index in place.** Rejected: breaks immutable revisions and every report, citation, and trace that points at one.
- **A separate embedding cache collection keyed by text hash.** Rejected: vectors of removed code would outlive revision removal (@.archcore/code-quality/revision-removal.spec.md, item 2), a cache shared across projects would reveal which texts exist, and it would need its own pruning lifecycle.

## Consequences

- Vectors live and die with their revisions; nothing crosses projects.
- Revisions indexed before 2026-11-12 have no `embed_hash`, so each project's next revision embeds everything once.
- A provider, model, or embeddable-text change never mixes vector spaces: the profile key differs and everything is re-embedded. `EMBEDDABLE_TEXT_FORMAT` must be bumped when `build_embeddable_text` changes.
- Measured: a one-line change to the Go Docker proxy re-embedded 1 chunk and reused 25.