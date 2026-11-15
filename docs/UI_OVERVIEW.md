## 1. Summary

Indexr’s web UI is a Vue single-page application for asking questions about uploaded code, managing source archives, and inspecting saved analyses and indexed PHP definitions. It is used by developers operating the local Developer Assistant stack; the main workflow is to upload/attach a ZIP, query it in chat, and browse related analysis and symbol data. The same shell also exposes RAG settings, developer logs, a database schema viewer, and domain knowledge views. Tabs are selected in `web/src/App.vue`; the UI calls the Express API in `web/server.ts`, which proxies most work to FastAPI.

## 2. Tech stack

| Area | Technology | Version | Where configured |
|---|---|---:|---|
| Framework | Vue | 3.5.39 | `web/package.json`, `web/src/main.ts` |
| Language | TypeScript | ~5.8.2 | `web/package.json`, `web/tsconfig.json` |
| Build and dev server | Vite, Express, tsx | Vite 6.2.3; Express 4.21.2; tsx 4.21.0 | `web/package.json`, `web/vite.config.ts`, `web/server.ts` |
| Graph libraries | AntV X6 + `@antv/layout`; Vue Flow | X6 ^3.1.8; layout ^2.0.0; Vue Flow ^1.48.2 | `web/package.json`; `ErdCanvas.vue`, `EntityModelCanvas.vue`, `DomainKnowledgeView.vue` |
| UI component library | No general-purpose component library found; Lucide Vue icons | `lucide-vue-next` ^1.0.0 | `web/package.json`, component imports |
| State management | Vue Composition API (`ref`, `computed`, `watch`); no external state library found | Vue 3.5.39 | `web/src/App.vue`, `web/src/composables/` |
| Data fetching | Native `fetch` via `makeRequest` and direct `fetch` in some components | N/A | `web/src/ui/makeRequest.ts`, `web/src/server/rag.ts` |
| Styling | Tailwind CSS Vite plugin, utility classes, component-scoped CSS | Tailwind ^4.1.14 | `web/vite.config.ts`, `web/src/style.css`, Vue component templates |
| Testing | Node test runner through tsx | tsx ^4.21.0 | `web/package.json` (`npm test`), `web/tests/` |

## 3. Pages and sections

The application has one URL route, `/`, and seven conditional tab sections rather than a route-per-page router (`web/server.ts`, SPA fallback; `web/src/App.vue`, `activeTab` and tab conditionals).

### Interactive Chat

- **Route and entry component:** `/` → `web/src/App.vue` renders `web/src/components/RAGChat.vue`.
- **Purpose:** Ask questions against an attached code archive or available indexed collections.
- **What it displays:** Conversation, message status and pipeline progress, retrieved matches/context preview, code validation, feedback, optional retrieval insights and evaluation modal (`App.vue`, `RAGChat.vue`, `PipelineProgress.vue`, `RetrievalInsights.vue`, `EvaluationModal.vue`).
- **User actions available:** Send prompts, attach/select/clear a project archive, preview or confirm context depending on configured preview mode, provide feedback, inspect retrieval insights, evaluate an answer (`App.vue`, `RAGChat.vue`).
- **Data sources:** `POST /api/query`, `POST /api/query/result`, `POST /api/query/stream`, `POST /api/feedback`, `GET /api/query/:queryId/insights`, `POST /api/query/:queryId/evaluate`; API handlers are in `web/server.ts`, Python answer/retrieval handlers in `api/Python-Service/app/main.py`.
- **Screenshot:** Not captured; see section 8.

### Knowledge Base (Files)

