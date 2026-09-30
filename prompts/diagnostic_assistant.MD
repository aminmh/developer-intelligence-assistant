Amin, here is the complete, integrated end-to-end production roadmap combining your PHP plugins, the dedicated non-blocking ingestion sidecar, Docker socket proxy metadata correlation, and your existing **Python + Node.js + Qdrant + Neo4j** AST stack.

Following the roadmap is the formal, cross-language **Diagnostic Event Standard (DES)** specification.

---

# Production Roadmap: Graph-Correlated Diagnostic Platform

```text
┌────────────────────────────────────────────────────────────────────────┐
│ Target Container (PHP-FPM / Laravel / Symfony)                         │
│  - DB::listen / Doctrine SQL Middleware                                │
│  - App Stack Trace Filtering (Application call site only)              │
│  - Non-blocking write to Unix Domain Socket (Timeout: 0ms)             │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │ /var/run/diag/telemetry.sock (Shared Volume)
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Ingestion Sidecar Daemon ("diag-collector" in Go/Python)               │
│  - In-memory Ring Buffer (zero disk I/O, dropped on overload)          │
│  - SQL Fingerprinting & Param Redaction (e.g. CRC32/MD5 hash)          │
│  - Enriches via Docker Socket Proxy: Container ID, Host Paths, Git SHA │
│  - Flushes micro-batches (e.g. every 500ms or 100 events)              │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │ HTTP POST / gRPC (/internal/v1/telemetry/ingest)
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Assistant Backend (Python + Node.js)                                   │
│  - Path Translator: maps container paths to host project paths         │
│  - N+1 & Anomaly Detector: groups by trace_id + fingerprint            │
│  - Graph Join: correlates call sites to Neo4j AST (:Method nodes)      │
│  - Semantic Enrichment: queries Qdrant for related repo patterns       │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Remediation & AI Interfaces                                            │
│  - MCP Server (Exposes graph-backed incident tools to Neovim/Cursor)    │
│  - Deterministic Git Diff Generator (Generates fixes with no halluc.)  │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Part 1: Comprehensive Production Roadmap

### Phase 1: The Diagnostic Event Standard (DES) Contract
*   Establish an immutable, language-agnostic data specification (JSON schema).
*   Enforce a shared boundary across any producer (Laravel, Symfony, FastAPI, Express) and any consumer (Collector, Stream Processor, AI Agent).
*   Standardize AST anchor points (`class`, `method`, `call_site`) so dynamic runtime traces map 1:1 to static AST representations.

### Phase 2: High-Performance Non-Blocking Emitters (Laravel & Symfony Plugins)
*   **Zero Critical-Path Penalty:** CPU latency added to host requests must stay below **1.0ms**; allocations must not increase memory baselines.
*   **Kernel Hooks:**
    *   **Laravel:** Bind a `DiagnosticServiceProvider` capturing `DB::listen` (query spans), application bootstrap metrics (`LARAVEL_START`), memory peaks (`memory_get_peak_usage(true)`), and `RequestHandled` / `Terminating` events.
    *   **Symfony:** Register a Doctrine DBAL Middleware / Event Subscriber and a `KernelEvents::TERMINATE` listener.
*   **Stack-Frame Pruning:** Automatically strip framework guts (`Illuminate\*`, `Symfony\*`, `Doctrine\*`, `Composer\*`) to locate the exact application call site (e.g., `App\Http\Controllers\OrderController::index`).
*   **Transport Safety:** Emit payloads over a local Unix Domain Socket (`/var/run/diag/telemetry.sock`) with non-blocking stream mode (`stream_set_blocking($sock, false)`). If the socket buffer fills or the collector restarts, **drop packets instantly**.

### Phase 3: The Ingestion & Normalization Layer (The Sidecar Collector)
*   **Separation of Concerns:**
    *   *Telemetry Plane:* Raw metrics flow across a shared local volume `/var/run/diag/telemetry.sock`.
    *   *Control Plane:* Metadata is fetched out-of-band using the read-only Docker Socket Proxy (`tecnativa/docker-socket-proxy`).
*   **Lightweight Collector (`diag-collector`):**
    *   Runs as an adjacent container or process (Go or Python `asyncio`).
    *   **In-Memory Ring Buffer:** Stores up to $N$ raw events in RAM. Drops stale metrics on overload rather than allocating system memory.
    *   **SQL Normalization:** Strips raw literals (`WHERE id = 42` $\to$ `WHERE id = ?`) and computes a deterministic fingerprint hash (`fingerprint_hash`).
*   **Docker Proxy Metadata Enrichment:**
    *   Queries `GET /containers/{id}/json` from the proxy.
    *   Translates internal container paths (e.g., `/var/www/html/app/...`) to host project paths (e.g., `/Users/amin/projects/my-app/app/...`), which match the file nodes indexed in your Neo4j database.
    *   Extracts container labels, Git commit SHA, and environment variables.
*   **Micro-Batch Ingestion:** Flushes batches to the Assistant Backend over HTTP/gRPC when either **100 events accumulate** or **500ms elapse**.

### Phase 4: The Graph Correlation Engine (Joining Runtime with Neo4j + Qdrant)
*   **Dynamic-to-Static Stitching:** When the Python backend receives a batch:
    1.  Normalize and extract `call_site.class` and `call_site.method`.
    2.  Query **Neo4j**:
        ```cypher
        MATCH (c:Class {name: $class})-[:DECLARES]->(m:Method {name: $method})
        RETURN m
        ```
    3.  If an N+1 incident is detected (same query fingerprint fired repeatedly within the same `trace_id`), traverse the call chain up to the Controller or Root Command:
        ```cypher
        MATCH path = (root:Controller)-[:CALLS*1..5]->(culprit:Method {name: $method})
        RETURN path ORDER BY length(path) ASC LIMIT 1
        ```
*   **Semantic Augmentation (Qdrant):**
    *   Query Qdrant with the problem method signature and query pattern to find existing eager-loading conventions, Model scopes, or Repository patterns in the project.
*   **Incident State in Neo4j:**
    *   Create an `:Incident {type: 'N_PLUS_ONE', fingerprint: '...', duration_ms: ...}` node linked directly to the offending `:Method` and `:Model` nodes.

### Phase 5: AI Orchestration & Automated Remediation (MCP Server)
*   Upgrade your existing Python MCP Assistant with graph-aware tools:
    *   `inspect_active_incidents(service_name)`: Returns high-priority incidents linked to their graph call paths.
    *   `explain_bottleneck(incident_id)`: Fuses runtime telemetry metrics with the Neo4j call graph and model relationships.
    *   `generate_remediation_patch(incident_id)`: Uses static AST definitions (from Neo4j) + dynamic query profiles to generate zero-hallucination Git diffs (e.g., adding `->with(['relation'])` in Laravel/Symfony).
*   **Impact Analysis:** Before proposing an index or query modification, run a Neo4j query to find every other method calling that model/table to eliminate downstream regressions.

### Phase 6: Developer Tooling (CLI & AntV X6 UI)
*   **CLI (`diagctl`):**
    *   `diagctl watch`: Real-time terminal feed of incidents, call sites, and DB impact.
    *   `diagctl fix <incident-id> --apply`: Prompts the assistant, reviews the patch, and applies the Git diff locally.
*   **AntV X6 Visualization:**
    *   Build a Vue + AntV X6 interactive canvas overlaying runtime execution statistics (query counts, peak memory, execution time) on top of your static Neo4j AST Call Graph.

---

## Part 2: The Diagnostic Event Standard (DES) Specification

This schema is the single source of truth across all plugins and the collector.

### JSON Schema Definition (`des-v1.json`)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "DiagnosticEventStandard",
  "type": "object",
  "required": [
    "version",
    "event_id",
    "trace_id",
    "timestamp",
    "service",
    "runtime",
    "metrics",
    "request",
    "queries"
  ],
  "properties": {
    "version": {
      "type": "string",
      "enum": ["1.0.0"]
    },
    "event_id": {
      "type": "string",
      "format": "uuid",
      "description": "Unique ID for this diagnostic record."
    },
    "trace_id": {
      "type": "string",
      "description": "Distributed trace ID or HTTP Request correlation ID."
    },
    "timestamp": {
      "type": "string",
      "format": "date-time"
    },
    "service": {
      "type": "object",
      "required": ["name", "environment"],
      "properties": {
        "name": { "type": "string", "example": "order-service" },
        "environment": { "type": "string", "example": "local" },
        "git_commit": { "type": "string", "example": "c8f2a1b" },
        "container_id": { "type": "string", "example": "4a1b92c8d10e" },
        "host_project_root": { "type": "string", "example": "/Users/amin/projects/order-service" }
      }
    },
    "runtime": {
      "type": "object",
      "required": ["language", "framework"],
      "properties": {
        "language": { "type": "string", "enum": ["php", "python", "node", "go"] },
        "language_version": { "type": "string", "example": "8.3.4" },
        "framework": { "type": "string", "enum": ["laravel", "symfony", "fastapi", "express"] },
        "framework_version": { "type": "string", "example": "11.2.0" }
      }
    },
    "metrics": {
      "type": "object",
      "required": [
        "wall_time_ms",
        "cpu_user_ms",
        "cpu_system_ms",
        "memory_peak_bytes"
      ],
      "properties": {
        "wall_time_ms": { "type": "number", "description": "Total end-to-end request duration." },
        "cpu_user_ms": { "type": "number", "description": "CPU time spent in user space." },
        "cpu_system_ms": { "type": "number", "description": "CPU time spent in system kernel space." },
        "memory_peak_bytes": { "type": "integer", "description": "Peak memory allocated during request." },
        "db_time_total_ms": { "type": "number", "description": "Sum of all DB query execution times." }
      }
    },
    "request": {
      "type": "object",
      "required": ["type"],
      "properties": {
        "type": { "type": "string", "enum": ["http", "cli", "queue_job"] },
        "route": { "type": "string", "example": "api.orders.show" },
        "http_method": { "type": "string", "example": "GET" },
        "url_path": { "type": "string", "example": "/api/v1/orders/52" },
        "command_name": { "type": "string", "example": "orders:recalculate" },
        "status_code": { "type": "integer", "example": 200 }
      }
    },
    "queries": {
      "type": "array",
      "description": "Database queries executed within this request lifecycle.",
      "items": {
        "$ref": "#/$defs/QueryRecord"
      }
    }
  },
  "$defs": {
    "QueryRecord": {
      "type": "object",
      "required": [
        "query_id",
        "fingerprint_hash",
        "sql_raw_redacted",
        "duration_ms",
        "call_site",
        "ast_anchor"
      ],
      "properties": {
        "query_id": { "type": "string", "format": "uuid" },
        "connection": { "type": "string", "example": "mysql_read" },
        "fingerprint_hash": {
          "type": "string",
          "description": "MD5/CRC32 hash of normalized SQL skeleton for N+1 grouping."
        },
        "sql_raw_redacted": {
          "type": "string",
          "description": "Query with sensitive bindings/literals stripped.",
          "example": "SELECT * FROM orders WHERE user_id = ? AND status = ?"
        },
        "duration_ms": { "type": "number", "example": 14.8 },
        "rows_affected": { "type": ["integer", "null"], "example": 1 },
        "is_transaction": { "type": "boolean", "default": false },
        "call_site": {
          "type": "object",
          "required": ["file", "line"],
          "properties": {
            "file": { "type": "string", "example": "app/Http/Resources/OrderResource.php" },
            "line": { "type": "integer", "example": 38 },
            "class": { "type": "string", "example": "App\\Http\\Resources\\OrderResource" },
            "method": { "type": "string", "example": "toArray" }
          }
        },
        "ast_anchor": {
          "type": "object",
          "description": "Direct foreign keys to match nodes in your Neo4j AST graph.",
          "required": ["node_type", "qualified_name"],
          "properties": {
            "node_type": { "type": "string", "enum": ["Method", "Function", "ModelAction"] },
            "qualified_name": {
              "type": "string",
              "example": "App\\Http\\Resources\\OrderResource::toArray"
            },
            "ast_node_id": {
              "type": ["string", "null"],
              "description": "Optional deterministic node hash if pre-computed by indexer."
            }
          }
        }
      }
    }
  }
}
```

