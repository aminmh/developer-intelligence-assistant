---
title: "Language Servers Feed the Symbol Graph at Index Time"
status: draft
tags:
  - "api"
  - "code-quality"
---

## Context

The long-term goal is a cross-language assistant. An audit on 2026-11-15 found that LSP does one job today: diagnostics on fenced code blocks in model answers (@api/Python-Service/app/lsp/registry.py, `validate_markdown`). @api/Python-Service/app/lsp/client.py speaks only `didOpen`, `publishDiagnostics` and `didClose`.

Symbol navigation (definitions, references, implementations, call hierarchy) is built per language instead:

- PHP: a custom CLI over nikic/php-parser (`php bin/project-inspect`, @api/Python-Service/app/inspection/cli.py) writes `PhpSymbol` nodes to Neo4j (@api/Python-Service/app/inspection/graph.py).
- Go, Java, C#, Ruby, Rust: a Tree-sitter producer (@api/Python-Service/app/projects/ts_index.py), with name-based call resolution.
- TypeScript and Python: SCIP indexers in the analyzer worker (@api/Python-Service/app/code_quality/scip_index.py), origin `scip`.

`code_symbol_lookup` (@api/Python-Service/app/agent_tools/navigation.py) reads only the graph. phpactor is already installed (@api/Python-Service/php/lsp/composer.json) and speaks the same LSP methods that every other language server speaks. scip-php was deferred because it needs `composer install`.

## Decision

- Language servers become an index-time producer for the symbol graph. At indexing time a driver opens each file of the immutable revision, reads `documentSymbol`, `references` and `implementation` (and `callHierarchy` where the server offers it), derives call edges from each reference's enclosing definition, and writes the results as graph nodes and edges with origin `lsp`.
- The agent tools and inspection queries keep reading the graph. No tool call talks to a language server.
- The graph schema becomes language-neutral: `PhpSymbol` becomes `Symbol` with a `language` property.
- PHP (phpactor) is the first language. The `project-inspect` symbol and reference export stays as a fallback until LSP results for PHP match or beat it on a fixture revision.
- Language servers run under the no-build rule of @.archcore/code-quality/external-static-analyzer-integrations.spec.md: no project builds, dependency installs, build scripts, procedural macros or editor plugins.

- Where it runs (user, 2026-11-15): on the analyzer worker when the project has one with source-transfer consent, otherwise in the API container, where phpactor is already installed. Every project gets LSP edges; the parser export stays as the fallback when both fail.

Decided by the user on 2026-11-15.

## Alternatives

- **Worker only (like SCIP).** Rejected: projects without a consented worker, the common case, would keep only parser edges.
- **API container only.** Rejected: it gives up the worker's separate process and resource limits where a worker exists.

- **Live LSP queries per tool call.** Rejected: it needs one warm server per revision, cold start and workspace indexing can exceed the 10 s tool limit in @.archcore/agent-tools/source-code-navigation-tools.spec.md, and results would depend on server state rather than the stored revision.
- **Hybrid (graph for references, live LSP for definition and outline).** Rejected for now: it keeps both costs, and `code_outline` already works for every language through Tree-sitter.
- **More SCIP indexers.** Kept where they already work (TypeScript, Python). The PHP and Java indexers need project builds, and the SCIP ecosystem covers fewer languages than LSP does.

## Consequences

- Adding a language means adding a server entry in @api/Python-Service/app/config/lsps.yml and an adapter, not writing a new parser.
- Indexing becomes slower: one `references` request per definition. Each run needs request, time and output limits, and reports partial coverage when it hits them.
- Edge preference order becomes `scip`, then `lsp`, then `php-parser` or `tree-sitter`.
- Some servers execute project code by default (rust-analyzer build scripts and procedural macros, `tsserver` plugins from `tsconfig.json`, `gopls` module downloads). Every adapter turns these off. Neither the worker nor the API container blocks outbound network at the process level today, so this setting is the isolation that holds.
- Existing `PhpSymbol` collections need a migration or a re-index after the rename.
- Phase 0 of @.archcore/agent-tools/lsp-first-symbol-navigation.plan.md (2026-11-15) supports the decision for PHP: on pizza-builder, phpactor matched every project call site the parser found, resolved 18 the parser could not, scored 50/50 on hand-checked precision, and indexed in 75 s (parser 0.4 s). phpactor has no call hierarchy, and its default `composer.enable` loads the project autoloader, so the PHP adapter turns it off.
