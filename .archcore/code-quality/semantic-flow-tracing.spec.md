---
title: "Semantic Flow Tracing for Code Quality Assessment"
status: draft
tags:
  - "api"
  - "code-quality"
---

## Purpose & Scope

This specification defines bounded, revision-scoped source-to-sink traces used as compact evidence in Code Quality assessments. It does not promise whole-program completeness across unsupported languages, frameworks, dynamic behavior, or unavailable graph edges.

## Surface

- Trace construction and retrieval: @api/Python-Service/app/code_quality/semantic_context.py and @api/Python-Service/app/index/llamaindex_layer.py.
- Revision identity and indexed source metadata: @api/Python-Service/app/telemetry/snapshot.py.
- Assessment evidence and report coverage: @api/Python-Service/app/code_quality/service.py.

## Normative Behavior

1. WHEN an assessment requests a trace, the trace service MUST constrain every source node and edge to the selected project and immutable revision.
2. WHEN a trace service reads indexed nodes, it MUST verify project ID, revision ID, commit SHA, and source archive hash.
3. WHEN an analyzer is registered, the registry MUST declare its languages, source and sink types, edge types, version, and execution limits.
4. WHEN the trace service selects an analyzer, it MUST select only analyzers registered for the detected language and requested flow type.
5. WHEN the trace service builds a trace, it MUST use deterministic graph traversal over declared edge types.
6. WHEN a trace is emitted, the trace service MUST identify its source, sink, ordered nodes, and connecting edges.
7. WHEN a trace includes a node or edge, the trace service MUST provide its source path and line range.
8. WHEN the trace service enumerates alternatives, it MUST order nodes and edges by stable source identity before applying limits.
9. WHEN traversal reaches a declared execution limit, the trace service MUST mark the trace partial and report the limit reached.
10. WHEN traversal encounters dynamic dispatch, reflection, generated code, or an unresolved dependency, the trace service MUST record the unresolved edge and location.
11. WHEN a trace reports complete, the trace service MUST state the supported language, flow type, graph edges, and applied limits.
12. WHEN a trace reports complete, the trace service MUST mean traversal exhausted the declared graph scope without unresolved edges or reached limits.
13. WHEN an index lacks required graph edges, the trace service MUST report partial or unavailable coverage.
14. WHEN an analyzer does not support a detected language or framework construct, the report MUST identify the unsupported scope.
15. WHEN semantic context reaches the evaluator, the assessment service MUST send bounded trace summaries and cited source excerpts.
16. WHEN the evaluator receives a partial trace, it MUST treat unresolved paths as unknown rather than absent.
17. WHEN the evaluator receives no trace evidence, it MUST not infer that the requested flow does not exist.
18. WHEN a report persists trace evidence, the report store MUST retain trace status, analyzer provenance, evidence IDs, and source locations.

## Constraints & Invariants

- Complete means complete only within the declared language, flow type, graph edges, revision, and limits; it is not whole-program proof.
- Trace construction is deterministic, read-only, bounded, and isolated to one immutable revision.
- Retrieved source text remains transient; saved reports retain citations and summaries, not full snippets.
- Unsupported constructs and unresolved edges remain visible; the evaluator cannot convert missing evidence into a clean result.

## Failure Behavior

1. IF the selected revision lacks a ready matching index, THEN the trace service MUST report unavailable without searching another revision.
2. IF source identity metadata conflicts with the selected revision, THEN the trace service MUST discard the affected node or edge.
3. IF the index cannot provide required graph data, THEN the report MUST mark trace coverage unavailable or partial with a reason.
4. IF traversal exceeds time, node, edge, or path limits, THEN the report MUST mark affected traces partial.
5. IF a trace cannot resolve a source or sink, THEN the report MUST identify the missing endpoint and avoid asserting absence.

## Conformance

An implementation conforms when trace results are revision-isolated, deterministic within declared scope, bounded, cited, and explicit about unresolved paths and unsupported coverage.