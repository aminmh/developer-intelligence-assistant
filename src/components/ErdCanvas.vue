<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { Graph } from '@antv/x6';
import { DagreLayout } from '@antv/layout';
import type { Edge, Node } from '@antv/x6';
import type { ErdRef, ErdTable, NormalizedSchema } from '../types';
import { registerErdNode } from '../utils/registerErdNode';
import { buildErdEdges, tableDimensions } from '../utils/erdTransformer';
import { ERD_NODE_METRICS } from '../utils/erdMetrics';

const props = defineProps<{
  schema: NormalizedSchema | null;
  search?: string;
  selectedTableId?: string;
  selectedRefId?: string;
}>();

const emit = defineEmits<{
  (event: 'select-table', table: ErdTable): void;
  (event: 'select-ref', ref: ErdRef): void;
  (event: 'ready', graph: Graph): void;
}>();

const container = ref<HTMLElement | null>(null);
let graph: Graph | null = null;

const nodeFor = (table: ErdTable, visible: boolean) => {
  const { width, height } = tableDimensions(table);
  const portY = (index: number) => ERD_NODE_METRICS.HEADER_HEIGHT
    + index * ERD_NODE_METRICS.ROW_HEIGHT
    + ERD_NODE_METRICS.ROW_HEIGHT / 2;
  return {
    id: table.id,
    shape: 'erd-vue-table',
    x: table.position?.x || 40,
    y: table.position?.y || 40,
    width,
    height,
    visible,
    data: {
      table,
      id: table.id,
      name: table.name,
      columns: table.fields.map((field) => ({
        id: field.id,
        name: field.name,
        type: field.type,
        isPk: field.primaryKey,
        isFk: field.foreignKey,
        nullable: field.nullable,
      })),
      selected: props.selectedTableId === table.id,
    },
    ports: {
      groups: {
        left: { position: 'absolute', attrs: { circle: { r: 4, magnet: true, stroke: '#0f172a', strokeWidth: 1.5, fill: '#818cf8' } } },
        right: { position: 'absolute', attrs: { circle: { r: 4, magnet: true, stroke: '#0f172a', strokeWidth: 1.5, fill: '#818cf8' } } },
      },
      items: table.fields.flatMap((field, index) => [
        { id: `${field.id}:in`, group: 'left', args: { x: 0, y: portY(index) } },
        { id: `${field.id}:out`, group: 'right', args: { x: width, y: portY(index) } },
      ]),
    },
  };
};

const rebuild = () => {
  if (!graph || !props.schema) return;
  graph.clearCells();
  const query = (props.search || '').trim().toLowerCase();
  const visible = (table: ErdTable) => !query || `${table.name} ${table.fields.map((field) => field.name).join(' ')}`.toLowerCase().includes(query);
  const tables = props.schema.tables;
  graph.addNodes(tables.map((table) => nodeFor(table, visible(table))));
  graph.addEdges(buildErdEdges(props.schema).filter((edge) => {
    const source = tables.find((table) => table.id === edge.source.cell);
    const target = tables.find((table) => table.id === edge.target.cell);
    return Boolean(source && target && visible(source) && visible(target));
  }).map((edge) => {
    const ref = edge.data;
    return {
      id: ref.id,
      shape: 'edge',
      source: edge.source,
      target: edge.target,
      router: edge.router,
      connector: edge.connector,
      zIndex: 0,
      attrs: { line: { stroke: ref.cardinality === 'N:M' ? '#fbbf24' : ref.cardinality === '1:1' ? '#34d399' : '#818cf8', strokeWidth: props.selectedRefId === ref.id ? 2.8 : 1.6, targetMarker: { name: 'block', width: 8, height: 7 } } },
      labels: [{ position: 0.5, attrs: { label: { text: ref.cardinality, fill: '#cbd5e1', fontSize: 10, fontWeight: 600 }, body: { fill: '#0b1020', stroke: '#334155', rx: 4, ry: 3 } } }],
      data: ref,
    };
  }));
  graph.zoomToFit({ padding: 24, maxScale: 1.2 });
};

