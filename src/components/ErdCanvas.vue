<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { Graph } from '@antv/x6';
import type { Edge, Node } from '@antv/x6';
import type { ErdRef, ErdTable, NormalizedSchema } from '../types';
import { registerErdNode } from '../utils/registerErdNode';
import { buildErdEdges } from '../utils/erdTransformer';

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

const truncate = (value: string, maxLength: number) => {
  const text = String(value || '');
  return text.length > maxLength ? `${text.slice(0, Math.max(1, maxLength - 1))}…` : text;
};

const nodeFor = (table: ErdTable, visible: boolean) => {
  const width = table.width || 280;
  const height = table.height || 46 + table.fields.length * 27;
  const rowHeight = 27;
  const rowY = (index: number) => 36 + index * rowHeight;
  const rowTextY = (index: number) => rowY(index) + rowHeight / 2;
  const fieldContentWidth = Math.max(120, width - 40);
  const nameColumnWidth = Math.floor(fieldContentWidth * 0.56);
  const typeColumnWidth = Math.max(72, fieldContentWidth - nameColumnWidth);
  const nameLimit = Math.max(8, Math.floor(nameColumnWidth / 7));
  const typeLimit = Math.max(8, Math.floor(typeColumnWidth / 6.5));
  const markup = [
    { tagName: 'rect', selector: 'body' },
    { tagName: 'rect', selector: 'header' },
    { tagName: 'rect', selector: 'divider' },
    { tagName: 'text', selector: 'title' },
    { tagName: 'text', selector: 'count' },
    ...table.fields.flatMap((_, index) => [
      { tagName: 'rect', selector: `row-${index}` },
      { tagName: 'text', selector: `marker-${index}` },
      { tagName: 'text', selector: `name-${index}` },
      { tagName: 'text', selector: `type-${index}` },
    ]),
  ];
  return {
    id: table.id,
    shape: 'erd-table',
    markup,
    x: table.position?.x || 40,
    y: table.position?.y || 40,
    width,
    height,
    visible,
    attrs: {
      body: { width, height, fill: '#11182d', stroke: props.selectedTableId === table.id ? '#a5b4fc' : '#334155', strokeWidth: props.selectedTableId === table.id ? 2.2 : 1.2, rx: 10, ry: 10 },
      header: { width, height: 36, fill: '#1e293b', stroke: 'none', rx: 10, ry: 10 },
      divider: { x: 0, y: 36, width, height: 1, fill: '#334155', stroke: 'none' },
      title: {
        text: truncate(table.name, Math.max(8, Math.floor((width - 72) / 7))),
        x: 14,
        y: 11,
        fill: '#fff',
        fontSize: 13,
        fontWeight: 600,
        textAnchor: 'start',
        textVerticalAnchor: 'top',
      },
      count: { text: `${table.fields.length} cols`, x: width - 12, y: 11, fill: '#94a3b8', fontSize: 9, textAnchor: 'end', textVerticalAnchor: 'top' },
      ...Object.fromEntries(table.fields.flatMap((field, index) => {
        const textY = rowTextY(index);
        const fieldName = truncate(field.name, nameLimit);
        const fieldType = truncate(`${field.type}${field.nullable ? ' · null' : ''}`, typeLimit);
        return [
          [`row-${index}`, { x: 0, y: rowY(index), width, height: rowHeight, fill: index % 2 ? '#0f172a' : '#11182d', stroke: '#1e293b', strokeWidth: 0.6 }],
          [`marker-${index}`, { text: field.primaryKey ? '◆' : field.foreignKey ? '↳' : '', x: 14, y: textY, fill: field.primaryKey ? '#fcd34d' : '#818cf8', fontSize: 10, textAnchor: 'middle', textVerticalAnchor: 'middle' }],
          [`name-${index}`, { text: fieldName, ...(fieldName !== field.name ? { textLength: nameColumnWidth, lengthAdjust: 'spacingAndGlyphs' } : {}), x: 28, y: textY, fill: '#e2e8f0', fontSize: 11, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', textAnchor: 'start', textVerticalAnchor: 'middle' }],
          [`type-${index}`, { text: fieldType, ...(fieldType !== `${field.type}${field.nullable ? ' · null' : ''}` ? { textLength: typeColumnWidth, lengthAdjust: 'spacingAndGlyphs' } : {}), x: width - 12, y: textY, fill: '#94a3b8', fontSize: 10, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', textAnchor: 'end', textVerticalAnchor: 'middle' }],
        ];
      })),
    },
    ports: {
      groups: {
        left: { position: 'absolute', attrs: { circle: { r: 4, magnet: true, stroke: '#0f172a', strokeWidth: 1.5, fill: '#818cf8' } } },
        right: { position: 'absolute', attrs: { circle: { r: 4, magnet: true, stroke: '#0f172a', strokeWidth: 1.5, fill: '#818cf8' } } },
      },
      items: table.fields.flatMap((field, index) => [
        { id: `${field.id}:in`, group: 'left', args: { x: 0, y: 48 + index * 27 } },
        { id: `${field.id}:out`, group: 'right', args: { x: width, y: 48 + index * 27 } },
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

defineExpose({ fit, zoomIn, zoomOut, reset, exportSvg, exportPng });

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
