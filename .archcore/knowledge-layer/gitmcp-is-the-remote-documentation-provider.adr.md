---
title: "GitMCP Is the Remote Documentation Provider, Tried After Version-Exact Sources"
status: accepted
tags:
  - "api"
  - "code-quality"
---

## Context

The knowledge layer (@.archcore/knowledge-layer/framework-knowledge-layer.spec.md) needs up-to-date framework documentation for the agent and the regression check, and Context7 is not free. The comparison in @.archcore/knowledge-layer/remote-documentation-provider.rnd.md found that DeepWiki MCP returns model-written answers from a generated wiki, is hosted only, covers indexed repositories only, and can lag the default branch. GitMCP returns the repository's own files and search results, is Apache 2.0 and self-hostable, and works for any public GitHub repository. Neither can serve a specific branch, tag or version, so a remote provider alone cannot give version-exact answers.

## Decision

1. GitMCP is the only remote documentation MCP provider. DeepWiki MCP and Context7 are not used.
2. The layer tries sources in a fixed order:
   1. the installed package inside the revision (`vendor/`, `node_modules/`);
   2. the framework's guides repository, named by its plugin, read at the matching version line (amended 2026-11-23);
   3. the library's repository files read at the installed version's tag;
   4. GitMCP.
3. GitMCP excerpts are always marked unversioned, naming the default branch.
4. The first implementation calls the hosted `gitmcp.io` through the host allowlist. A self-hosted instance replaces it once its runtime is confirmed.
5. Only repository names, versions, file paths and topic words are sent to GitMCP and GitHub.

## Alternatives

- **DeepWiki MCP.** Rejected: its generated answers cannot be cited as evidence, it cannot be self-hosted, unindexed repositories fail, and questions go to a third-party model.
- **Context7.** Rejected: not free.
- **GitMCP as the primary source.** Rejected: it reads the default branch only, so answers can describe a newer version than the project runs.

## Consequences

- Most answers come from version-exact sources that need no network. GitMCP covers discovery and gaps.
- The layer depends on a community project. Its Apache 2.0 license and self-hosting keep that risk bounded.
- Self-hosting needs a runtime check, because the repository points to Cloudflare Workers. [assumption]
- Remote documentation is on by default; a project can turn it off (user, 2026-11-23; spec setting `remote_docs_allowed`).
- Chosen by the user on 2026-11-23.
- Amended with the user's confirmation on 2026-11-23: Laravel keeps its upgrade guide in `laravel/docs` (branch `13.x`), not in `laravel/framework`, so a framework plugin may name a guides repository (`DOC_SOURCES`). That source is version-exact, so the rule stands: every exact source comes before GitMCP.
