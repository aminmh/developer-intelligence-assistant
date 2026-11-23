---
title: "Source Code Navigation Tools for the Model and Agent"
status: draft
tags:
  - "api"
---

## Purpose & Scope

Normative for the built-in, read-only source code navigation tools that the remote model calls through the LangChain agent runtime (@.archcore/agent-tools/langchain-tool-orchestration.spec.md) in chat, explain, debug, refactor, knowledge, and search workflows. Dependents: the agent runtime, Code Quality evidence assembly, and the LLM Tools UI.

Tools covered: text search (ripgrep), structural search (ast-grep), file read (bounded line windows), file outline (Tree-sitter), symbol definitions and references (Neo4j, including SCIP- and LSP-derived edges), change impact (`code_impact`, whose report is governed by @.archcore/agent-tools/change-impact-analysis.spec.md), repository map, and source statistics (scc).

Not covered: indexing pipelines that produce chunks and graph edges, external analyzers (SonarQube, Joern, Rector) — see @.archcore/code-quality/external-static-analyzer-integrations.spec.md — and tools that change source.

## Surface

- Tool registry entries with JSON argument schemas and effect `read_only`: @api/Python-Service/app/agent/core/registry.py [assumption: final module chosen during implementation].
- Tool implementations: @api/Python-Service/app/agent_tools/navigation.py.
- Source resolution and revision cache: @api/Python-Service/app/agent_tools/source.py (revision archive cache, else the retained upload workspace).
- Remote provider and tool-calling probe: @api/Python-Service/app/runtime/llm.py (`test_remote`, `_get_remote_client`).
- Result envelope fields: `tool`, `tool_version`, `project_id`, `revision_id`, `items[]` (path, start_line, end_line, excerpt, symbol), `truncated`, `limit_reached`, `coverage`, `error`.

## Normative Behavior

1. WHEN a navigation tool runs, the tool runner MUST resolve one authorized project and one immutable revision before reading source.
2. WHEN a navigation tool reads source, the tool runner MUST read only the revision's read-only extracted cache.
3. WHEN the cache for a revision is absent, the tool runner MUST extract the revision archive with the existing safe-extraction limits.
4. WHEN a tool lists files or matches, the tool runner MUST exclude paths that the revision's effective exclusions exclude.
5. WHEN the tool registry loads navigation tools, the registry MUST classify each one as `read_only`.
6. WHEN the remote model passed the tool-calling probe, the agent runtime MUST expose enabled navigation tools allowed for the workflow.
7. WHEN the remote connection is tested, the provider adapter MUST verify one native tool-call round trip with a stub tool.
8. WHEN a tool call returns items, the tool runner MUST return them to the model with path and line citations.
9. WHEN text search runs, the tool runner MUST pass the pattern to ripgrep as an argument, never through a shell.
10. WHEN structural search runs, the tool runner MUST run ast-grep in search mode without rewrite output.
11. WHEN a symbol lookup runs, the symbol service MUST report each edge's origin: `php-parser`, `tree-sitter`, `python-ast`, `lsp`, `scip`, or `runtime`.
11a. WHEN a symbol lookup runs, the symbol service MUST read one language-neutral symbol graph and report each item's language.
11b. WHEN a static call edge ran on the selected revision, the symbol service MUST mark it confirmed and list it first.
11c. WHEN a revision has runtime data, the symbol service MUST mark static call edges never seen running as unconfirmed.
11d. WHEN a runtime edge was recorded on another revision, the symbol service MUST exclude it and count it as stale.
12. WHEN a repository map is requested, the map builder MUST rank symbols deterministically and fit the result to the requested token budget.
13. WHEN a tool returns items, the tool runner MUST order them by stable path and line before applying limits.
14. WHEN a tool applies a limit, the tool runner MUST set `truncated` and name the limit reached.
15. WHEN a tool result is recorded, the tool runner MUST record tool name, tool version, and revision identity in the answer trace.

## Constraints & Invariants

- Navigation tools MUST NOT execute project code, project build scripts, or project configuration. Rationale: uploaded source is untrusted.
- Navigation tools MUST NOT write to the revision cache or the archive.
- Navigation tools MUST NOT search, read, or list credential files (`.env*` except examples, keys, certificates, `auth.json`, `.git/`). Rationale: tool output goes to the remote model.
- Each call is bounded by wall time (default 10 s), output bytes (default 64 KiB), and item count (default 200). Rationale: small local model context and shared API process.
- ripgrep and ast-grep run as subprocesses with CPU, memory, and process limits, matching @api/Python-Service/app/code_quality/analyzers.py.
- Tool output is untrusted text; the answer prompt treats it as data, never as instructions.
- A tool result never contains credentials, environment values, or paths outside the revision cache.
- Tool results are sent only to the configured remote model endpoint. Rationale: source excerpts leave the host through that one provider.
- Local models are out of scope; no prefetch or non-tool-calling fallback exists for navigation tools.
- Navigation tools never call a language server; language servers feed the graph at index time only (@.archcore/agent-tools/language-servers-feed-the-symbol-graph-at-index-time.adr.md).

## Failure Behavior

1. IF a revision is not ready or not authorized, THEN the tool runner MUST return an error without reading source.
2. IF ripgrep, ast-grep, or scc is missing, THEN the tool runner MUST return `unavailable` naming the binary.
3. IF a pattern is invalid, THEN the tool runner MUST return the tool's parse error, bounded to 2 KiB.
4. IF a call exceeds its time limit, THEN the tool runner MUST stop the subprocess and return partial items with `limit_reached: time`.
5. IF the language of a file has no Tree-sitter grammar, THEN the outline tool MUST report the file as unsupported.
6. IF the graph store is unavailable, THEN the symbol service MUST return `coverage: unavailable` and no fabricated edges.
7. IF the remote model fails the tool-calling probe, THEN the application MUST report navigation tools unavailable and keep retrieval-only answers.

## Conformance

An implementation conforms when every navigation tool is read-only, revision-scoped, bounded, and cited, and when a remote model that passed the tool-calling probe reaches the tools through the agent runtime.

Given a remote-model chat on revision R, When the user asks "where is `InvoiceMailer` sent", Then the agent calls text search and symbol lookup on R and the answer cites `path:line`.

## Clarifications

- 2026-11-22: `code_impact` added to the tools covered; its behavior lives in the change impact analysis spec.
- 2026-11-15: origin `runtime` added for indexr-otel `RUNTIME_CALLS` edges (@.archcore/agent-tools/lsp-first-symbol-navigation.plan.md, Phase 3b).
- 2026-11-15: origins `lsp` and `python-ast` added to clause 11 (`python-ast` was already emitted, per the implementation plan's 2026-11-11 note); clause 11a added for the language-neutral graph.
- 2026-11-10: the user targets external (remote) models only for accuracy; local models such as gemma3-4b are out of scope. The route-prefetch path was removed.
