---
title: "Keep the Code Index in Sync with a Changing Codebase"
status: draft
tags:
  - "api"
  - "frontend"
---

## Goal

An edit in the developer's project folder, or a push to the tracked branch, becomes a new ready revision within minutes, re-embedding only changed chunks; chat answers from it automatically, and old revisions are removed per policy. Decision: @.archcore/knowledge-base/sync-creates-new-revisions-and-reuses-unchanged-vectors.adr.md. Business rules 14–21 in the Outline collection "Developer Assistant business rules" (sources in @docs/business-rules/) state the expected behavior.

## Tasks

### Phase 1 — Registry groundwork (done 2026-11-12, api `d61bde7`)
1. Add `source_kind`, `created_at`, `base_commit_sha`, `parent_revision_id`, `latest_ready`, `project_revisions` to @api/Python-Service/app/telemetry/registry.py. (done)
2. Add working-tree identity (`dirty`, `wt_` reference) to @api/Python-Service/app/telemetry/snapshot.py. (done)
3. Make Cohere generation publishing a locked read-modify-write in @api/Python-Service/app/index/embedding_rebuild.py. (done, api `b338063`)
4. Amend @.archcore/knowledge-base/knowledge-base-upload-identity.spec.md, @.archcore/code-quality/revision-removal.spec.md, @.archcore/code-quality/assessment-index-precheck.spec.md, and @.archcore/code-quality/codebase-quality-assessment.spec.md items 70–75. (done 2026-11-12, with Phase 6)

### Phase 2 — Re-embed only changed chunks (done 2026-11-12, api `b338063`)
5. Store `embed_hash` and `embed_profile` per point and reuse vectors from the project's newest three ready revisions in @api/Python-Service/app/index/llamaindex_layer.py. (done)
6. Report embedded vs reused counts in the index status. (done)

### Phase 3 — Git pushes on the shared pipeline (done 2026-11-12, api `d091e66`, web `0054f0f`)
7. Make `index_source` in @api/Python-Service/app/telemetry/source.py use the same multi-language pipeline as uploads. (done: exact PHP parse kept; other languages via the shared dispatch, plus SCIP and the repo map)
8. Add a 10-minute reconcile loop in @web/src/server/codeQualityRoutes.ts. (done: `gitReconcileIntervalMs`, listing from `GET /telemetry/git/sources`)

### Phase 4 — Chat follows the latest revision (done 2026-11-12)
9. Add a project-level "Latest" choice resolved by `latest_ready`, and label each answer with its revision. (done: @api/Python-Service/app/rag/revision_source.py, the picker in @web/src/components/RAGChat.vue)

### Phase 5 — Retention (done 2026-11-12)
10. Add a per-project `keep_latest` setting and worker-hosted cleanup with the exceptions in business rule 16; remove repo maps and the code-tools cache with their revision. (done: @api/Python-Service/app/telemetry/retention.py; rules in @.archcore/code-quality/revision-removal.spec.md items 7–10)

### Phase 6 — Local working tree (done 2026-11-12, api `9575399`, web `7e79267`)
11. Add the revocable folder grant, the helper's polling and deterministic packaging, the working-tree endpoint, and the latest-wins slot.
12. Show working-tree revisions in Code Quality as unavailable with that reason (the review of Phase 1 found they would read "not indexed").

## Acceptance Criteria

- A one-method change re-embeds single-digit chunks; graph and vector counts equal the chunk count.
- "Latest" answers from the newest ready revision and labels it; a pinned revision stays pinned.
- With N=2 and four revisions, the oldest unprotected revision is removed; reported revisions survive.
- Ten saves in a row index only the last state; `.env` never reaches the archive; revoking the grant stops uploads.

## Clarifications

