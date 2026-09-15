***

### 📋 Agent Prompt: Migrate AntV X6 ERD Nodes from Native SVG to Vue 3 Components

```markdown
You are a Senior Frontend Engineer specializing in Vue 3 (Composition API, `<script setup>`, TypeScript, Tailwind CSS) and AntV X6 (`@antv/x6`).

### OBJECTIVE
Migrate our AntV X6 ERD table node implementation away from fragile, error-prone manual SVG markup (`Graph.registerNode` with declarative `<rect>`/`<text>`) to **Vue 3 SFC components** using `@antv/x6-vue-shape` (or `@antv/x6-plugin-teleport` / `@antv/x6-vue-shape` v2/v3).

This resolves text-protrusion bugs, text alignment clipping, and layout calculation overhead by leveraging standard HTML/CSS (Flexbox, Tailwind, text truncation).

---

### REQUIRED CHANGES & ARCHITECTURE

#### 1. Dependencies
Ensure `@antv/x6-vue-shape` is utilized for registering Vue components into the X6 lifecycle:
```bash
npm install @antv/x6-vue-shape
```

#### 2. Create the Vue Table Node Component (`components/ErdTableNode.vue`)
Create a single-file component for rendering table nodes:
- Access node data cleanly using `inject<() => Node>('getNode')`.
- Design: Modern dark theme (Tailwind classes `bg-slate-900`, `border-slate-700/80`, `font-mono text-xs`).
- **Header:**
  - Table icon + Table name (bold, truncated).
  - Badge showing column count (e.g., `14 cols`).
- **Body / Rows (Flexbox layout - no SVG coordinate math):**
  - Left side: 
    - Icon: 🔑 for Primary Keys, ↳ for Foreign Keys, or dot indicator.
    - Column name (`text-slate-200`, `truncate`).
  - Right side:
    - Data type (e.g., `varchar`, `bigint unsigned`, dimmed `text-slate-400`).
    - `null` badge/tag if nullable (`text-slate-500 text-[10px]`).
- Row height should be fixed (e.g., `h-7` / `28px`) to ensure exact port alignment.

#### 3. Refactor Node Registration (`utils/registerErdNode.ts`)
- Remove all manual SVG markup generators (`tagName: 'text'`, `refX2`, `textAnchor`, etc.).
- Register the Vue component using `register` from `@antv/x6-vue-shape`:
```ts
import { register } from '@antv/x6-vue-shape';
import ErdTableNode from '@/components/ErdTableNode.vue';

export function initErdCustomShapes() {
  register({
    shape: 'erd-vue-table',
    width: 280,
    component: ErdTableNode,
  });
}
```

#### 4. Update Graph Transformer & Port Mapping (`composables/useErdTransformer.ts`)
When transforming database schema tables into X6 node metadata:
- **Shape:** Set `shape: 'erd-vue-table'`.
- **Node Data:** Pass the normalized table payload:
  ```ts
  data: {
    id: table.id,
    name: table.name,
    columns: table.columns.map(col => ({
      name: col.name,
      type: col.type,
      isPk: col.isPk,
      isFk: col.isFk,
      nullable: col.nullable,
    }))
  }
  ```
- **Height Calculation:** 
  - `const HEADER_HEIGHT = 38;`
  - `const ROW_HEIGHT = 28;`
  - `height: HEADER_HEIGHT + (table.columns.length * ROW_HEIGHT)`
- **Port Alignment:**
  - Configure left and right ports attached to the vertical center of each corresponding row:
  ```ts
  ports: {
    groups: {
      left: { position: 'left', attrs: { circle: { r: 3.5, magnet: true, fill: '#38bdf8', stroke: '#0f172a', strokeWidth: 1.5 } } },
      right: { position: 'right', attrs: { circle: { r: 3.5, magnet: true, fill: '#38bdf8', stroke: '#0f172a', strokeWidth: 1.5 } } },
    },
    items: table.columns.flatMap((col, idx) => [
      { id: `${table.name}:${col.name}:left`, group: 'left', args: { dy: HEADER_HEIGHT + (idx * ROW_HEIGHT) + (ROW_HEIGHT / 2) } },
      { id: `${table.name}:${col.name}:right`, group: 'right', args: { dy: HEADER_HEIGHT + (idx * ROW_HEIGHT) + (ROW_HEIGHT / 2) } },
    ]),
  }
  ```

#### 5. Relationship Edges & Canvas Configuration (`components/ErdCanvas.vue`)
- Router: Use `er` or `manhattan` router to connect ports neatly.
- Connector: `rounded` (radius: 4–6px).
- Verify clean disposal of graph on unmount (`graph.dispose()`).

---

### DELIVERABLES EXPECTED
1. `components/ErdTableNode.vue` (Clean Vue 3 SFC with Tailwind layout).
2. `utils/registerErdNode.ts` (Registration via `@antv/x6-vue-shape`).
3. Updated `composables/useErdTransformer.ts` (Generating node metadata with data payload and matching port heights).
4. No SVG coordinate math or text overflow bugs remaining.
```