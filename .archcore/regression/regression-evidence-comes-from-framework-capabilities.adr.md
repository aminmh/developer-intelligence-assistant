---
title: "Regression Evidence Comes from Framework Capabilities; the Core Is Language-Neutral"
status: draft
tags:
  - "api"
  - "code-quality"
---

## Context

The regression check must work across languages, not only PHP. The accepted three-layer decision (@.archcore/code-quality/framework-knowledge-lives-in-plugins-over-language-facts.adr.md) puts language facts in parsers, framework knowledge in plugins with capabilities (@api/Python-Service/app/agent/core/capability.py), and keeps checks framework-neutral. Today `ROUTE_ANALYSIS` and `TEST_CONVENTIONS` exist; test generation in @api/Python-Service/app/code_quality/rule_assist.py `generate_tests` is PHP-only (`_framework`).

## Decision

1. The regression core reads only normalized records: symbol graph rows, route records, test records, contract versions and HTTP responses.
2. Three new capabilities are added: `PROBE_HINTS` (parameter sources as `ParamHint` records), `CHARACTERIZATION_TESTS` (runner, layout, golden-master template), `RUN_HINTS` (env, services, migrations to start the app).
3. A missing capability makes the affected evidence `partial` with the capability named, never silently clean.
4. Without `CHARACTERIZATION_TESTS`, route tests are emitted as a runner-neutral HTTP request file.
5. The first providers are the Laravel and Symfony plugins; the core's tests include one non-PHP fixture project.

## Alternatives

- **PHP first, generalize later.** Rejected by the user: the core would grow PHP assumptions that are costly to remove.
- **Model-only inference per language.** Rejected as the only source: unbounded and uncited; the model still proposes probe values from `PROBE_HINTS`.

## Consequences

- Adding a framework means one plugin providing some capabilities; the check picks it up.
- Telemetry-tier evidence stays PHP-only until other languages have an adapter with the same span attributes.
- Requested by the user on 2026-11-22.
