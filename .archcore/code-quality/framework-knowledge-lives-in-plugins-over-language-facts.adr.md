---
title: "Framework Knowledge Lives in Plugins; Language Syntax Comes from Each Language's Own Parser"
status: accepted
tags:
  - "api"
  - "code-quality"
---

## Context

The gap-closing plan (@.archcore/code-quality/close-assessment-gaps-with-panel-review.plan.md) adds many framework-aware checks: route authorization, configuration risks, translations, templates, test conventions. The first one, the Symfony route-authorization analyzer (branch `feature/route-authorization-analyzer`, @api/Python-Service/app/code_quality/route_authorization.py), parses PHP with Python regular expressions (class declarations, attributes, brace matching) and keeps Symfony rules inside `code_quality`. The Laravel route parser (@api/Python-Service/app/agent/plugins/frameworks/laravel/routes.py) does the same. Meanwhile the PHP CLI already exports PHP 8 attributes from nikic/php-parser (`project-inspect attributes`), phpactor feeds references and types into the symbol graph, and framework plugins (@api/Python-Service/app/agent/plugins/frameworks/) already declare capabilities such as `ROUTE_ANALYSIS`. The user wants to add frameworks later without touching the checks.

## Decision

Three layers, each with one job:

1. **Language facts** come from the language's own parser, never from Python regular expressions over source. PHP: one batch export from the PHP CLI (classes, methods, attributes and docblock annotations with resolved arguments, class constants, static calls with literal arguments). Other languages: their compiler or Tree-sitter queries. References, types and call edges come from the symbol graph (LSP, SCIP, parsers). Configuration formats (YAML, JSON, XML, `.env` examples) are data and may be read in Python.
2. **Framework plugins** turn facts and configuration into normalized, language-neutral records through capability providers: routes (method, path, handler symbol, location), authorization guards and access rules, translation catalogs, templates, test conventions, configuration risks. A plugin lives in `agent/plugins/frameworks/<name>/` and declares the capabilities it provides.
3. **Category checks** are framework-neutral: they read only normalized records (for example "a route whose handler has no guard and no matching access rule") and never name a framework.

Adding a framework means adding one plugin that provides some capabilities; checks pick it up through the registry. Adding a language means a facts exporter (and, if available, an LSP or SCIP producer).

## Alternatives

- **Pure Python per framework (current route check).** Fast to write; rejected because PHP syntax handling is duplicated per check and misses cases the real parser handles (multi-line attributes, nested arrays, constant expressions).
- **LSP for framework facts.** Rejected for this layer: LSP exposes symbols, references and types, not attribute arguments or framework configuration. It stays the source for the symbol graph.
- **PHP-side framework plugins (logic in the PHP CLI).** Rejected: every framework check would need PHP code and a second plugin system; facts export stays generic instead.

## Consequences

- The PHP CLI gains a batch facts export; the route-authorization analyzer and the Laravel route parser move onto it and into their plugins.
- New capabilities in @api/Python-Service/app/agent/core/capability.py: route authorization, translations, templates, test conventions, configuration risks.
- A plugin that lacks a capability leaves the related category partial with a stated reason, not silently clean.
- Accepted by the user on 2026-11-16, to meet the requirement that frameworks can be added later without changing checks.
