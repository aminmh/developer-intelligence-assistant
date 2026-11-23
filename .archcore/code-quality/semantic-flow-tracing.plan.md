---
title: "Implement Bounded Semantic Flow Tracing"
status: draft
tags:
  - "api"
  - "code-quality"
---

## Goal

Implement deterministic, bounded source-to-sink traces for supported Code Quality language and flow types, with explicit partial and unavailable evidence.

## Tasks

1. Inventory existing AST, symbol, call-edge, framework, and revision metadata capabilities.
2. Define the first supported language and source-to-sink flow types against the tracing specification.
3. Add versioned analyzer registrations for supported languages, flow types, edge types, and traversal limits.
4. Build deterministic source and sink discovery over inventoried business source files.
5. Traverse declared call and data-flow edges within the selected immutable revision.
6. Emit ordered trace nodes, edges, path-and-line citations, and stable evidence identifiers.
7. Record unresolved dynamic dispatch, reflection, generated code, and dependency edges.
8. Mark traces partial or unavailable when graph coverage, analyzer support, or traversal limits prevent completion.
9. Pass bounded trace summaries and cited excerpts into the assessment evidence context.
10. Persist trace status, analyzer provenance, citations, and unresolved-edge summaries without full source snippets.
11. Expose trace coverage and unresolved reasons in assessment reports and the Code Quality view.
12. Add tests for deterministic ordering, revision isolation, supported complete paths, unresolved edges, limits, and unsupported constructs.
13. Document supported flow types, completeness semantics, limits, and known coverage gaps.

## Acceptance Criteria

- Every trace reads only nodes and edges matching the assessed project, immutable revision, commit, and source checksum.
- Repeated runs over identical indexed content produce the same trace ordering and evidence identifiers.
- Complete status applies only within the declared language, flow type, edge set, revision, and limits.
- Dynamic or unsupported edges remain visible as unresolved and never become proof that a path is absent.
- Limit exhaustion, incomplete graph indexing, and unsupported constructs produce partial or unavailable states with reasons.
- The evaluator receives bounded, cited evidence rather than an unrestricted source tree.
- Persisted reports retain trace summaries and citations but omit full source snippets.
- The user interface distinguishes complete-within-scope, partial, and unavailable trace coverage.

## Dependencies

- The Semantic Flow Tracing specification defines bounded completeness, evidence, and failure behavior.
- The Codebase Quality Assessment specification defines revision-scoped retrieval and evidence-based evaluation.
- The Git source synchronization plan supplies immutable source revisions when Git-backed projects are enabled.
- Existing Qdrant and symbol/call graph indexing may provide candidate context; unsupported edge types remain explicit.