- **Route and entry component:** `/` → `web/src/App.vue` renders `web/src/components/RAGFiles.vue`.
- **Purpose:** Manage ZIP archives used by chat and indexing.
- **What it displays:** Uploaded file list, upload/index/parse status, and progress (`RAGFiles.vue`, `web/src/composables/useProjectFiles.ts`).
- **User actions available:** Upload, select/attach, index/re-index, parse, and delete files; large uploads use resumable TUS (`useProjectFiles.ts`, `web/src/services/tusUpload.ts`).
- **Data sources:** `GET /api/files`, `POST /api/upload`, `POST /api/rag/upload`, `POST /api/rag/parse-code`, `DELETE /api/files/:id`; status polling includes the Python download-job status path. Express handlers in `web/server.ts`; Python archive handlers in `api/Python-Service/app/main.py`.
- **Screenshot:** Not captured; see section 8.

### Connection & Settings

- **Route and entry component:** `/` → `web/src/App.vue` renders `web/src/components/RAGConfig.vue`.
- **Purpose:** Configure model providers, retrieval, UI appearance, and LLM tools.
- **What it displays:** Appearance, Models & Providers, Retrieval & Context, and LLM Tools sections composed from `web/src/components/config/*.vue`.
- **User actions available:** Edit settings and save; test provider/document retrieval connections; list Ollama models; configure tools; change local appearance preferences (`RAGConfig.vue`, config cards, `useAppearancePreferences.ts`).
- **Data sources:** `GET /api/config`, `POST /api/config`; supporting endpoints include `/api/ollama/models`, `/api/lmstudio/models`, `/api/lmstudio/load`, `/api/health/*`, `/api/docretrieval/test`, and `/api/tools*` (`App.vue`, `web/src/components/config/*.vue`, `web/server.ts`, `api/Python-Service/app/main.py`).
- **Screenshot:** Not captured; see section 8.

### Developer Logs

- **Route and entry component:** `/` → `web/src/App.vue` renders `web/src/components/LogsView.vue`.
- **Purpose:** Review and clear developer log events.
- **What it displays:** Newest-first log cards for LLM prompts, exceptions, and router fallbacks, with expandable detail text and filters (`LogsView.vue`).
- **User actions available:** Search; filter by type, severity, and time; refresh or enable 10-second auto-refresh; load more; clear a type or all logs; choose soft/hard deletion (`LogsView.vue`, `ConfirmModal.vue`).
- **Data sources:** `GET /api/logs`, `DELETE /api/logs`, `DELETE /api/logs/:type`, `PUT /api/logs/config`; Python log router reads/writes log files and configuration (`web/server.ts`, `api/Python-Service/app/routers/developer_logs.py`, `api/Python-Service/app/infra/log_viewer.py`).
- **Screenshot:** Not captured; see section 8.

### Extracted Definitions

- **Route and entry component:** `/` → `web/src/App.vue` renders `web/src/components/DefinitionsView.vue`.
- **Purpose:** Browse definitions recovered into a project’s PHP symbol graph.
- **What it displays:** Project selector and grouped definition rows, with expandable FQN, visibility, static/abstract, owner, and source location fields (`DefinitionsView.vue`).
- **User actions available:** Choose a symbol-indexed project; search name/FQN/owner; filter kind and file; expand definition details; paginate in batches of 100; refresh (`DefinitionsView.vue`).
- **Data sources:** `GET /api/inspect/projects`, `GET /api/inspect/definitions`; Cypher and returned fields are in `api/Python-Service/app/inspection/queries.py`, service and route in `inspection/service.py` and `inspection/http.py`.
- **Screenshot:** Not captured; see section 8.

### Database Schema

- **Route and entry component:** `/` → `web/src/App.vue` renders `web/src/components/DatabaseSchemaView.vue`.
- **Purpose:** View a saved database schema as an entity-relationship diagram or import schema text locally for viewing.
- **What it displays:** X6 canvas of tables and references, warnings, selected table/reference detail panel, and import/source controls (`DatabaseSchemaView.vue`, `ErdCanvas.vue`).
- **User actions available:** Select a saved analysis; paste SQL, DBML, or normalized JSON; search tables/fields; select tables/relationships; zoom, fit, reset, prettify, export SVG/PNG/DBML/JSON, toggle the details panel, and delete a saved analysis (`DatabaseSchemaView.vue`, `ErdCanvas.vue`).
- **Data sources:** `GET /api/analysis?limit=100`, `GET /api/analysis/:id/schema-visualization`, `DELETE /api/analysis/:id`; SQL/DBML/JSON text imports are parsed client-side (`web/src/utils/erdTransformer.ts`). Python analysis persistence and response construction are in `api/Python-Service/app/main.py` and `app/analysis/analysis_store.py`.
- **Screenshot:** Not captured; see section 8.

