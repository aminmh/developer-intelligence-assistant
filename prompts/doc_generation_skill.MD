# Documentation Generation Agent Prompt

## Goal
Build a documentation pipeline that generates **README**, **API reference**, and **architecture overview** for an indexed project, using both the Qdrant knowledge base and the raw code tree.

## Output formats
- **README / architecture**: Markdown files
- **API reference**: OpenAPI/Swagger JSON (only if the project exposes an HTTP API — detect from routes, routers, or config)

## Architecture

Laravel UI ──/api/docs/generate─────► Python RAG service
                                        │
                                        ├─ Read Qdrant (project context)
                                        ├─ Read raw file tree (source)
                                        └─ LLM pipeline (3 tasks)
                                             │ README
                                             ├ Architecture
                                             └ API reference (OpenAPI)


## Inputs

- **project_root** — selected project path (same one indexed for Qdrant)
- **scope** — which docs to generate (all / readme / architecture / api)
- **output_mode** — `files` (write into repo) or `download` (return as response, write nothing)

## Phase 1 — Index & corpus assembly

- Pull the top-N relevant chunks from Qdrant per doc section (query = e.g. "project structure", "API endpoints", "how services communicate").
- **Fallback**: if Qdrant returns poor coverage (below a similarity threshold), fall back to reading files directly from the project tree — `composer.json`/`package.json`, entrypoints, route definitions, controllers, service classes, migrations.
- Log which source (Qdrant vs raw) was used for each section, and add to the eval log for Ragas (like the query-eval flow).

## Phase 2 — Generation (three parallel tasks)

Run three independent LLM tasks (parallelize if the backend supports it, else sequential):

### Task A — README
Input: chunks + file list. Output: README.md with
- Project title & one-line description
- Getting started (install, run, env vars)
- Basic usage example
- Project structure overview (from file tree)

Constraints: markdown, no placeholders like `TODO` or `<...>`. Every section must come from real project data.

### Task B — Architecture overview
Input: chunks + `composer.json`/`package.json` + entrypoints. Output: `docs/ARCHITECTURE.md`
- High-level component diagram (ASCII, mermaid optional)
- Data flow description
- Service/module boundaries

### Task C — API reference
Input: route definitions (Laravel `routes/`, Symfony annotations/attributes, FastAPI routers) + controller/service code. Output: `openapi.json` (valid Swagger 2.0 or OpenAPI 3.0)

- Detect framework from the file tree before generating (don't assume).
- Map HTTP method → path → controller/method → request/response types from code where inferable.
- Mark unresolvable endpoints as `"description": "Manually verify — could not infer from code"`.

## Phase 3 — Validation (gates before writing)

Run every generation through checks before it's accepted:

1. **Markdown lint** (README/ARCHITECTURE) — no broken headings, valid code fences.
2. **Swagger validation** — `openapi.json` must pass online validator / `swagger-cli validate`, or fail the task (don't write invalid swagger into a project).
3. **Content check** — LLM self-review pass: does each claim trace back to a source chunk/file? Flag hallucinated sections for manual review.
4. **No-overwrite guard** — never blindly overwrite an existing README; if one exists, write to `README.generated.md` or prompt the user.

## Phase 4 — Delivery

- `output_mode=files`: write files directly into the repo (respecting the structure above).
- `output_mode=download`: return `{readme, architecture, openapi_json}` in the response for the UI to render/download.

## Endpoints (add to RAG service)

POST /api/docs/generate
  body:  { project_root, scope, output_mode }
  res:   202 { job_id }             → async
GET  /api/docs/status/{job_id}
  res:   { status, progress, result: {files[] | download_payload} }


Use the same async job pattern you already have for evaluation — reuse that machinery.

## System constraints
- All LLM calls go through Ollama locally — keep prompts/tokens modest; stream chunks if the model is slow.
- Python service owns generation + validation; Laravel only proxies.
- Filesystem writes happen on the **same machine** that runs the RAG service (host-mounted volumes). Airgap from the Laravel container.
- Log every generation into the Ragas eval store so you can measure doc quality offline against your ~50-question fixture later.

## Test plan
1. Fixture: point the service at one of your own repos → all 3 docs generate, swagger validates, no placeholders.
2. Second repo with no HTTP layer → API ref must gracefully report "no endpoints detected", not crash.
3. Repo where Qdrant has never indexed it → raw fallback path fires, same output quality.
4. Overwrite test: existing README → `README.generated.md`, no loss.
5. Empty/edge: tiny repo (2 files) → docs still generate, sections clearly state what's absent.

## Commit
Commit as one feature branch (`feature/docs-generation`). Run PHPStan on the Laravel side changes, and the Python service through `ruff`/`mypy` if configured.

---

Worth noting before you start: **Task C (API reference)** is the highest-risk piece — reliably extracting endpoints from code is the hard 20%. If you want, I'd do it as a two-step (extract routes with a small deterministic parser, then let the LLM only *describe* them) rather than asking the LLM to do extraction+description in one pass. Much more accurate, easier to validate.

Want me to expand any of the three tasks, or sketch that two-step extractor for the API reference?