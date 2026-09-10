<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import {
  AlertTriangle,
  Database,
  Focus,
  KeyRound,
  Link2,
  Maximize2,
  Minus,
  Plus,
  RefreshCw,
  Search,
  X,
} from 'lucide-vue-next';
import { makeRequest } from '../ui/makeRequest';
import type {
  AnalysisSession,
  SchemaVisualizationEdge,
  SchemaVisualizationNode,
  SchemaVisualizationResponse,
} from '../types';

const analyses = ref<AnalysisSession[]>([]);
const selectedAnalysisId = ref('');
const graph = ref<SchemaVisualizationResponse | null>(null);
const search = ref('');
const loading = ref(false);
const loadingAnalyses = ref(false);
const error = ref('');
const selectedTableId = ref('');
const selectedEdgeId = ref('');
const zoom = ref(1);
const pan = ref({ x: 24, y: 24 });
const dragging = ref(false);
const dragStart = ref({ x: 0, y: 0 });
const panStart = ref({ x: 0, y: 0 });

const selectedAnalysis = computed(() => analyses.value.find((item) => item.id === selectedAnalysisId.value) || null);
const visibleNodes = computed(() => {
  const query = search.value.trim().toLowerCase();
  if (!graph.value || !query) return graph.value?.nodes || [];
  return (graph.value.nodes || []).filter((node) => (node.data.searchText || node.data.table).toLowerCase().includes(query));
});
const matchingNodeIds = computed(() => new Set(visibleNodes.value.map((node) => node.id)));
const visibleEdges = computed(() => (graph.value?.edges || []).filter((edge) => matchingNodeIds.value.has(`table:${edge.source}`) && matchingNodeIds.value.has(`table:${edge.target}`)));
const selectedTable = computed(() => graph.value?.nodes.find((node) => node.id === selectedTableId.value) || null);
const selectedEdge = computed(() => graph.value?.edges.find((edge) => edge.id === selectedEdgeId.value) || null);

const nodeWidth = 300;
const nodeHeight = (node: SchemaVisualizationNode) => 76 + node.data.columns.length * 30;
const nodeCenter = (node: SchemaVisualizationNode) => ({
  x: node.position.x + nodeWidth / 2,
  y: node.position.y + nodeHeight(node) / 2,
});
const edgeLine = (edge: SchemaVisualizationEdge) => {
  const source = graph.value?.nodes.find((node) => node.data.table === edge.source);
  const target = graph.value?.nodes.find((node) => node.data.table === edge.target);
  if (!source || !target) return null;
  const a = nodeCenter(source);
  const b = nodeCenter(target);
  return { x1: a.x, y1: a.y, x2: b.x, y2: b.y };
};

const loadAnalyses = async () => {
  loadingAnalyses.value = true;
  error.value = '';
  try {
    const response = await makeRequest('/api/analysis?limit=100');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || data.error || 'Failed to load saved analyses.');
    analyses.value = (data.analyses || []).filter((item: AnalysisSession) => item.kind !== 'code_index');
    if (!selectedAnalysisId.value && analyses.value.length) selectedAnalysisId.value = analyses.value[0].id;
  } catch (cause: any) {
    error.value = cause?.message || 'Failed to load saved analyses.';
  } finally {
    loadingAnalyses.value = false;
  }
};

const loadGraph = async () => {
  if (!selectedAnalysisId.value) { graph.value = null; return; }
  loading.value = true;
  error.value = '';
  graph.value = null;
  selectedTableId.value = '';
  selectedEdgeId.value = '';
  try {
    const response = await makeRequest(`/api/analysis/${encodeURIComponent(selectedAnalysisId.value)}/schema-visualization`);
    const data = await response.json() as SchemaVisualizationResponse & { detail?: string };
    if (!response.ok) throw new Error(data.detail || 'Failed to load schema visualization.');
    graph.value = data;
    await nextTick();
    resetView();
  } catch (cause: any) {
    error.value = cause?.message || 'Failed to load schema visualization.';
  } finally {
    loading.value = false;
  }
};