### Domain Knowledge

- **Route and entry component:** `/` → `web/src/App.vue` renders `web/src/components/DomainKnowledgeView.vue`.
- **Purpose:** Inspect saved analysis concepts and a deterministic or enriched entity model.
- **What it displays:** Concepts relationship graph (up to 80 graph concepts), concept/context detail, global vocabulary index, or an entity model canvas with entity, relationship, and behavior details (`DomainKnowledgeView.vue`, `EntityModelCanvas.vue`, `EntityModelNode.vue`).
- **User actions available:** Select saved analysis; search/filter concepts by type; select graph nodes; inspect contextual evidence and relationships; switch to entity model; enrich model; search/focus entities; select entities, relationships, or behaviors; load more usages; copy/download PlantUML; ask chat about a concept (`DomainKnowledgeView.vue`, `EntityModelCanvas.vue`, `App.vue`).
- **Data sources:** `GET /api/analysis?limit=100`, `/api/domain/dictionary`, `/api/analysis/:id/domain-concepts`, `/domain-context`, `/domain-model`, `/domain-model/usages`; `POST /api/analysis/:id/domain-model/enrich`. Routes and persistence/query logic are in `api/Python-Service/app/main.py`, `app/analysis/analysis_store.py`, and `app/projects/domain/store.py`.
- **Screenshot:** Not captured; see section 8.

## 4. Graph visualization

- **Libraries and components:** AntV X6 powers schema and entity-model canvases (`web/src/components/ErdCanvas.vue`, `EntityModelCanvas.vue`); `@antv/layout` Dagre arranges them (`web/src/utils/erdTransformer.ts`, `entityModelLayout.ts`). Vue Flow renders the domain concept graph in `DomainKnowledgeView.vue`.
- **Schema graph node and edge types:** Nodes represent database tables; each contains fields/columns with type, PK/FK, and nullable indicators (`ErdCanvas.vue`, `ErdTableNode.vue`). Edges represent supported foreign-key references and connect source/target field ports (`web/src/utils/erdTransformer.ts`). Labels show cardinality. N:M strokes are yellow, 1:1 green, and other cardinalities indigo; selected edge width increases (`ErdCanvas.vue`).
- **Entity-model graph node and edge types:** Nodes represent domain entities and show bounded property/behavior summaries (`EntityModelNode.vue`). Edges represent entity relationships; labels include relationship/cardinality metadata (`EntityModelCanvas.vue`). Exact backend relationship taxonomy is data-driven; no exhaustive fixed list is declared in the renderer.
- **Concept graph node and edge types:** Nodes are domain concepts or related concepts with concept type and optional roles; edges are returned concept relationships and are labelled from relationship metadata (`DomainKnowledgeView.vue`). The graph concept list is capped to 80 client-side. The concept template does not declare a fixed node color map by type; selected styling is a cyan drop shadow.
- **Interactions and filters:** X6 node/edge click selects table/reference or entity/relationship/behavior; canvases support zoom/fit/reset/layout, drag, and configured panning. Schema search hides nonmatching tables and edges whose endpoints are hidden. Entity search selects/focuses matches. Vue Flow supports draggable but not connectable nodes; click selects a concept. Concept search/type filters and entity model search are available (`ErdCanvas.vue`, `DatabaseSchemaView.vue`, `EntityModelCanvas.vue`, `DomainKnowledgeView.vue`).
- **Loading and limits:** Schema graph loads one saved visualization response; local schema input is fully parsed in-browser. Entity model loads as one API response. Concept graph loads returned concepts/relationships and keeps at most 80 graph concepts. No general graph paging, expand-on-demand, or node-count safeguard is visible in these renderers. Entity cards display at most 14 properties and 10 behaviors (`EntityModelNode.vue`).
- **Detail panels:** Schema selection detail is in `DatabaseSchemaView.vue`; entity, relationship, and behavior details are in `DomainKnowledgeView.vue`; concept details/context are in that same component’s concept mode.

