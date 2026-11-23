---
title: "MCP Servers Reach Only Allowlisted Hosts Through an Egress Proxy"
status: accepted
tags:
  - "api"
---

## Context

LangChain spec clause 8 restricts MCP network access to configured hosts. Today @api/Python-Service/app/infra/tool_executor.py starts MCP servers with `docker run --network none`, so no MCP server can reach the internet.

## Decision

- MCP containers join an internal Docker network with no direct route out.
- That network's only exit is an egress proxy that allows HTTPS `CONNECT` to each tool's configured hosts.
- Each MCP container gets a per-tool proxy identity so the proxy applies only that tool's allowlist.
- A tool with an empty allowlist keeps `--network none`.
- Container limits stay as today: read-only filesystem, no capabilities, process and memory caps.

## Alternatives

- **No network.** Only offline MCP servers would work. Rejected.

## Consequences

- Network MCP servers (for example GitHub or documentation servers) become usable with bounded egress.
- Adds a proxy service to Compose and a host-allowlist field to managed tools (LangChain plan task 9).
- Proxy denials are reported to the agent as bounded tool errors.
- Decided by the user on 2026-11-10.