const selectNode = (cell: Node) => {
  const table = props.schema?.tables.find((item) => item.id === cell.id);
  if (table) emit('select-table', table);
};
const selectEdge = (cell: Edge) => {
  const ref = props.schema?.refs.find((item) => item.id === cell.id);
  if (ref) emit('select-ref', ref);
};

const fit = () => graph?.zoomToFit({ padding: 24, maxScale: 1.2 });
const zoomIn = () => graph?.zoom(0.15);
const zoomOut = () => graph?.zoom(-0.15);
const reset = () => { if (props.schema) { props.schema.tables.forEach((table, index) => { table.position = { x: 40 + (index % 3) * 360, y: 40 + Math.floor(index / 3) * 260 }; }); rebuild(); } };
const prettify = async () => {
  if (!props.schema) return;
  const tables = props.schema.tables;
  const nodes = tables.map((table) => ({ id: table.id, width: tableDimensions(table).width, height: tableDimensions(table).height }));
  const edges = buildErdEdges(props.schema).map((edge) => ({ id: edge.id, source: edge.source.cell, target: edge.target.cell }));
  try {
    const layout = new DagreLayout({
      type: 'dagre',
      rankdir: 'LR',
      nodesep: 70,
      ranksep: 150,
      controlPoints: false,
      nodeSize: (node: { width: number; height: number }) => [node.width, node.height],
    });
    await layout.execute({ nodes, edges }, { width: 1600, height: 1000 });
    const positions = new Map<string, { x: number; y: number }>();
    layout.forEachNode((node) => positions.set(String(node.id), {
      x: Math.max(40, node.x - ((node.size as any)?.[0] || 0) / 2),
      y: Math.max(40, node.y - ((node.size as any)?.[1] || 0) / 2),
    }));
    tables.forEach((table) => { table.position = positions.get(table.id) || table.position || { x: 40, y: 40 }; });
  } catch {
    reset();
    return;
  }
  rebuild();
};

const exportSvg = () => {
  if (!graph) return;
  const svg = graph.container.querySelector('svg');
  if (!svg) return;
  const clone = svg.cloneNode(true) as SVGElement;
  clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
  const blob = new Blob([new XMLSerializer().serializeToString(clone)], { type: 'image/svg+xml' });
  const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'schema-erd.svg'; anchor.click(); URL.revokeObjectURL(url);
};

const exportPng = () => {
  if (!graph) return;
  const svg = graph.container.querySelector('svg');
  if (!svg) return;
  const clone = svg.cloneNode(true) as SVGElement; clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
  const source = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(new XMLSerializer().serializeToString(clone))}`;
  const image = new Image(); image.onload = () => { const canvas = document.createElement('canvas'); canvas.width = Math.max(1, svg.clientWidth * 2); canvas.height = Math.max(1, svg.clientHeight * 2); const ctx = canvas.getContext('2d'); if (!ctx) return; ctx.fillStyle = '#0b1020'; ctx.fillRect(0, 0, canvas.width, canvas.height); ctx.drawImage(image, 0, 0, canvas.width, canvas.height); const anchor = document.createElement('a'); anchor.href = canvas.toDataURL('image/png'); anchor.download = 'schema-erd.png'; anchor.click(); }; image.src = source;
};

defineExpose({ fit, zoomIn, zoomOut, reset, prettify, exportSvg, exportPng });

onMounted(() => {
  if (!container.value) return;
  registerErdNode();
  graph = new Graph({ container: container.value, background: { color: '#0b1020' }, grid: { visible: true, type: 'dot', args: { color: '#1e293b', thickness: 1 } }, panning: true, mousewheel: { enabled: true, modifiers: ['ctrl', 'meta'] }, interacting: { nodeMovable: true, edgeMovable: false, edgeLabelMovable: false } });
  graph.on('node:click', ({ node }) => selectNode(node));
  graph.on('edge:click', ({ edge }) => selectEdge(edge));
  emit('ready', graph);
  rebuild();
});
watch(() => [props.schema, props.search, props.selectedTableId, props.selectedRefId], rebuild, { deep: true });
onBeforeUnmount(() => { graph?.dispose(); graph = null; });
</script>

<template><div ref="container" class="h-full w-full" /></template>