## 5. Data flow

### Backend API endpoints

The UI-facing `/api/*` handlers are declared in `web/server.ts`; most proxy to `PYTHON_API_URL` (default `http://localhost:9002`). Python paths below are relative to the FastAPI service unless prefixed `/api`.

| Method | Path | Purpose | Storage queried |
|---|---|---|---|
| GET | `/api/config` | Load settings | Python settings store (SQLite; `main.py`, `infra/settings_store.py`) |
| POST | `/api/config` | Save settings | Python settings store (SQLite) |
| GET | `/api/files` | Merge tracked and stored uploads | Node `web/data/files.json`; Python `/api/rag/files` backed by MinIO (`web/src/server/rag.ts`, Python `main.py`) |
| POST | `/api/upload`, `/api/rag/upload` | Upload or create resumable upload session | MinIO via Python RAG upload service (`web/server.ts`, `app/index/upload_service.py`) |
| POST | `/api/rag/parse-code` | Parse/index uploaded source | Python PHP parser, Qdrant and Neo4j through indexing service (`app/main.py`, `app/index/indexing_service.py`, `app/index/llamaindex_layer.py`) |
| DELETE | `/api/files/:id` | Remove upload record/archive | Node file tracking and Python MinIO upload (`web/server.ts`, `app/main.py`) |
| POST | `/api/query`, `/api/query/result`, `/api/query/stream` | Start/query/poll or stream RAG response | Qdrant vector retrieval and Neo4j graph expansion (`web/server.ts`, `app/main.py`, `app/index/llamaindex_layer.py`) |
| POST | `/api/feedback` | Record route feedback | Qdrant feedback collection (`app/main.py`, `app/rag/router.py`) |
| GET; POST | `/api/query/:queryId/insights`; `/api/query/:queryId/evaluate` | Retrieval trace and response evaluation | In-memory per-response cache; retrieval/evaluation services (`web/server.ts`, `app/main.py`) |
| GET | `/api/inspect/projects` | List indexed symbol projects | Neo4j `PhpSymbol` nodes (`app/inspection/queries.py`) |
| GET | `/api/inspect/definitions` | Filter/paginate symbols | Neo4j `PhpSymbol` nodes via Cypher (`app/inspection/queries.py`) |
| GET | `/api/analysis?limit=100` | List saved analysis sessions | Analysis SQLite store (`app/analysis/analysis_store.py`) |
| GET | `/api/analysis/:id/schema-visualization` | Fetch saved schema visualization | Saved analysis payload in SQLite (`app/main.py`, `app/analysis/analysis_store.py`) |
| DELETE | `/api/analysis/:id` | Delete saved analysis | Analysis SQLite store |
| GET | `/api/domain/dictionary` | Read vocabulary counts | Domain dictionary file (`app/main.py`, `app/rag/domain_dictionary.py`) |
| GET | `/api/analysis/:id/domain-concepts`, `/domain-context`, `/domain-model`, `/domain-model/usages` | Browse concepts, context, entities, and usages | Analysis payload and project domain store; symbol usages query indexed graph (`app/main.py`, `app/projects/domain/store.py`, `app/inspection/queries.py`) |
| POST | `/api/analysis/:id/domain-model/enrich` | Enrich saved entity model | Analysis store and configured LLM provider (`app/main.py`, `app/analysis/analysis_store.py`) |
| GET | `/api/logs` | Read filtered developer logs | Log files (`app/routers/developer_logs.py`, `app/infra/log_viewer.py`) |
| DELETE | `/api/logs`, `/api/logs/:type`; GET/PUT `/api/logs/config` | Clear logs and manage deletion mode | Log files and log viewer settings |
| GET/POST | `/api/ollama/models` | Discover Ollama models | Ollama API (`web/server.ts`, Python `app/main.py`) |
| GET/POST | `/api/lmstudio/models`, `/api/lmstudio/load`, `/api/health/*`, `/api/docretrieval/test` | Provider/model health and retrieval tests | External provider APIs or dependency/document retrieval services (`app/routers/system.py`, `app/main.py`) |
| GET/POST/PUT/DELETE | `/api/tools*` | Configure and test LLM tools | Tool store (`app/infra/tool_store.py`, `app/main.py`) |

