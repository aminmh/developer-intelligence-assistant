---
title: "Remote Documentation Provider: DeepWiki MCP or GitMCP"
status: accepted
tags:
  - "api"
  - "code-quality"
---

## Goal

Choose the remote source of up-to-date framework documentation for the knowledge layer (@.archcore/knowledge-layer/framework-knowledge-layer.spec.md). Context7 is ruled out because it is not free (user, 2026-11-23). Candidates: DeepWiki MCP (Cognition) and GitMCP (idosal/git-mcp).

## Questions

1. Is it free, and does it need an account or key?
2. Can it serve the version a project has installed, not only the default branch?
3. Does it return source text we can cite, or a model-written answer?
4. Can it run inside our network, given local mode and the egress allowlist (@.archcore/agent-tools/mcp-servers-reach-only-allowlisted-hosts-through-egress-proxy.adr.md)?
5. Does any repository work, or only pre-indexed ones?
6. What leaves the machine, and what does the service keep?

## Approach

Read the official docs and repositories, cross-checked against directory listings and issue trackers, on 2026-11-23. No live calls were made from this app.

## Findings

| Question | DeepWiki MCP | GitMCP |
|---|---|---|
| 1. Cost, auth | Free, no auth, public repositories only (Devin docs, Cognition blog). | Free, no auth; hosted at gitmcp.io (README). |
| 2. Versions | Wiki built from the default branch at index time; can lag (one issue reports 846 commits stale; re-index is manual and reCAPTCHA-gated). No evidence of tag or release selection. | No branch, tag or ref selection in URLs or tool arguments (README). Fetches on request, so it follows the default branch without an index lag. |
| 3. Output | `ask_wiki_question` (legacy `ask_question`) returns a model-written answer from the generated wiki; `read_wiki_structure`, `read_wiki_contents` return wiki pages, themselves generated. | Returns repository text: `fetch_<repo>_documentation` (llms.txt, then docs, then README), `search_<repo>_documentation`, `search_<repo>_code` (GitHub code search), `fetch_url_content`; generic `gitmcp.io/docs` variants take the repository as an argument. |
| 4. Self-host | Not possible; hosted service only. Open-source look-alikes exist (deepwiki-open, OpenDeepWiki) but are different products needing our own model. | Apache License 2.0; "can be deployed independently in your environment". Repository files point to Cloudflare Workers (wrangler); local runtime is an [assumption: `wrangler dev` / workerd]. |
| 5. Coverage | Only indexed repositories; otherwise "Repository not found. Visit deepwiki.com/<repo> to index it". | Documentation fetch works for any public GitHub repository. Corrected 2026-11-23 after live calls: search needs GitMCP's own index; unindexed repositories (`symfony/symfony`, `laravel/docs`) answer "No relevant documentation found ... either being indexed" with the README as a fallback. |
| 6. Data | Questions go to Cognition's model; retention not stated in the sources read. Throttling of high-frequency calls reported by one secondary source [unverified]. | README: stores no queries and no personal data, does not keep repository content; counts tool calls per repository for its badge. |

Activity: GitMCP shows 8.5k stars and 280 commits on GitHub; last commit date was not visible. DeepWiki is maintained by Cognition.

## Recommendation

**GitMCP**, for these reasons:

- It returns the repository's own text, which the layer can cite by path. DeepWiki returns generated prose, which cannot serve as evidence in a regression report.
- It is open source and self-hostable, so it can sit behind the egress allowlist and keep working if the hosted service changes terms. DeepWiki cannot be self-hosted.
- It needs no indexing step and has no index lag. DeepWiki fails on unindexed repositories and can be weeks stale.
- Less leaves the machine: a repository name and search words, with no third-party model in the path.

Neither candidate serves a specific version. GitMCP is therefore one provider among three, used last: installed packages in the revision come first (exact), then repository files read at the installed tag, then GitMCP for discovery on the default branch, labelled as unversioned.

DeepWiki stays a possible later addition for conceptual "how does this framework work" questions, never as cited evidence.

## Next Action

- Done 2026-11-23: the user accepted GitMCP; recorded in @.archcore/knowledge-layer/gitmcp-is-the-remote-documentation-provider.adr.md.
- Start with the hosted gitmcp.io on the allowlist; self-hosting is a later task once the runtime is confirmed.

Sources (read 2026-11-23):
- https://docs.devin.ai/work-with-devin/deepwiki-mcp
- https://cognition.com/blog/deepwiki-mcp-server
- https://github.com/CognitionAI/deepwiki/issues/20
- https://github.com/pjt222/agent-almanac/issues/636
- https://benchclaw.io/deepwiki-mcp/
- https://mcp.directory/blog/deepwiki-mcp-complete-guide-2026
- https://repowise.dev/blog/comparisons/deepwiki-alternatives
- https://github.com/idosal/git-mcp (README and LICENSE)
- https://github.com/idosal/git-mcp/blob/main/wrangler.jsonc
