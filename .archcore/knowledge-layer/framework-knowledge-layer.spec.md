---
title: "Framework Knowledge Layer: Version-Specific Documentation for the Agent and Checks"
status: accepted
tags:
  - "api"
  - "code-quality"
---

## Purpose & Scope

Normative for the knowledge layer: given a project revision, a library and a topic, return cited documentation excerpts for the version the project has installed. Dependents: the agent through a `framework_docs` tool; the regression check for upgrade notes and for prompts that write probes or tests (@.archcore/regression/regression-check.spec.md); the template version check discussed on 2026-11-23.

Covered: installed-version resolution, four providers in a fixed order, citations, caching, what may leave the machine, consent, failure.

Not covered: choosing upgrade targets, applying code changes, private repositories, DeepWiki and Context7 (@.archcore/knowledge-layer/gitmcp-is-the-remote-documentation-provider.adr.md).

## Surface

- Package @api/Python-Service/app/knowledge/: `service`, `cache`, `providers/vendor`, `providers/doc_source`, `providers/github_ref`, `providers/gitmcp`.
- Capability `DOC_SOURCES` with `DocSourceRecord(framework, package, repository, ref, upgrade_path, topics)`, provided by framework plugins: the repository that holds a framework's guides, and the branch pattern of each version line (`{major}.x`, `{major}.{minor}`).
- New capability `INSTALLED_PACKAGES` with `PackageRecord(ecosystem, name, version, repository, lockfile)`, provided by language plugins from lockfiles (`composer.lock`, `package-lock.json`, `poetry.lock`, `uv.lock`), per @.archcore/code-quality/framework-knowledge-lives-in-plugins-over-language-facts.adr.md.
- Providers, in order:
  - `vendor`: the installed package inside the revision archive (`vendor/`, `node_modules/`); `UPGRADE*.md`, `CHANGELOG*`, `README*`, docs folders.
  - `doc_source`: the framework's guides repository read at the installed version line for topics, or at each major line up to the target for upgrade guides, from `raw.githubusercontent.com`.
  - `github_ref`: files of `repository` read at the tag matching the installed version, from `raw.githubusercontent.com` / `api.github.com`.
  - `gitmcp`: remote MCP over HTTP (`gitmcp.io/docs` or a self-hosted instance), default branch only.
- Agent tool `framework_docs`, effect `read_only`, workflows chat, explain, debug, code_refactor.
- Request: `project_id`, `revision_id`, `library`, `topic`, optional `target_version`.
- Result: `library`, `installed_version`, `excerpts[]` (`provider`, `version_or_ref`, `path_or_url`, `line`, `text`, `retrieved_at`, `versioned`), `coverage` (`complete`, `partial`, `unavailable`), `reasons[]`.
- Project setting `remote_docs_allowed`, default on; a project administrator can turn it off.

## Normative Behavior

1. WHEN a request names a library, the knowledge service MUST resolve its installed version from the revision's `INSTALLED_PACKAGES` records.
2. WHEN no record names the library, the knowledge service MUST return coverage `unavailable` with the reason, without assuming a version.
3. WHEN the revision archive contains the installed package, the knowledge service MUST query the vendor provider before any remote provider.
4. WHEN a record names a repository and version, the github_ref provider MUST read files at that version's tag, never the default branch.
5. WHEN earlier providers lack the topic and remote documentation is allowed, the knowledge service MAY query GitMCP.
6. WHEN an excerpt comes from GitMCP, the knowledge service MUST mark it `versioned: false` and name the default branch.
7. WHEN an excerpt is returned, the knowledge service MUST cite its provider, version or ref, path or URL, and retrieval time.
8. WHEN a target version is given, the knowledge service MUST return upgrade notes covering each version between installed and target.
9. WHEN a project has turned remote documentation off, the knowledge service MUST use only the vendor provider and say so.
10. WHEN a remote provider is called, the knowledge service MUST send only repository names, versions, file paths and topic words.
11. WHEN the same library, version and topic repeat within the cache lifetime, the knowledge service MUST answer from the cache.
12. WHEN a caller puts excerpts into a model prompt, the caller MUST label them untrusted data and respect the size caps.
13. WHEN the agent tool returns excerpts, the tool runner MUST cite each with provider, version or ref, and path or URL.
14. WHEN a framework plugin names a guides repository for the library, the doc_source provider MUST read it at the installed or target version line.
15. WHEN the installed copy's notes reach the target's minor line, the knowledge service MUST NOT call remote providers for those upgrade notes.