const resetView = () => { zoom.value = 1; pan.value = { x: 24, y: 24 }; };
const fitView = () => {
  if (!graph.value?.nodes.length) return resetView();
  const maxX = Math.max(...graph.value.nodes.map((node) => node.position.x + nodeWidth));
  const maxY = Math.max(...graph.value.nodes.map((node) => node.position.y + nodeHeight(node)));
  zoom.value = Math.min(1, 0.9 * Math.min(1, 900 / Math.max(maxX, 1), 560 / Math.max(maxY, 1)));
  pan.value = { x: 24, y: 24 };
};
const selectNode = (node: SchemaVisualizationNode) => { selectedTableId.value = node.id; selectedEdgeId.value = ''; };
const selectEdge = (edge: SchemaVisualizationEdge) => { selectedEdgeId.value = edge.id; selectedTableId.value = ''; };
const startPan = (event: PointerEvent) => {
  if ((event.target as HTMLElement).closest('[data-schema-node]')) return;
  dragging.value = true;
  dragStart.value = { x: event.clientX, y: event.clientY };
  panStart.value = { ...pan.value };
  (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
};
const movePan = (event: PointerEvent) => {
  if (!dragging.value) return;
  pan.value = { x: panStart.value.x + event.clientX - dragStart.value.x, y: panStart.value.y + event.clientY - dragStart.value.y };
};
const stopPan = () => { dragging.value = false; };
const onWheel = (event: WheelEvent) => {
  event.preventDefault();
  zoom.value = Math.min(1.8, Math.max(0.45, zoom.value * (event.deltaY > 0 ? 0.9 : 1.1)));
};
const formatDefault = (value: unknown) => value === null || value === undefined ? '—' : String(value);

watch(selectedAnalysisId, loadGraph);
onMounted(loadAnalyses);
</script>

<template>
  <section class="flex-1 min-h-0 overflow-hidden p-5 flex flex-col gap-4">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h3 class="text-lg font-bold text-white flex items-center gap-2"><Database class="w-5 h-5 text-indigo-400" /> Database Schema</h3>
        <p class="text-xs text-slate-500 mt-1">Deterministic Laravel migration graph with optional Eloquent relationship enrichment.</p>
      </div>
      <button @click="loadAnalyses" class="px-3 py-2 rounded-xl border border-slate-700 text-xs text-slate-300 hover:bg-slate-800"><RefreshCw :class="loadingAnalyses ? 'animate-spin' : ''" class="w-3.5 h-3.5 inline mr-1" />Refresh</button>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-[250px_1fr_auto] gap-2 rounded-2xl border border-slate-800 bg-slate-900/30 p-3">
      <select v-model="selectedAnalysisId" class="bg-[#11162d] border border-slate-800 rounded-xl px-3 py-2 text-xs text-slate-300">
        <option value="">Select saved analysis</option>
        <option v-for="analysis in analyses" :key="analysis.id" :value="analysis.id">{{ analysis.label || analysis.filename }} · {{ analysis.created_at }}</option>
      </select>
      <div class="relative"><Search class="absolute left-3 top-2.5 w-3.5 h-3.5 text-slate-500" /><input v-model="search" placeholder="Search table or column…" class="w-full bg-[#11162d] border border-slate-800 rounded-xl pl-9 pr-3 py-2 text-xs text-slate-200" /></div>
      <div class="flex items-center gap-1">
        <button @click="fitView" title="Fit to view" class="p-2 rounded-lg border border-slate-700 text-slate-300 hover:bg-slate-800"><Maximize2 class="w-3.5 h-3.5" /></button>
        <button @click="zoom = Math.min(1.8, zoom + 0.1)" title="Zoom in" class="p-2 rounded-lg border border-slate-700 text-slate-300 hover:bg-slate-800"><Plus class="w-3.5 h-3.5" /></button>
        <button @click="zoom = Math.max(0.45, zoom - 0.1)" title="Zoom out" class="p-2 rounded-lg border border-slate-700 text-slate-300 hover:bg-slate-800"><Minus class="w-3.5 h-3.5" /></button>
        <button @click="resetView" title="Reset layout" class="p-2 rounded-lg border border-slate-700 text-slate-300 hover:bg-slate-800"><Focus class="w-3.5 h-3.5" /></button>
      </div>
    </div>

    <div v-if="error" class="rounded-xl border border-rose-500/30 bg-rose-950/20 p-4 text-xs text-rose-300">{{ error }}</div>
    <div v-else-if="!selectedAnalysisId" class="flex-1 rounded-2xl border border-dashed border-slate-700 p-10 text-center text-sm text-slate-500 flex items-center justify-center">Choose a saved analysis to inspect its database schema.</div>
    <div v-else-if="loading" class="flex-1 rounded-2xl border border-slate-800 bg-slate-900/30 flex items-center justify-center text-sm text-slate-500">Loading schema graph…</div>
    <div v-else-if="graph && !graph.nodes.length" class="flex-1 rounded-2xl border border-dashed border-slate-700 p-10 text-center text-sm text-slate-500 flex items-center justify-center">{{ graph.source.framework?.toLowerCase() === 'laravel' ? 'This Laravel analysis contains no migration-derived tables.' : 'Schema visualization is available for Laravel analyses with migrations.' }}</div>
    <div v-else-if="graph" class="flex-1 min-h-0 grid grid-cols-1 xl:grid-cols-[1fr_280px] gap-3">
      <div class="min-h-0 rounded-2xl border border-slate-800 bg-[#0b1020] overflow-hidden relative select-none" @pointerdown="startPan" @pointermove="movePan" @pointerup="stopPan" @pointercancel="stopPan" @wheel="onWheel">
        <div class="absolute top-3 left-3 z-10 flex items-center gap-2 text-[10px] text-slate-400 bg-slate-950/80 border border-slate-800 rounded-lg px-2 py-1">{{ visibleNodes.length }} table(s) · {{ visibleEdges.length }} relationship(s) · {{ Math.round(zoom * 100) }}%</div>
        <div class="absolute top-3 right-3 z-10 flex items-center gap-3 text-[10px] text-slate-400 bg-slate-950/80 border border-slate-800 rounded-lg px-2 py-1"><span><i class="inline-block w-2 h-2 rounded-full bg-indigo-400 mr-1" />1:N</span><span><i class="inline-block w-2 h-2 rounded-full bg-emerald-400 mr-1" />1:1</span><span><i class="inline-block w-2 h-2 rounded-full bg-amber-400 mr-1" />N:M</span></div>
        <div class="absolute inset-0 overflow-auto" style="cursor: grab">
          <div class="relative" :style="{ width: `${Math.max(1100, ...graph.nodes.map((node) => node.position.x + nodeWidth + 80))}px`, height: `${Math.max(700, ...graph.nodes.map((node) => node.position.y + nodeHeight(node) + 80))}px`, transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`, transformOrigin: 'top left' }">
            <svg class="absolute inset-0 overflow-visible pointer-events-none" :width="Math.max(1100, ...graph.nodes.map((node) => node.position.x + nodeWidth + 80))" :height="Math.max(700, ...graph.nodes.map((node) => node.position.y + nodeHeight(node) + 80))">
              <line v-for="edge in visibleEdges" :key="edge.id" v-if="edgeLine(edge)" :x1="edgeLine(edge)?.x1" :y1="edgeLine(edge)?.y1" :x2="edgeLine(edge)?.x2" :y2="edgeLine(edge)?.y2" :stroke="edge.cardinality === 'N:M' ? '#fbbf24' : edge.cardinality === '1:1' ? '#34d399' : '#818cf8'" :stroke-width="selectedEdgeId === edge.id ? 4 : 2" stroke-dasharray="edge.cardinality === 'N:M' ? '7 5' : undefined" class="pointer-events-auto cursor-pointer opacity-70" @click.stop="selectEdge(edge)" />
            </svg>
            <article v-for="node in visibleNodes" :key="node.id" data-schema-node class="absolute w-[300px] rounded-xl border bg-[#11182d] shadow-xl shadow-black/20 overflow-hidden cursor-pointer" :class="selectedTableId === node.id ? 'border-indigo-400 ring-2 ring-indigo-400/30' : 'border-slate-700'" :style="{ left: `${node.position.x}px`, top: `${node.position.y}px` }" @click.stop="selectNode(node)">
              <header class="px-3 py-2 border-b border-slate-700/80 bg-slate-900/60 flex items-center justify-between"><span class="font-semibold text-sm text-white">{{ node.data.table }}</span><span class="text-[10px] text-slate-500">{{ node.data.columns.length }} cols</span></header>
              <div v-for="column in node.data.columns" :key="column.name" class="px-3 py-1.5 border-b last:border-0 border-slate-800/80 flex items-center gap-2 text-[11px]" :class="search && (node.data.searchText || '').toLowerCase().includes(search.toLowerCase()) ? 'text-slate-200' : 'text-slate-400'"><KeyRound v-if="column.primaryKey" class="w-3 h-3 text-amber-300 shrink-0" /><Link2 v-else-if="column.foreignKey" class="w-3 h-3 text-indigo-300 shrink-0" /><span v-else class="w-3" /><span class="font-mono truncate">{{ column.name }}</span><span class="ml-auto text-slate-500">{{ column.type }}{{ column.nullable ? ' · null' : '' }}</span></div>
            </article>
          </div>
        </div>
      </div>

      <aside class="min-h-0 overflow-y-auto space-y-3">
        <div v-if="graph.warnings.length" class="rounded-2xl border border-amber-500/30 bg-amber-950/20 p-3"><h4 class="text-xs font-semibold text-amber-200 flex items-center gap-2"><AlertTriangle class="w-3.5 h-3.5" /> Warnings ({{ graph.warnings.length }})</h4><div v-for="warning in graph.warnings" :key="`${warning.code}-${warning.message}`" class="mt-2 text-[11px] text-amber-100/80">{{ warning.message }}</div></div>
        <div v-if="selectedTable" class="rounded-2xl border border-slate-800 bg-slate-900/40 p-3"><div class="flex items-center justify-between"><h4 class="text-sm font-semibold text-white">{{ selectedTable.data.table }}</h4><button @click="selectedTableId = ''" class="text-slate-500 hover:text-slate-300"><X class="w-3.5 h-3.5" /></button></div><div v-for="column in selectedTable.data.columns" :key="column.name" class="mt-3 text-[11px] border-b border-slate-800 pb-2"><div class="flex justify-between gap-2"><span class="font-mono text-slate-200">{{ column.name }}</span><span class="text-slate-500">{{ column.type }}</span></div><div class="mt-1 text-slate-500">{{ column.primaryKey ? 'Primary key · ' : '' }}{{ column.foreignKey ? `FK → ${column.references?.table}.${column.references?.column} · ` : '' }}{{ column.nullable ? 'nullable' : 'required' }} · default {{ formatDefault(column.default) }}{{ column.unique ? ' · unique' : '' }}{{ column.indexed ? ' · indexed' : '' }}</div></div></div>
        <div v-else-if="selectedEdge" class="rounded-2xl border border-slate-800 bg-slate-900/40 p-3"><div class="flex items-center justify-between"><h4 class="text-sm font-semibold text-white">{{ selectedEdge.cardinality }} relationship</h4><button @click="selectedEdgeId = ''" class="text-slate-500 hover:text-slate-300"><X class="w-3.5 h-3.5" /></button></div><p class="mt-3 text-xs text-slate-300">{{ selectedEdge.description }}</p><p class="mt-2 text-[11px] text-slate-500">{{ selectedEdge.provenance || 'static' }} · {{ selectedEdge.source }} → {{ selectedEdge.target }}</p></div>
        <div v-else class="rounded-2xl border border-slate-800 bg-slate-900/40 p-4 text-xs text-slate-500">Select a table or relationship for details.</div>
        <div class="rounded-2xl border border-slate-800 bg-slate-900/40 p-3 text-[11px] text-slate-500"><p>Source: <span class="text-slate-300">{{ graph.source.framework || 'unknown' }}</span></p><p class="mt-1">Generated {{ graph.source.generatedAt }}</p><p class="mt-1">Migration tables: {{ graph.source.migrationTableCount }} · Models: {{ graph.source.modelCount }}</p></div>
      </aside>
    </div>
  </section>
</template>
