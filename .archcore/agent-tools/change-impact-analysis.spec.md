---
title: "Change Impact Analysis over the Symbol Graph"
status: draft
tags:
  - "api"
  - "code-quality"
---

## Purpose & Scope

Normative for change impact analysis: given a change, report the symbols, routes and tests that depend on it, read from the revision's language-neutral `Symbol` graph in Neo4j. Dependents: the remote model through the `code_impact` agent tool, API clients (CI scripts, a future UI) through one HTTP route, and the business-rule impact check (@api/Python-Service/app/code_quality/rule_assist.py `check_impact`).

Covered: change resolution (diff, symbol names, revision pair), upstream traversal over call, reference and runtime edges, route and test attribution, ranking, limits and coverage.

Not covered: building the graph (@.archcore/agent-tools/language-servers-feed-the-symbol-graph-at-index-time.adr.md), API contract impact over UI maps (@api/Python-Service/app/api_contracts/conformance.py `impact_report`), and a UI view.

## Surface

- Engine: @api/Python-Service/app/inspection/impact.py [assumption: new module], reading through @api/Python-Service/app/inspection/queries.py.
- Agent tool `code_impact`, effect `read_only`: @api/Python-Service/app/agent_tools/navigation.py.
- HTTP route `POST /code-tools/impact`: @api/Python-Service/app/routers/code_tools.py.
- Diff-to-symbol mapping: @api/Python-Service/app/code_quality/rule_code.py `changed_symbols`; per-symbol body hashes: `symbol_hashes`.
- Route sources: `RuntimeRoute -[HANDLED_BY]-> Symbol` (@api/Python-Service/app/inspection/graph.py) and static routes (@api/Python-Service/app/projects/backend_routes.py).
- Service shared by the tool and the route: @api/Python-Service/app/inspection/impact_service.py (`run_impact`); external callers reach the route through the web server's authorized proxy `POST /api/code/impact` (@web/src/server/codeImpactRoutes.ts).
- Producer coverage: LSP and SCIP status per collection, stored at index time on one `SymbolCoverage {collection}` node (@api/Python-Service/app/inspection/graph.py `upsert_coverage`), read by `SymbolGraphQuery.producer_coverage`.
- Input: `project_id`, `revision_id` (or `latest`), exactly one of `diff`, `symbols[]`, `base_revision_id`; optional `depth`, `max_nodes`.
- Report fields: `project_id`, `revision_id`, `base_revision_id` and `unavailable_revision` (revision pairs), `changed[]` (graph `symbol_id`, stable `canonical_symbol_id`, fqn, language, path, line, change kind), `ambiguous[]`, `impacted[]` (graph and canonical ids, `depth`, `via` path, `edge` with origin and `via_class`, `confirmed`, `test`, `revision_id`), `routes[]` (source, method, path, handler fqn, `handler_path`, `handler_line`, depth), `tests[]`, `truncated`, `limit_reached`, `coverage`, `coverage_reasons[]`, `producers`, `unresolved[]`, `runtime`. The agent tool adds cited `items[]`.

## Normative Behavior

1. WHEN an impact request arrives, the impact service MUST resolve one authorized project and one ready revision before reading the graph.
2. WHEN the input is a unified diff, the impact service MUST map hunks to changed symbols with `changed_symbols`.
3. WHEN the input is symbol names, the impact service MUST resolve each name by FQN, then by name, on the selected revision.
4. WHEN a symbol name matches more than one definition, the impact service MUST treat every match as changed and report the ambiguity.
5. WHEN the input is a base revision, the impact service MUST mark symbols added, removed, or with a different body hash as changed.
6. WHEN changed symbols are known, the impact service MUST walk caller, reference, implementation and runtime-caller edges upstream breadth-first.
7. WHEN the walk reaches a symbol, the impact service MUST record its depth and one shortest `via` path back to a changed symbol.
8. WHEN several edges reach the same symbol at one depth, the impact service MUST keep the edge in origin order `scip`, `lsp`, parser, `runtime`.
9. WHEN an impacted symbol is a method of a class, the impact service MUST also walk callers of that method on subclasses that do not override it.
10. WHEN a reached symbol handles a runtime or static route, the impact service MUST add that route to `routes`.
11. WHEN a reached symbol lies in a test file, the impact service MUST add it to `tests` and stop walking past it.
12. WHEN the revision has runtime data, the impact service MUST mark each static edge confirmed or unconfirmed per @.archcore/agent-tools/source-code-navigation-tools.spec.md clauses 11b–11d.
13. WHEN the report is returned, the impact service MUST order `impacted` by depth, then confirmed first, then path and line.
14. WHEN the agent tool returns the report, the tool runner MUST cite each impacted symbol and route with path and line.
15. WHEN the HTTP route and the agent tool receive the same input, the impact service MUST return the same report.
16. WHEN the business-rule impact check runs, the rule service MUST include rules linked to impacted symbols up to depth 2.
17. WHEN a rule is included only through an impacted symbol, the rule service MUST mark that link indirect with its `via` path.

