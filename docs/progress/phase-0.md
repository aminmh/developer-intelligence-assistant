# Phase 0 — PHP runtime telemetry foundations

Status: implementation complete; gate ready for review. Date: 2026-09-26.

Scope is the approved Phase 0 plan. Stop for review before discovery,
instrumentation, aggregation, hooks, or runtime product features. Production,
staging and development are in scope for contracts; application container
management is outside this feature.

## Baseline and task checkpoint

Backend root `api`, branch `feature/domain-behavior-usages`, initial HEAD
`c8daba7d309ccd7e34066f8d31e8a0bb08a91ae5`; no upstream or active Git operation;
one existing worktree. Preexisting edits: `.env`, `app/agent/domain_model.py`,
`app/config/domain_dictionary.txt`, `app/tests/test_domain_model.py` (under
Python-Service), plus two untracked prompt files. Preserve all of these.
Workspace-level docs are outside both backend and frontend Git repositories.

Read: approved user plan, complete telemetry roadmap, architecture map,
index/storage/upload/indexing flows, PHP AST chunk exporter, inspection graph.

Identity inventory:

| Owner | Existing identity | Phase 0 boundary |
| --- | --- | --- |
| `index/llamaindex_layer.py` | SHA1(class FQN + method) | preserve legacy; explicit snapshot storage IDs |
| `index/indexer.py` | class/name + file/line | canonical readable symbol field, legacy storage retained |
| `inspection/graph.py` | SHA1(kind + FQN + name) | optional snapshot scope, canonical metadata |
| PHP `CodeChunk` | SHA1(file + FQN + type + lines) | retrieval chunk ID remains; add canonical symbol ID |
| saved analyses | filename/session collections | unchanged; never infer commit metadata |

Change necessity: telemetry cannot safely join the existing conflicting IDs or
mutable filename collections. New code is necessary for a shared normalizer,
immutable snapshot registry and authenticated upload boundary. Minimum scope:
new telemetry owners, narrow opt-in wiring into existing indexers, optional
isolated stack. No data migration/deletion.

Architecture review required: yes (identity/persistence/security contract).
Complexity budget: separate identity, registry, upload orchestration and HTTP
owners; existing large indexer receives wiring only. Legacy ID behavior is a
bounded compatibility exception for saved analyses; retirement requires an
explicit future migration with legacy-read evidence. TDD route: off; tests
are risk-based contract and regression checks, without strict test-first order.

Tasks:
1. Baseline/contracts and stale architecture correction — complete.
2. PHP/Python canonical normalizers and shared golden fixtures — complete.
3. Opt-in scoped storage and static export integration — complete.
4. Snapshot metadata, authenticated CI upload, state transitions — complete.
5. Attribute/privacy contract and isolated development stack — complete.
6. Gate verification — complete with limitations below; phase review pending.

## Acceptance checks

- PHP fixture script and real AST exporter agree byte-for-byte with Python for
  all roadmap IDs; invalid identities and duplicate definitions are reported.
- Two projects × two full commits isolate graph/vector IDs and collections.
- Repeated archive is idempotent; conflicting archive is rejected; failed
  indexing never publishes ready; missing source reports awaiting_source.
- Authentication fails closed and tokens are project scoped.
- Legacy indexing and saved-analysis read regressions still pass.
- Optional Collector/ClickHouse/Neo4j stack accepts sanitized test telemetry;
  canaries are absent from persisted span contents. Telemetry has no graph or
  Qdrant export in Phase 0.
- Record actual commands/results and limits. Runtime p95 overhead, resolution
  and coverage targets are unmeasured until later instrumentation phases.

## Verification receipt

- Cross-language golden identities and real PHP exporter fixtures: 72 golden
  cases pass byte-for-byte; PHP syntax checks pass.
- Python isolated regression run in the prebuilt Python 3.12 container with
  network disabled and source mounted read-only: 173 passed, 1 warning. It
  includes the three telemetry suites plus indexing, saved-analysis, AST,
  inspection and graph-leg regressions. Command and selected paths are recorded
  in the implementation handoff; runtime integration still depends on the
  configured application data services.
- Snapshot tests cover project/commit namespace isolation, repeat/conflict
  uploads, readiness-gated lookup, strict persistence counts, reserved snapshot
  namespaces and legacy read/write boundaries.
- Optional development stack smoke passed against actual Collector,
  ClickHouse and Neo4j services: sanitized canary absent from stored trace
  columns, linked span rejected, seven-day TTL present, graph empty, and network
  separation verified. This collector allow-list is synthetic-fixture-only.
- The throwaway-certificate mTLS integration test is unverified in the current
  handoff: Docker access is denied by the active environment. Collector config
  validation had passed in the prior execution context, but the complete
  trusted/missing/untrusted client handshake result is not established.
- `git diff --check` passes. Pre-existing `.env`, domain-model, domain-dictionary,
  tests and prompt edits are preserved; task changes did not change their
  content. The `.gitignore` adjustment made during work was removed.
- No application telemetry exporter exists yet. Performance overhead,
  resolution percentage, visibility latency under real instrumentation,
  and Phase 4 exercised-edge coverage are not measured or claimed.

Phase 0 gate is ready for review, subject to the explicit mTLS limitation above.
No Phase 1 discovery or later-phase work was started.

Checkpoint carrier: this requested progress document plus inline updates.
Next: review this gate before authorizing Phase 1. Drift decision: stop at Phase 0.
