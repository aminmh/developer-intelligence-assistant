---
title: "Project indexing source discovery and failure reporting"
status: draft
tags:
  - "api"
  - "frontend"
---

## Purpose & Scope

Project ZIP indexing selects application PHP source consistently and reports parsing, upload metadata, and embedding provider failures accurately. Cohere remains the user's selected embedding provider.

## Surface

- Shared PHP discovery: @api/Python-Service/php/src/Inspection/PhpFileFinder.php.
- Parse inputs: @api/Python-Service/php/src/Console/CodeParseInput.php, @api/Python-Service/php/src/CodeParser/WorkspaceTypeScanner.php, @api/Python-Service/php/src/CodeParser/CodeChunker.php.
- JSON output: @api/Python-Service/php/src/Console/CodeParseRenderer.php and @api/Python-Service/php/src/Console/CodeParseCommand.php.
- Python parser boundary: @api/Python-Service/app/infra/php_cli.py.
- Upload metadata: @api/Python-Service/app/index/storage.py.
- Embeddings: @api/Python-Service/app/index/embeddings.py and @api/Python-Service/app/index/llamaindex_layer.py.
- Job status: @api/Python-Service/app/index/indexing_service.py.

## Normative Behavior

1. WHEN scanning a project directory, PHP parse input discovery MUST use the shared PhpFileFinder exclusions.
2. WHEN scanning workspace types or directory chunks, the PHP scanners MUST use the shared PhpFileFinder exclusions.
3. WHEN JSON encoding fails, the PHP renderer MUST throw an encoding exception.
4. WHEN the renderer reports an encoding exception, the CLI MUST exit unsuccessfully with a diagnostic.
5. WHEN the parser returns blank output, the Python adapter MUST reject it as invalid output.
6. WHEN upload identity is persisted, storage MUST pass MinIO object Tags to the client.
7. WHEN upload tags are read, storage MUST decode the returned mapping without requiring a to_dict method.
8. WHEN parsing fails, the indexing service MUST persist a nonempty actionable error.
9. WHEN an embedding HTTP request fails, the embedding client MUST raise a typed error containing provider and HTTP status.
10. WHEN the vector indexer receives a typed provider error, the indexer MUST stop remaining batches.
11. WHEN a typed provider error reaches the job service, the service MUST preserve its safe message in job status.

## Constraints & Invariants

Directory scans retain project-relative source locations and deterministic sorting. The shared discovery policy excludes dependency, generated, and macOS metadata directories. Explicit single-file parsing remains available. Encoding failures never become empty successful output. Upload archive bytes and registered revision identity remain immutable. Provider errors exclude raw response bodies and credentials. Indexes missing embeddings remain unavailable for assessment; graph persistence alone is insufficient. Provider selection remains unchanged during error recovery.

## Failure Behavior

1. IF no indexable PHP chunks exist, THEN the service MUST explain the missing supported source instead of reporting an embedding failure.
2. IF a provider rejects authentication or access, THEN the embedding client MUST identify HTTP 401 or 403 with a configuration hint.
3. IF provider rate limits persist after bounded retries, THEN the embedding client MUST identify HTTP 429 with a quota hint.

## Conformance

Use an uploaded ZIP containing application PHP, dependencies, and macOS metadata. Confirm valid chunk JSON excludes dependency and metadata paths. Confirm failed encoding and blank output cannot report successful parsing. Verify upload identity tags round-trip through the installed MinIO SDK. Observe an actual embedding rejection preserved in terminal job status and verify that the revision remains unavailable. Full successful indexing additionally requires provider access.