- 2026-11-12: business-rule review of Phase 1 (deterministic, without the model, by user choice): 6 linked rules, all hold. Gaps: identical bytes uploaded as a ZIP and from the working tree become two revisions (rule 2), handled by the proposed rule 19 sentence and still undecided; working-tree revisions would show as "not indexed" in Code Quality (task 12).
- 2026-11-12: Phase 6 design: grants live in the registry (`working_tree_grants`, token SHA-256 only) behind `/telemetry/working-tree`; the web server creates them for operators and administrators, signs a helper authorization scoped to the grant, and accepts uploads at `POST /api/working-tree/upload` before session auth, re-checking the granting user's role on every upload and allowing one upload per grant every 30 s. The helper (@api/telemetry-dev/working_tree_sync.py) picks the folder in a native chooser, shows a native approval, keeps the token in the keychain, polls every 5 s, uploads after 30 s quiet and at most every 3 min, and imports the server's `is_secret_path` so both sides exclude the same files. Latest wins through job state `superseded`: queued jobs never run, a running one stops at the next embedding batch (progress callback now runs outside the per-batch try), and retention removes `superseded` rows; retention also runs right after every job. SCIP is skipped for working trees. Verified live on a scratch Git folder (project `wt-demo`): the helper's real poll cycle uploaded through the web route with `.env` left out; the revision indexed as `working_tree` on base `85250c8`; an edit produced a new revision and the old one was removed automatically; chat's latest pointed at it; Code Quality listed it disabled and the precheck and assess route refused it (409); revoking the grant made the next upload 401 and the helper stop and drop its keychain entry. Not verified: the native folder chooser and approval dialog (needs a person at the desktop) and Linux keyring paths. Known edges: the web's 30 s limit is per server process (in memory); a `superseded` row revived by an identical re-upload could race a retention pass.
- 2026-11-12: Phase 5 design: the policy (`keep_latest`, `pinned`) is a settings-store entry per project, edited in Project access next to remote-source consent (members read, administrators change). The persistent pin is "Keep always" in Project access, not the chat picker: the chat's pinned choice is per-browser state the server cannot see, so business rule 16's "pinned in chat" needs rewording. Cleanup runs in the telemetry worker loop every 10 minutes when no job ran, through `remove_revision`; a busy revision is retried next pass. Only ready revisions count. Working-tree revisions keep only the newest, outside N. Removal also drops the repo map and code-tools cache when no other revision shares the archive checksum. Found while testing: `source_reference` and several row-to-Snapshot rebuilds dropped the `dirty` flag, so a working-tree row would raise or resolve to the wrong collection; fixed with `Snapshot.from_row`. `embedding_rebuild` still builds its Snapshot from an identity without `dirty` (Phase 6 must pass it). Verified: dry run on live data with the default 3 removed nothing; in the browser, pinning docker-proxy's Git revision and setting Keep latest to 1 planned exactly the v2 test upload. The worker's first pass failed because ClickHouse was stopped and left the row in `deleting`, which retention then never retried; fixed so interrupted removals are retried every pass. After ClickHouse was started, the worker removed the revision and its repo map. Commits: api `e835de1`, web `e75022a`.
- 2026-11-12: Phase 4 design: revision mode `{project_id, revision}` is resolved once in Python (answer, stream, citation routes); the collection is pinned server-side and upload fields are dropped; the web server sends revision mode down the streaming path (classified, no orchestration job, since orchestration needs an uploaded ZIP). Trade-off: revision mode skips orchestration extras (PHP tool output, framework profile, doc retrieval) and runtime evidence, which needs a commit SHA the web server does not know; the agent's code tools cover reading code. Verified in the browser: the picker listed all projects including the Git-staged `proxy-git`; "docker-proxy · latest" answered from the newest upload (it reported the `status` field that exists only in that revision) labelled "upload · 27 min ago (latest)"; `proxy-git` resolved and was stopped by remote-source consent; the check found citations used the current selection instead of the answer's revision, fixed so a citation opens the revision that answered. Not done: the chat input placeholder still reads as if no project is selected in revision mode; gate preview is not offered in revision mode. Commits: api `cc1e0d9`, web `617f164` on `feature/agent-code-tools`.
- 2026-11-12: Phase 3 verified by staging the Go proxy through `SourceService.upload_git_snapshot` (what a sync does after fetching): the API's background worker indexed it to ready (previously "PHP only"), with 26 vectors, the repo map, and `code_symbol_lookup` callers of `refuse` from the tree-sitter graph. The GitHub fetch and webhook were not run live: no Git source is configured, and connecting one needs the user's GitHub token. The scheduler is covered by a web test (one source synced with its helper token, one skipped when the helper fails).
- 2026-11-12: Phase 2 verified through the real upload path: a one-line change to the Go proxy embedded 1 chunk and reused 25; line shifts do not affect reuse; search over reused vectors works.