## Constraints & Invariants

- Remote calls go only to allowlisted hosts (`gitmcp.io` or the self-hosted host, `raw.githubusercontent.com`, `api.github.com`), consistent with @.archcore/agent-tools/mcp-servers-reach-only-allowlisted-hosts-through-egress-proxy.adr.md. [assumption: the API calls the remote MCP endpoint itself; the isolated MCP tool runner is not needed for a remote server]
- No project source code, file content or user prompt text leaves the machine through this layer.
- Excerpts: at most 4,000 characters each, 20,000 per request; timeout 10 s per remote call. [assumption]
- Cache: tag-pinned and vendor excerpts are immutable and kept until the revision is removed; guides-repository excerpts expire after 7 days, because a version line's branch still moves; GitMCP excerpts expire after 24 h. [assumption]
- An optional GitHub token for rate limits lives in the OS credential store, never in settings or logs. [assumption]
- Excerpts never change a verdict on their own; they are evidence for a person or a model prompt.

## Failure Behavior

1. IF a remote provider times out or is refused, THEN the knowledge service MUST return the other providers' excerpts with coverage `partial` and the reason.
2. IF no tag matches the installed version, THEN the github_ref provider MUST return nothing and report the missing tag.
3. IF GitHub or GitMCP rate-limits a request, THEN the knowledge service MUST stop remote calls for that request and report it.
4. IF an excerpt exceeds its cap, THEN the knowledge service MUST truncate it and mark it truncated.
5. IF the lockfile is missing or unreadable, THEN the knowledge service MUST return coverage `unavailable` naming the lockfile.

## Conformance

An implementation conforms when every excerpt is cited with its version or marked unversioned, the vendor provider answers first, nothing but library identifiers and topic words leaves the machine, and a project that turns remote documentation off makes no remote calls.

Given pizza-builder (Laravel 12, `symfony/http-foundation` in `composer.lock`), When `framework_docs` asks for "upgrade" with target 8.0, Then excerpts cite the installed vendor files or the tag-pinned `UPGRADE-8.0.md`, each with its version.

Check map (`docker compose run --rm --no-deps python-api python -m pytest tests/<file> -k <name>`), file `test_knowledge_layer.py`:

- 1-2, F5: test_installed_version_from_lockfile_records
- 3, 5-6, 9: test_provider_order_and_consent
- 4, F2: test_github_ref_reads_at_tag
- 7, 13: test_excerpts_are_cited
- 8: test_upgrade_notes_between_versions
- 10: test_only_identifiers_leave_the_machine
- 11: test_cache_lifetimes
- 12, F4: test_prompt_caps_and_untrusted_label
- F1, F3: test_remote_failures_are_partial
- 14: test_doc_sources_upgrade_guide_for_each_major_line, test_doc_sources_topic_guides_at_the_installed_line
- 15: test_upgrade_notes_between_versions

## Clarifications

- 2026-11-23: the user asked for an up-to-date knowledge layer after a question on keeping framework test templates current; Context7 is out because it is not free.
- 2026-11-23: the user chose GitMCP over DeepWiki MCP (@.archcore/knowledge-layer/gitmcp-is-the-remote-documentation-provider.adr.md); comparison in @.archcore/knowledge-layer/remote-documentation-provider.rnd.md.
- 2026-11-23: amended with the user's confirmation after Phase 4 (api 1b10db2): a fourth, version-exact provider `doc_source` sits between `vendor` and `github_ref`, because Laravel keeps its upgrade guide in `laravel/docs`, not in `laravel/framework` (found live). Clause 15 lets the regression check read notes from the candidate revision's own installed copy without remote calls. Clauses 1-13 keep their numbers.
- 2026-11-23: the user chose `remote_docs_allowed` on by default. Rationale: only public library identifiers, versions and topic words leave the machine, never project code. A project can turn it off, for example on air-gapped installs.