### Complete Concrete Payload Example (Emitted Event)

```json
{
  "version": "1.0.0",
  "event_id": "f2a71d18-971c-4b6e-82d2-8b4e72c842b1",
  "trace_id": "req-98f5a2a7-53e2",
  "timestamp": "2026-09-22T10:35:12.182Z",
  "service": {
    "name": "checkout-api",
    "environment": "local",
    "git_commit": "e4a2c109",
    "container_id": "b182a4d98f71",
    "host_project_root": "/Users/amin/projects/checkout-api"
  },
  "runtime": {
    "language": "php",
    "language_version": "8.3.4",
    "framework": "laravel",
    "framework_version": "11.2.0"
  },
  "metrics": {
    "wall_time_ms": 142.3,
    "cpu_user_ms": 45.1,
    "cpu_system_ms": 12.0,
    "memory_peak_bytes": 18874368,
    "db_time_total_ms": 78.4
  },
  "request": {
    "type": "http",
    "route": "orders.index",
    "http_method": "GET",
    "url_path": "/api/v1/orders",
    "status_code": 200
  },
  "queries": [
    {
      "query_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
      "connection": "mysql",
      "fingerprint_hash": "a1c4b72e90f23d11",
      "sql_raw_redacted": "SELECT * FROM `orders` WHERE `status` = ? LIMIT 20",
      "duration_ms": 3.4,
      "rows_affected": 20,
      "is_transaction": false,
      "call_site": {
        "file": "app/Http/Controllers/OrderController.php",
        "line": 28,
        "class": "App\\Http\\Controllers\\OrderController",
        "method": "index"
      },
      "ast_anchor": {
        "node_type": "Method",
        "qualified_name": "App\\Http\\Controllers\\OrderController::index",
        "ast_node_id": null
      }
    },
    {
      "query_id": "18f9c104-5827-4cb1-8092-127914fbb012",
      "connection": "mysql",
      "fingerprint_hash": "8f3b20c941a87b41",
      "sql_raw_redacted": "SELECT * FROM `order_items` WHERE `order_id` = ?",
      "duration_ms": 2.1,
      "rows_affected": 3,
      "is_transaction": false,
      "call_site": {
        "file": "app/Http/Resources/OrderItemResource.php",
        "line": 19,
        "class": "App\\Http\\Resources\\OrderItemResource",
        "method": "toArray"
      },
      "ast_anchor": {
        "node_type": "Method",
        "qualified_name": "App\\Http\\Resources\\OrderItemResource::toArray",
        "ast_node_id": null
      }
    }
  ]
}
```
