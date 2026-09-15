You are a Senior Full-Stack Engineer specializing in Vue 3, TypeScript, and Data Visualization engines.

### OBJECTIVE
Build a high-performance, production-ready Vue 3 (Composition API + `<script setup>` + TypeScript) component that parses database schemas (SQL DDL, DBML text, or pre-parsed backend JSON from Laravel/Symfony) and renders them into an interactive, column-level Entity-Relationship Diagram (ERD) using **AntV X6 (`@antv/x6`)**.

---

### ARCHITECTURAL CONSTRAINTS & TECH STACK
1. **Framework:** Vue 3 + TypeScript (`<script setup lang="ts">`).
2. **Diagram Engine:** `@antv/x6` (v2.x or v3.x).
3. **Performance Requirement (Crucial):**
   - **Do NOT** use `@antv/x6-vue-shape` or Vue DOM Teleports for table nodes. 
   - Render table nodes using **native X6 SVG Markup / Declarative Shapes** (`Graph.registerNode` with SVG `<rect>`, `<text>`, `<path>`, and SVG groups) to ensure zero Vue reactivity/teleport overhead and smooth 60fps panning/zooming on large schemas (50+ tables).
4. **Dual Data Ingestion Pipeline:**
   - **Option A (Client-side Parsing):** Accept raw DBML or SQL DDL strings and parse them via `@dbml/core` (`Parser.parse` + `ModelExporter.export(..., 'json')`).
   - **Option B (Direct Backend Feed):** Accept pre-parsed normalized JSON directly via component props (for zero-bundle backend integration with Laravel/Symfony).
5. **Layout Engine:** Use `@antv/layout` (Dagre layout) or standard `dagre` to automatically calculate non-overlapping `(x, y)` coordinates for tables.

---

### DETAILED SPECIFICATIONS

#### 1. Data Contract & Types (`types/erd.ts`)
- Define strict TypeScript interfaces for:
  - Normalized Schema: `Table`, `Field` (name, type, pk, unique, notNull, note), `Ref` (endpoints: tableName, fieldNames, relation: '1' | '*').
  - X6 Node/Edge conversion payloads.

#### 2. Schema Parser & Transformer (`composables/useErdTransformer.ts`)
- Implement `parseSchemaInput(input: string | object, format?: 'dbml' | 'sql' | 'json'): NormalizedSchema`.
- Implement `transformSchemaToGraph(schema: NormalizedSchema): { nodes: Node.Metadata[], edges: Edge.Metadata[] }`:
  - Calculate dynamic node dimensions (width/height) based on field count and longest string length.
  - Dynamically generate **left and right port groups** attached to each individual column index (`portId = ${tableName}:${fieldName}:left|right`).
  - Run Dagre layout on the graph topology to produce initial `(x, y)` positions before node creation.

#### 3. Custom Native SVG Node Definition (`utils/registerErdNode.ts`)
- Register a custom shape named `erd-table` with:
  - Header: Rect background, table icon, table name label.
  - Rows: Alternating or clean list background, PK indicator (🔑 or distinct badge), Field name, and Data type (dimmed/secondary color).
  - Port configurations: invisible or subtle circular anchors on left and right edges aligned exactly with each column's Y-offset.

#### 4. Relationship Edges & Routing
- Connect source column port to target column port directly.
- **Router:** `router: { name: 'er' }` or `router: { name: 'manhattan', args: { padding: 24, startDirections: ['left', 'right'], endDirections: ['left', 'right'] } }` to prevent lines from slicing through table bodies.
- **Connector:** `connector: { name: 'rounded', args: { radius: 6 } }` or `connector: { name: 'jumpover' }`.
- **Markers & Labels:** Standard Crow's Foot markers or cardinality text badges (`1`, `*`, `0..1`, `1..n`) along the edge path.

#### 5. Canvas Component (`components/ErdCanvas.vue`)
- Initialize `new Graph(...)` in `onMounted` with container ref.
- Enable:
  - Panning (`panning: true`)
  - Mouse wheel zoom (`mousewheel: { enabled: true, modifiers: ['ctrl', 'meta'], maxScale: 2, minScale: 0.2 }`)
  - Grid mesh background (`grid: { visible: true, type: 'dot', args: { color: '#e5e7eb', thickness: 1 } }`)
  - Magnet snapping when dragging edges interactively.
- Lifecycle: properly call `graph.dispose()` in `onBeforeUnmount`.
- Expose a clean top/floating toolbar:
  - Zoom In / Zoom Out / Reset View (`zoomToFit({ padding: 40, maxScale: 1 })`)
  - Re-run Auto Layout
  - Export to PNG / SVG / DBML string.

---

### DELIVERABLES EXPECTED
1. `types/erd.ts` (Full TypeScript definitions)
2. `utils/registerErdNode.ts` (Native SVG markup and port layout registration)
3. `composables/useErdTransformer.ts` (Parser, transformer, and Dagre auto-layout logic)
4. `components/ErdCanvas.vue` (Complete single-file component with toolbar and canvas)
5. A working mock schema (e.g., `users`, `orders`, `order_items`, `products`) included by default so the component works immediately out-of-the-box.