## Constraints & Invariants

- Impact analysis MUST NOT execute project code or call a language server. Rationale: uploaded source is untrusted; the graph is filled at index time.
- Defaults: `depth` 3 (maximum 6), `max_nodes` 300 (maximum 2,000), wall time 10 s. Rationale: hub methods such as `ApiResponse::successResponse` have 59 callers on pizza-builder. [assumption]
- Test files: paths under `tests/`, `test/`, `spec/`, `__tests__/`, or named `*Test.php`, `test_*.py`, `*_test.go`, `*.spec.ts`, `*.test.ts`. [assumption]
- The report never contains an edge absent from the graph; an unresolved call stays in `unresolved`, never in `impacted`.
- Runtime edges from another revision never enter the walk.
- Input diffs are bounded to 200 KB, matching `MAX_DIFF_BYTES` in `rule_assist.py`.
- The business-rule check keeps its cap of 12 reviewed rules, preferring direct links over indirect ones.
- `diff` and `base_revision_id` inputs need revisions identified by a commit sha, because definitions and archives load through `rule_code.load_revision`; a revision without one can use `symbols[]` only.

## Failure Behavior

1. IF the revision is not ready or not authorized, THEN the impact service MUST return an error without reading the graph.
2. IF the request names none or more than one input kind, THEN the HTTP route MUST return 422.
3. IF a diff yields no hunks, THEN the impact service MUST return an error naming the expected unified diff format.
4. IF no changed symbol resolves, THEN the impact service MUST return an empty report listing the unresolved inputs.
5. IF the base revision has no graph, THEN the impact service MUST return `coverage: unavailable` naming that revision.
6. IF the graph store is unavailable, THEN the impact service MUST return `coverage: unavailable` and no fabricated edges.
7. IF a depth, node or time limit stops the walk, THEN the impact service MUST return the partial report with `truncated` and `limit_reached`.
8. IF the revision's LSP or SCIP coverage is partial or unavailable, THEN the impact service MUST report coverage partial.

## Conformance

An implementation conforms when every report is revision-scoped, bounded, built only from stored graph edges, and identical across the tool and the HTTP route.

Given pizza-builder revision R, When `code_impact` runs with symbol `ApiResponse::successResponse` and depth 1, Then `impacted` lists its 59 callers with origin `lsp` and the routes they handle.

Check map (`docker compose run --rm --no-deps python-api python -m pytest tests/<file> -k <name>`):

- 1: test_change_impact.py::test_rejects_unready_revision
- 2: test_change_impact.py::test_diff_maps_to_changed_symbols
- 3: test_change_impact.py::test_symbol_names_resolve_by_fqn_then_name
- 4: test_change_impact.py::test_ambiguous_name_marks_all_matches
- 5: test_change_impact.py::test_revision_pair_detects_added_removed_changed
- 6: test_change_impact.py::test_walks_callers_references_implementations_runtime
- 7: test_change_impact.py::test_records_depth_and_shortest_via
- 8: test_change_impact.py::test_keeps_most_precise_origin
- 9: test_change_impact.py::test_inherited_method_callers_on_subclasses
- 10: test_change_impact.py::test_routes_from_runtime_and_static_sources
- 11: test_change_impact.py::test_tests_collected_and_walk_stops
- 12: test_change_impact.py::test_runtime_confirmation_marks
- 13: test_change_impact.py::test_impacted_order
- 14: test_navigation_tools.py::test_code_impact_items_cite_path_and_line
- 15: test_impact_route.py::test_route_matches_tool_report
- 16: test_code_quality_rule_assist.py::test_impact_check_includes_rules_of_callers
- 17: test_code_quality_rule_assist.py::test_indirect_rule_links_carry_via
- F1: test_change_impact.py::test_rejects_unready_revision
- F2: test_impact_route.py::test_requires_exactly_one_input_kind
- F3: test_change_impact.py::test_diff_without_hunks_errors
- F4: test_change_impact.py::test_unresolved_inputs_empty_report
- F5: test_change_impact.py::test_base_revision_without_graph_unavailable
- F6: test_change_impact.py::test_graph_down_unavailable
- F7: test_change_impact.py::test_limits_truncate_with_reason
- F8: test_change_impact.py::test_partial_producer_coverage_reported

## Clarifications

- 2026-11-22 (review): Surface brought up to the implemented report, the shared service, the web proxy and producer coverage; the commit-sha constraint added after the live pizza-builder run found that uploads without a commit cannot use diff or revision-pair input.
- 2026-11-22: the user chose an agent tool plus one HTTP route; no UI in this scope.
- 2026-11-22: the user chose all three inputs: unified diff, symbol names, and a pair of indexed revisions.
- 2026-11-22: the user chose to have the business-rule impact check use impacted symbols now, not as a follow-up.
- 2026-11-22: defaults for depth, node limit and test-file patterns were set by the planner and are marked [assumption].
