---
title: "Local Mode Fits an 8 GB Machine with Hosted Models"
status: accepted
tags:
  - "api"
  - "embeddings"
---

## Context

Local mode must stay usable on a developer laptop. A Team/Enterprise edition will later move shared dependencies to a dedicated server; the existing remote seams are the analyzer worker (@.archcore/code-quality/remote-analyzer-worker.spec.md) and remote Runtime ClickHouse (@.archcore/runtime-telemetry/runtime-clickhouse-remote-provisioning.spec.md). The installer today demands 8 GiB Docker memory and 4 CPUs (@scripts/installer/system.py) and always installs Ollama with `bge-m3` and `gemma3:4b` (@scripts/installer/config.py), even when hosted models are used.

Measurements on 2026-11-21/22, indexing skiplanner-api (2,185 files, 14,423 chunks) with Cloudflare Workers AI `bge-m3`, `docker stats` sampled every ~2 s:

- Idle stack: ~1.7 GiB (Neo4j 539 MiB, Keycloak 488, python_api 228, web-dev 217, Qdrant 100, MinIO 80).
- Before fixes: python_api and Neo4j sat at their 1 GiB caps (cgroup `max` events 1,761 and 4,951); graph build took ~36 min.
- Causes: no Neo4j index on `CodeSymbol.id` or `Symbol.id` (every MERGE/MATCH was a `NodeByLabelScan`); Neo4j heap 512m + page cache 512m filled its cap; vectors accumulated in memory before upsert; phpactor (~390 MiB) runs inside the API container beside ~450 MiB of chunks and hits its 900 s run timeout (@api/Python-Service/app/lsp/indexer.py).
- After fixes: python_api peak 1,132 MiB of 1,536 with 0 cap events, ~420 MiB after the job; Neo4j peak 951 MiB, 631 cap events; app peak ~2.4 GiB for indexing services, ~3.0 GiB with Keycloak and web. The developer's own project stack added 1.2–1.6 GiB during the run.

## Decision

- Local mode with hosted embedding and chat models targets an **8 GB machine with a 4 GiB Docker VM**; 16 GB is the recommended size.
- Container memory limits for that target: python_api 1536m (it hosts phpactor when no analyzer worker exists), Neo4j 1g with heap 384m and page cache 256m, Qdrant/MinIO 1g, tusd 512m (@api/docker-compose.yml).
- Neo4j lookups by id are always indexed: `code_symbol_id` (@api/Python-Service/app/index/llamaindex_layer.py) and `symbol_id` (@api/Python-Service/app/inspection/graph.py), created when each graph store opens.
- Indexing keeps memory flat: vectors upsert per size-capped batch while embedding continues, and each parse job ends with `release_memory()` (@api/Python-Service/app/index/indexing_service.py).
- Analyzers (SonarQube + worker, ~9 GiB of caps), local Ollama models and local Runtime ClickHouse are outside the 8 GB budget; they belong on the Team server or are opt-in locally.

Decided by the user on 2026-11-22.

## Alternatives

- **Keep the 1 GiB python_api cap and bound phpactor instead.** Rejected for now: a lower PHP memory limit loses LSP coverage, and the ADR @.archcore/agent-tools/language-servers-feed-the-symbol-graph-at-index-time.adr.md runs LSP in the API container when no worker exists.
- **Move LSP indexing off the laptop.** Deferred to the Team/Enterprise server, where the analyzer worker already provides it.
- **Advertise 16 GB as the minimum.** Rejected: the measured peak fits 8 GB, and a lower floor widens local adoption.

## Consequences

- The installer's 8 GiB Docker-memory floor can drop to ~4 GiB for a lean hosted-model profile once Ollama becomes optional; that installer change is not done yet.
- On 8 GB the margin is thin: a large developer project stack, browser and IDE share the remaining ~4 GB.
- Local LSP indexing still costs ~15 min per PHP revision of this size and returns partial results at the 900 s timeout [assumption: whether local mode shortens or skips it is an open product question].
- Neo4j keeps ~800 MiB resident after heavy writes; the JVM does not return heap to the OS.
- Measured with reused vectors on later runs; a fresh full embed with streamed upserts is covered by tests (@api/Python-Service/app/tests/test_index_memory_bounds.py), not by a live measurement.