Other backend routes exist for indexing, evaluator runs, system monitoring, and telemetry snapshots/deployments; they are not called by the current UI (`web/server.ts`, `api/Python-Service/app/main.py`, `app/telemetry/http.py`).

### Key storage queries

- **Neo4j symbol browsing:** `inspection/queries.py` matches `PhpSymbol` by collection/FQN/name; project and definition lists use `MATCH`, filters, `ORDER BY`, `SKIP`, and `LIMIT`. References and call hierarchy use `REFERENCES`; implementations use `DECLARES` with `kind` `implements`/`extends` (`api/Python-Service/app/inspection/queries.py`).
- **Neo4j code graph:** Indexing creates code symbol nodes and `CALLS`, structural, and knowledge relations; retrieval queries neighboring nodes after vector hits (`api/Python-Service/app/index/llamaindex_layer.py`).
- **Qdrant RAG retrieval:** The query path embeds the query, searches relevant code/knowledge collections, optionally filters from extracted intent, merges results, then expands graph neighbors; feedback routing uses a separate feedback collection (`app/index/llamaindex_layer.py`, `app/rag/router.py`, `app/main.py`).
- **Saved analyses/domain data:** These are read from the SQLite analysis store; domain concepts may be read from the project domain store and source-analysis payloads (`app/analysis/analysis_store.py`, `app/projects/domain/store.py`, `app/main.py`).

### Graph data model as exposed

| UI view | Labels / types | Properties consumed by UI |
|---|---|---|
| Extracted Definitions | Neo4j `PhpSymbol`; relationships include `REFERENCES` and `DECLARES` in the inspection graph | `kind`, `name`, `fqn`, `file`, `line`, `end_line`, `visibility`, `static`, `abstract`, `owner`; project response also exposes `collection`, `stem`, `definitions` (`inspection/queries.py`, `inspection/service.py`) |
| RAG graph retrieval | `CodeSymbol`; `CALLS`, structural relations, `HAS_KNOWLEDGE` | Symbol payload includes identity/name/kind/file/line/code/signature and collection/project metadata used in returned matches (`app/index/llamaindex_layer.py`) |
| Schema diagram | UI-normalized tables, fields, and refs (not a Neo4j graph) | Table name/schema/fields; field name/type/null/default/PK/FK/unique; reference source/target table/column/cardinality/provenance (`web/src/types.ts`, `web/src/utils/erdTransformer.ts`) |
| Domain concept graph | API concepts and relationships; exact persisted label taxonomy is data-dependent | `identity_key`, `canonical_name`, `concept_type`, `roles`, relationship count; edges expose source/target identities and relationship label/evidence (`DomainKnowledgeView.vue`, `api/Python-Service/app/main.py`) |
| Entity model | API entities, behaviors, and relationships; renderer uses model JSON rather than direct Neo4j nodes | Entity name/id/namespace/table/description/properties/behaviors; relationships include source/target/name/cardinality/foreign key (`DomainKnowledgeView.vue`, `EntityModelNode.vue`) |

## 6. Shared UI structure

