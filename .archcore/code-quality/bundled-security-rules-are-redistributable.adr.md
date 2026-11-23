---
title: "Bundled Security Rules Must Be Redistributable: Psalm Taint Analysis for PHP, LGPL Rules Elsewhere"
status: accepted
tags:
  - "api"
  - "code-quality"
  - "licensing"
  - "security"
---

## Context

Plan task 9 (@.archcore/code-quality/close-assessment-gaps-with-panel-review.plan.md) bundles a maintained security ruleset instead of rules derived from one project's findings. The user stated on 2026-11-16 that Developer Assistant will be **offered to others**, so every bundled rule must be licensed for redistribution inside a product or service.

Licences checked 2026-11-16:

| Source | Licence | PHP | Fits a product offered to others |
|---|---|---|---|
| semgrep/semgrep-rules | Semgrep Rules License v1.0 (internal business use only, no distribution or service use) | yes | no |
| opengrep/opengrep-rules (archived) | LGPL 2.1 with Commons Clause (no selling) | yes | no |
| GitLab sast-rules `rules/gitlab/` | GitLab Enterprise Edition licence | no | no |
| GitLab sast-rules `rules/lgpl/` | LGPL-3.0 (Java, JavaScript, Kotlin, Objective-C, Swift) | no | yes, with notice and published modifications |
| GitLab sast-rules `rules/lgpl-cc/` | LGPL with Commons Clause | — | no |
| trailofbits/semgrep-rules | AGPL-3.0 | no | avoided (network-use obligations) |
| vimeo/psalm (taint analysis) | MIT, maintained | yes | yes |
| betterleaks/betterleaks (secrets in Git history; checked 2026-11-16) | MIT, actively released; Go dependencies MIT, BSD, ISC, Apache-2.0 and two MPL-2.0 modules | n/a | yes, with notice |
| gitleaks/gitleaks | MIT, but "feature complete" since May 2026: security patches only, rules no longer updated | n/a | yes, but its rules go stale |
| trufflehog | AGPL-3.0 | n/a | avoided (network-use obligations) |

The engine already shipped, Semgrep CE 1.181.0 (@api/Python-Service/app/code_quality/analyzers.py), is LGPL 2.1 and runs as a separate binary. The SonarQube integration (@.archcore/code-quality/external-static-analyzer-integrations.spec.md) is optional and uses the customer's own server, so it cannot be the default.

## Decision

- **PHP:** run Psalm's taint analysis on the analyzer worker with an app-owned configuration (no project `psalm.xml`, no project plugins, no Composer install, no project code executed). Taint sinks map to CWE (command, SQL, file path, HTML, header, SSRF, unserialize, eval) and to the Security category.
- **Framework taint sources and sinks** (request objects, query builders, templating) ship with each framework plugin as Psalm stubs, MIT-licensed or written in-house; for example the Laravel and Symfony Psalm plugins (MIT) or stubs derived from them. Loaded only when the plugin is detected.
- **Other languages:** bundle GitLab's `rules/lgpl/` Semgrep rules at a pinned commit, keep the LGPL-3.0 notice, and publish any modification.
- **Gaps** (for example JWT decoded without signature or claim verification, CWE-347): write small in-house Semgrep rules from the CWE definition and the library's documented API, never from one project's findings.
- **Secrets in Git history:** Betterleaks (from the Gitleaks authors) with its own bundled rules, pinned by version and SHA-256 at the newest release at least two weeks old (1.9.0 on 2026-11-16; 1.10.x and the 2.0 release candidates were newer), on the analyzer worker with an API fallback. Validation against providers stays off.
- **Context-dependent classes** (CWE-770 unbounded page sizes, CWE-307 missing attempt limits) stay with the budgeted review's checklist (plan task 21), not rules.
- Every bundled source is recorded in a third-party notice with licence, version and commit; coverage names the ruleset and commit that ran.

## Alternatives

- **Semgrep Registry rules:** best PHP coverage, but the licence forbids use in a product or service offered to others.
- **Opengrep rules:** archived and Commons Clause forbids selling.
- **Write a full PHP pattern pack in-house:** licence-clean but large to build and maintain, and pattern rules miss data flow that taint analysis follows.
- **Rely on SonarQube:** optional, needs the customer's server and consent to upload source; cannot be the bundled baseline.

## Consequences

- PHP security findings come from data flow rather than patterns, so "shell command built from a variable" is reported only when user input reaches it.
- Psalm is a new pinned tool on the analyzer worker (PHP, already present); without the project's `vendor/`, taint sources outside the framework stubs are missed and the report says so.
- Fewer non-PHP rules than the Semgrep Registry; coverage per language is reported rather than implied.
- A licence notice file and a pinned-commit update procedure become part of the release.
