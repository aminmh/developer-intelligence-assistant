# Developer Assistant — architecture map

Python backend (`api/Python-Service/app`) + Vue 3 frontend (`web`). Backend runs
in-container with `/app` = repo `api/Python-Service/app` (bind mount, no rebuild
needed); web prod image is NOT bind-mounted (rebuild to ship changes); `web-dev`
profile hot-reloads.

## Python package layout (domain-first)

`app` is an importable package; `main.py` + packages only (no loose modules):

```
app/
  main.py                 # FastAPI routes and router registration
  infra/                  # cross-cutting: settings (env+dynamic), settings_store
                          #   (KV sqlite), exception_logger, php_tools,
                          #   providers.py (resource Protocols + registry)
  rag/                    # retrieval-answer domain: answer, code_validation,
                          #   doc_retrieval, intent, query_normalizer, router,
                          #   domain_dictionary, evaluation
  index/                  # ingestion/vector/graph: indexer, llamaindex_layer,
                          #   graph_store, embeddings, models, storage
  projects/               # project knowledge: project_profile (detect+analyze),
                          #   project_knowledge (canonical grounding renderer),
                          #   project_context (knowledge chunks), knowledge_cache,
                          #   llm_detect (LLM fallback probe), dependencies
  analysis/               # saved analyses: analysis_store (sqlite sessions +
                          #   artifacts), analysis_service (bundle IO, persist,
                          #   Qdrant/Neo4j knowledge link)
  pipelines/              # pipelines/steps.py (declarative step plan + runner),
                          #   orchestrator.py (job lifecycle + capabilities)
  runtime/                # LLM clients: llm, lmstudio
  telemetry/              # opt-in CI source snapshots, canonical PHP symbol
                          #   identity, registry and authenticated upload router
  agent/                  # framework/language plugin registry + Laravel/php
                          #   plugins (knowledge extraction stays here)
  lsp/                    # language-server adapters (code validation back-end)
  config/                 # YAML: orchestrator.yml, lsps.yml
  tests/                  # pytest suite
```

## Dependency rules

- **Top-down only.** `main.py` → packages; packages import other packages only
  along the flow: `pipelines → {rag, index, projects, analysis, runtime}`;
  `projects`/`analysis` never import `main`; nothing imports `pipelines`.
- **No main-app module imports framework/language internals.** Framework
  resolution is reached through the canonical-profile contract
  (`project_knowledge.py` renderer) and generic adapters (`lsp`, `agent` registry,
  `infra/providers.py`). Main-app code never names a framework.
- **`infra/` is importable by everyone, imports nothing app-level.** `settings`,
  `exception_logger`, `providers` (Protocols only).
- Data/config paths derive from `__file__` (`Path(__file__).parent.parent / …`),
  never from cwd assumptions.

## Interfaces (abstraction points)

| Contract | Where | Consumed by |
|---|---|---|
| Canonical project profile | `project_knowledge.py` (dict schema, `build_grounding`) | answer prompt, chunk builder |
| Step plan / SharedState | `pipelines/steps.py` | orchestrator `_run_job` |
| LlmProvider / EmbeddingProvider / DocRetriever | `infra/providers.py` | provider callers |
| FrameworkPlugin / LanguagePlugin | `agent/interfaces/*` | `agent/registry.detect` + analyze |
| LanguageAdapter | `lsp/base.py` | generic `lsp.registry.validate` |
| CI source snapshot | `telemetry/snapshot.py`, `telemetry/registry.py` | authenticated CI upload, static graph/vector indexing |
| Browser identity and roles | `web/src/server/oidcAuth.ts` | OIDC PKCE, persisted sessions, project membership and API authorization |
| User upload identity | `index/storage.py`, `index/upload_service.py` | MinIO object tags for project, commit, and exact archive checksum |

## Cross-cutting invariants

- **Honest absence**: features that cannot be verified (no vendor, no egress,
  no plugin) report `unavailable`/`not detected` with a reason — never guesses.
- **Best-effort, non-fatal**: doc retrieval, LLM detection, LSP validation,
  the legacy graph link pass degrade to `None`/warnings; the answer
  pipeline never fails on an ancillary service.
- **Snapshot publication is strict**: exact source parsing plus graph, vector
  and inspection persistence must succeed before `ready`. Snapshot IDs are
  scoped by project/commit/canonical symbol. Legacy saved analyses are not
  migrated or assigned guessed commits.
- **Secret discipline**: keys read from env only at call time; UI values masked;
  config extraction renders `env:KEY`; embedding/profile payloads redact
  secrets defensively.
- **Deterministic ids**: knowledge chunks `kn:<kind>:<project>`; qdrant point id
  = uuid5(URL, node id). Legacy graph IDs retain their historical hashes;
  snapshot graph IDs use SHA256(compact JSON [project, commit, symbol]).
- **Lazy singletons** everywhere stateful (provider registry, executor, store)
  so module import is side-effect free.
- **Browser access fails closed**: the web API requires an OIDC session. Upload and indexing writes
  require an operator or admin membership for the request's project; legacy uploads stay unattributed.

## Web layout

```
web/src/
  main.ts, App.vue        # OIDC sign-in, query-synced tab shell, chat, files, analyses, schema, domain, config
  types.ts                # PascalCase types mirroring snake_case payloads
  components/             # RAGChat, RAGFiles, RAGConfig (thin parent), config/*
                          #   (one card per section), saved analyses, ...
  components/config/*.vue # LlmProviders, ExternalModel,
                          #   VectorStore, DocRetrieval
  ui/makeRequest.ts       # VITE_API_URL-aware fetch (multi-consumer)
web/server.ts             # express: proxying + orchestration glue (route map header)
```

The former Sandbox executor/runtime is retired. Application container management
is outside telemetry scope. The optional `api/docker-compose.telemetry.yml`
provides a separate Collector/ClickHouse/Neo4j development stack. Phase 0 stores
sanitized raw spans only; discovery, app instrumentation and runtime aggregation
remain later review-gated phases. See `api/docs/telemetry/` for contracts.