- **Layout/navigation:** Dark application shell with collapsible sidebar and seven buttons; the selected tab is held in `App.vue` state, not encoded in the URL (`web/src/App.vue`).
- **Reusable UI:** `ConfirmModal.vue`, `PipelineProgress.vue`, `ChatLoading.vue`, `WelcomeTerminal.vue`, `SysMonitor.vue`, `JsonEditorField.vue`, and shared config cards are used in their relevant views. Icons come from Lucide Vue (`web/src/components/`).
- **Project selection:** No single global project selector. Chat/files use the selected uploaded archive; definitions use indexed symbol collections; schema/domain pages select saved analysis sessions (`App.vue`, `useProjectFiles.ts`, `DefinitionsView.vue`, `DatabaseSchemaView.vue`, `DomainKnowledgeView.vue`).
- **Authentication:** No sign-in or user/session authentication UI was found in the frontend; API authentication requirements beyond the telemetry host-certificate flow are **Unknown** (no general frontend auth client found; `web/server.ts`, `web/src/`).
- **Settings:** RAG/provider settings load through `/api/config` and save through `/api/config`; appearance/sidebar preferences are stored in browser local storage (`App.vue`, `useAppearancePreferences.ts`).
- **Runtime/build serving:** Express serves Vite middleware during development and built assets with an SPA fallback in production (`web/server.ts`, `web/vite.config.ts`). The root `just` menu starts the full stack, and `just up-dev` starts its development configuration (`README.md`).

## 7. Extension points

- **Node detail surfaces:** Existing schema table/reference panels and domain entity/relationship/behavior panels render selected-object properties; they are defined in `DatabaseSchemaView.vue` and `DomainKnowledgeView.vue`.
- **Badges and tags:** Definition kind labels, concept type/role tags, entity enrichment status, and log type/severity treatments already appear in `DefinitionsView.vue`, `DomainKnowledgeView.vue`, and `LogsView.vue`.
- **Filters:** The definitions toolbar has search/kind/file controls; logs has search/type/severity/time filters; schema has table/field search; domain has concept/entity search and type filters (`DefinitionsView.vue`, `LogsView.vue`, `DatabaseSchemaView.vue`, `DomainKnowledgeView.vue`).
- **Tabbed sections:** Settings uses a section selector and config-card composition (`RAGConfig.vue`, `web/src/components/config/`).
- **Graph attachments:** Schema and domain views already render distinct node/edge models and expose selection callbacks (`ErdCanvas.vue`, `EntityModelCanvas.vue`, `DomainKnowledgeView.vue`).
- **Chat attachment/context:** Chat messages include retrieved matches, validation, context, insights, and evaluation affordances (`App.vue`, `RAGChat.vue`, `RetrievalInsights.vue`, `EvaluationModal.vue`).

## 8. Open questions

- **Screenshots:** Not captured. The documented full local startup requires Docker services (`README.md`); checking `docker compose -f api/docker-compose.yml ps` failed because this environment could not access the Docker socket (`permission denied`), so the backend/data-dependent pages could not be started through the documented stack.
- **Authentication:** Whether API routes other than telemetry host reports enforce authentication is **Unknown** from the UI code; no general browser auth flow was found.
- **Schema size handling:** No explicit table/field/relationship count limit or large-schema paging behavior is visible in the schema UI; backend limits are **Unknown**.
- **Entity graph bounds:** Cards cap displayed properties and behaviors, but an entity/relationship count cap or graph paging behavior is **Unknown**.
- **Telemetry integration:** Telemetry snapshot and deployment routes exist in the backend, but the current frontend has no calls to those routes (`api/Python-Service/app/telemetry/http.py`, `web/src/`); intended telemetry UI placement is therefore not determined by existing code.
- **Route-map mismatch:** The route-map comment in `web/server.ts` describes some route families with older `/api/settings` and `/api/answer` naming while active UI calls use `/api/config` and `/api/query/*`; treat the implemented handlers and component calls as authoritative.
- **Package metadata:** `web/package.json` is named `react-example` and includes React dependencies, but the app entry and views use Vue; whether the React dependencies are still needed is **Unknown**.
