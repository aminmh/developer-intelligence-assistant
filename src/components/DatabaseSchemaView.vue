<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { AlertTriangle, ChevronDown, ChevronRight, Database, Focus, KeyRound, Link2, Maximize2, Minus, PanelRightClose, PanelRightOpen, Plus, RefreshCw, Search, X } from 'lucide-vue-next';
import { Handle, Position, VueFlow, useVueFlow, type Edge, type Node } from '@vue-flow/core';
import '@vue-flow/core/dist/style.css';
import '@vue-flow/core/dist/theme-default.css';
import { makeRequest } from '../ui/makeRequest';
import type { AnalysisSession, SchemaVisualizationEdge, SchemaVisualizationNode, SchemaVisualizationResponse } from '../types';

const analyses = ref<AnalysisSession[]>([]);
const selectedAnalysisId = ref('');
const graph = ref<SchemaVisualizationResponse | null>(null);
const search = ref('');
const loading = ref(false);
const loadingAnalyses = ref(false);
const error = ref('');
const selectedTableId = ref('');
const selectedEdgeId = ref('');
const rightPanelOpen = ref(true);
const warningsExpanded = ref(true);

const { fitView, zoomIn, zoomOut } = useVueFlow('database-schema');
const visibleNodes = computed(() => {
  const query = search.value.trim().toLowerCase();
  if (!graph.value || !query) return graph.value?.nodes || [];
  return graph.value.nodes.filter((node) => (node.data.searchText || node.data.table).toLowerCase().includes(query));
});
const matchingNodeIds = computed(() => new Set(visibleNodes.value.map((node) => node.id)));
const visibleEdges = computed(() => (graph.value?.edges || []).filter((edge) => matchingNodeIds.value.has(`table:${edge.source}`) && matchingNodeIds.value.has(`table:${edge.target}`)));
const flowNodes = computed<Node[]>(() => visibleNodes.value.map((node) => ({ ...node, class: selectedTableId.value === node.id ? 'schema-node-selected' : '' })));
const flowEdges = computed<Edge[]>(() => visibleEdges.value.map((edge) => ({
  id: edge.id,
  source: `table:${edge.source}`,
  target: `table:${edge.target}`,
  label: edge.cardinality,
  class: `schema-edge-${edge.cardinality.replace(':', '-')}${selectedEdgeId.value === edge.id ? ' schema-edge-selected' : ''}`,
  data: edge,
})));
const selectedTable = computed(() => graph.value?.nodes.find((node) => node.id === selectedTableId.value) || null);
const selectedEdge = computed(() => graph.value?.edges.find((edge) => edge.id === selectedEdgeId.value) || null);

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
    fitView({ padding: 0.18, duration: 250 });
  } catch (cause: any) {
    error.value = cause?.message || 'Failed to load schema visualization.';
  } finally {
    loading.value = false;
  }
};

const resetLayout = () => fitView({ padding: 0.18, duration: 250 });
const selectNode = (node: SchemaVisualizationNode) => { selectedTableId.value = node.id; selectedEdgeId.value = ''; };
const selectEdge = (edge: SchemaVisualizationEdge) => { selectedEdgeId.value = edge.id; selectedTableId.value = ''; };
const formatDefault = (value: unknown) => value === null || value === undefined ? '—' : String(value);

watch(selectedAnalysisId, loadGraph);
onMounted(loadAnalyses);
</script>

<template>
  <section class="flex-1 min-h-0 overflow-hidden p-5 flex flex-col gap-4">
    <div class="flex flex-wrap items-center justify-between gap-3"><div><h3 class="text-lg font-bold text-white flex items-center gap-2"><Database class="w-5 h-5 text-indigo-400" /> Database Schema</h3><p class="text-xs text-slate-500 mt-1">Deterministic Laravel migration graph with optional Eloquent relationship enrichment.</p></div><button @click="loadAnalyses" class="px-3 py-2 rounded-xl border border-slate-700 text-xs text-slate-300 hover:bg-slate-800"><RefreshCw :class="loadingAnalyses ? 'animate-spin' : ''" class="w-3.5 h-3.5 inline mr-1" />Refresh</button></div>
    <div class="grid grid-cols-1 lg:grid-cols-[250px_1fr_auto] gap-2 rounded-2xl border border-slate-800 bg-slate-900/30 p-3"><select v-model="selectedAnalysisId" class="bg-[#11162d] border border-slate-800 rounded-xl px-3 py-2 text-xs text-slate-300"><option value="">Select saved analysis</option><option v-for="analysis in analyses" :key="analysis.id" :value="analysis.id">{{ analysis.label || analysis.filename }} · {{ analysis.created_at }}</option></select><div class="relative"><Search class="absolute left-3 top-2.5 w-3.5 h-3.5 text-slate-500" /><input v-model="search" placeholder="Search table or column…" class="w-full bg-[#11162d] border border-slate-800 rounded-xl pl-9 pr-3 py-2 text-xs text-slate-200" /></div><div class="flex items-center gap-1"><button @click="fitView({ padding: 0.2 })" title="Fit to view" class="p-2 rounded-lg border border-slate-700 text-slate-300 hover:bg-slate-800"><Maximize2 class="w-3.5 h-3.5" /></button><button @click="zoomIn" title="Zoom in" class="p-2 rounded-lg border border-slate-700 text-slate-300 hover:bg-slate-800"><Plus class="w-3.5 h-3.5" /></button><button @click="zoomOut" title="Zoom out" class="p-2 rounded-lg border border-slate-700 text-slate-300 hover:bg-slate-800"><Minus class="w-3.5 h-3.5" /></button><button @click="resetLayout" title="Reset layout" class="p-2 rounded-lg border border-slate-700 text-slate-300 hover:bg-slate-800"><Focus class="w-3.5 h-3.5" /></button></div></div>
    <div v-if="error" class="rounded-xl border border-rose-500/30 bg-rose-950/20 p-4 text-xs text-rose-300">{{ error }}</div><div v-else-if="!selectedAnalysisId" class="flex-1 rounded-2xl border border-dashed border-slate-700 p-10 text-center text-sm text-slate-500 flex items-center justify-center">Choose a saved analysis to inspect its database schema.</div><div v-else-if="loading" class="flex-1 rounded-2xl border border-slate-800 bg-slate-900/30 flex items-center justify-center text-sm text-slate-500">Loading schema graph…</div><div v-else-if="graph && !graph.nodes.length" class="flex-1 rounded-2xl border border-dashed border-slate-700 p-10 text-center text-sm text-slate-500 flex items-center justify-center">{{ graph.source.framework?.toLowerCase() === 'laravel' ? 'This Laravel analysis contains no migration-derived tables.' : 'Schema visualization is available for Laravel analyses with migrations.' }}</div>
    <div v-else-if="graph" class="flex-1 min-h-0 grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_var(--schema-rail)] gap-3" :style="{ '--schema-rail': rightPanelOpen ? '320px' : '48px' }">
      <div class="min-h-0 rounded-2xl border border-slate-800 bg-[#0b1020] overflow-hidden relative">
        <div class="absolute top-3 left-3 z-10 text-[10px] text-slate-400 bg-slate-950/80 border border-slate-800 rounded-lg px-2 py-1">{{ visibleNodes.length }} table(s) · {{ visibleEdges.length }} relationship(s)</div>
        <div class="absolute top-3 right-3 z-10 flex items-center gap-3 text-[10px] text-slate-400 bg-slate-950/80 border border-slate-800 rounded-lg px-2 py-1">
          <span><i class="inline-block w-2 h-2 rounded-full bg-indigo-400 mr-1" />1:N</span>
          <span><i class="inline-block w-2 h-2 rounded-full bg-emerald-400 mr-1" />1:1</span>
          <span><i class="inline-block w-2 h-2 rounded-full bg-amber-400 mr-1" />N:M</span>
        </div>
        <VueFlow id="database-schema" :nodes="flowNodes" :edges="flowEdges" :fit-view-on-init="false" :nodes-draggable="true" :nodes-connectable="false" :min-zoom="0.15" :max-zoom="2.5" @node-click="({ node }) => selectNode(node as SchemaVisualizationNode)" @edge-click="({ edge }) => selectEdge(edge.data as SchemaVisualizationEdge)">
          <template #node-databaseTable="{ data }">
            <article class="relative w-[280px] rounded-xl border border-slate-700 bg-[#11182d] shadow-xl shadow-black/20 overflow-visible">
              <Handle type="target" :position="Position.Left" class="!h-2.5 !w-2.5 !border-2 !border-slate-900 !bg-indigo-400" />
              <Handle type="source" :position="Position.Right" class="!h-2.5 !w-2.5 !border-2 !border-slate-900 !bg-indigo-400" />
              <header class="rounded-t-xl px-3 py-2 border-b border-slate-700/80 bg-slate-900/60 flex items-center justify-between">
                <span class="font-semibold text-sm text-white truncate pr-2">{{ data.table }}</span>
                <span class="text-[10px] text-slate-500 shrink-0">{{ data.columns.length }} cols</span>
              </header>
              <div v-for="column in data.columns" :key="column.name" class="px-3 py-1.5 border-b last:border-0 border-slate-800/80 flex items-center gap-2 text-[11px] text-slate-300">
                <KeyRound v-if="column.primaryKey" class="w-3 h-3 text-amber-300 shrink-0" />
                <Link2 v-else-if="column.foreignKey" class="w-3 h-3 text-indigo-300 shrink-0" />
                <span v-else class="w-3" />
                <span class="font-mono truncate">{{ column.name }}</span>
                <span class="ml-auto text-slate-500">{{ column.type }}{{ column.nullable ? ' · null' : '' }}</span>
              </div>
            </article>
          </template>
        </VueFlow>
      </div>
      <aside class="min-h-0 overflow-hidden rounded-2xl border border-slate-800 bg-slate-900/30">
        <div class="flex h-full flex-col">
          <div class="flex items-center justify-between border-b border-slate-800 px-3 py-2">
            <h4 v-if="rightPanelOpen" class="text-xs font-semibold uppercase tracking-wide text-slate-400">Schema details</h4>
            <button @click="rightPanelOpen = !rightPanelOpen" class="rounded-lg p-1.5 text-slate-400 hover:bg-slate-800 hover:text-white" :title="rightPanelOpen ? 'Collapse details panel' : 'Expand details panel'">
              <PanelRightClose v-if="rightPanelOpen" class="h-4 w-4" />
              <PanelRightOpen v-else class="h-4 w-4" />
            </button>
          </div>
          <div v-if="rightPanelOpen" class="min-h-0 flex-1 overflow-y-auto space-y-3 p-3">
            <div v-if="graph.warnings.length" class="rounded-2xl border border-amber-500/30 bg-amber-950/20 p-3">
              <button @click="warningsExpanded = !warningsExpanded" class="flex w-full items-center justify-between text-left">
                <span class="text-xs font-semibold text-amber-200 flex items-center gap-2"><AlertTriangle class="w-3.5 h-3.5" /> Warnings ({{ graph.warnings.length }})</span>
                <ChevronDown v-if="warningsExpanded" class="h-3.5 w-3.5 text-amber-300" />
                <ChevronRight v-else class="h-3.5 w-3.5 text-amber-300" />
              </button>
              <div v-if="warningsExpanded" class="mt-2 space-y-2">
                <div v-for="warning in graph.warnings" :key="warning.code + warning.message" class="rounded-lg border border-amber-500/10 bg-black/10 p-2 text-[11px] text-amber-100/80">{{ warning.message }}</div>
              </div>
            </div>
            <div v-if="selectedTable" class="rounded-2xl border border-slate-800 bg-slate-900/40 p-3">
              <div class="flex items-center justify-between"><h4 class="text-sm font-semibold text-white">{{ selectedTable.data.table }}</h4><button @click="selectedTableId = ''" class="text-slate-500 hover:text-slate-300"><X class="w-3.5 h-3.5" /></button></div>
              <div v-for="column in selectedTable.data.columns" :key="column.name" class="mt-3 text-[11px] border-b border-slate-800 pb-2">
                <div class="flex justify-between gap-2"><span class="font-mono text-slate-200">{{ column.name }}</span><span class="text-slate-500">{{ column.type }}</span></div>
                <div class="mt-1 text-slate-500">{{ column.primaryKey ? 'Primary key · ' : '' }}{{ column.foreignKey ? 'FK → ' + column.references?.table + '.' + column.references?.column + ' · ' : '' }}{{ column.nullable ? 'nullable' : 'required' }} · default {{ formatDefault(column.default) }}{{ column.unique ? ' · unique' : '' }}{{ column.indexed ? ' · indexed' : '' }}</div>
              </div>
            </div>
            <div v-else-if="selectedEdge" class="rounded-2xl border border-slate-800 bg-slate-900/40 p-3">
              <div class="flex items-center justify-between"><h4 class="text-sm font-semibold text-white">{{ selectedEdge.cardinality }} relationship</h4><button @click="selectedEdgeId = ''" class="text-slate-500 hover:text-slate-300"><X class="w-3.5 h-3.5" /></button></div>
              <p class="mt-3 text-xs text-slate-300">{{ selectedEdge.description }}</p>
              <p class="mt-2 text-[11px] text-slate-500">{{ selectedEdge.provenance || 'static' }} · {{ selectedEdge.source }} → {{ selectedEdge.target }}</p>
            </div>
            <div v-else class="rounded-2xl border border-slate-800 bg-slate-900/40 p-4 text-xs text-slate-500">Select a table or relationship for details.</div>
            <div class="rounded-2xl border border-slate-800 bg-slate-900/40 p-3 text-[11px] text-slate-500">
              <p>Source: <span class="text-slate-300">{{ graph.source.framework || 'unknown' }}</span></p>
              <p class="mt-1">Generated {{ graph.source.generatedAt }}</p>
              <p class="mt-1">Migration tables: {{ graph.source.migrationTableCount }} · Models: {{ graph.source.modelCount }}</p>
            </div>
          </div>
        </div>
      </aside>
    </div>
  </section>
</template>

<style scoped>
:deep(.vue-flow__edge-path) {
  stroke-width: 1.6;
}

:deep(.vue-flow__edge-textbg) {
  fill: #0b1020;
  opacity: 0.92;
}

:deep(.vue-flow__edge-text) {
  fill: #cbd5e1;
  font-size: 10px;
  font-weight: 600;
}

:deep(.schema-edge-1-1 .vue-flow__edge-path) {
  stroke: #34d399;
}

:deep(.schema-edge-1-N .vue-flow__edge-path) {
  stroke: #818cf8;
}

:deep(.schema-edge-N-M .vue-flow__edge-path) {
  stroke: #fbbf24;
  stroke-dasharray: 5 4;
}

:deep(.schema-edge-selected .vue-flow__edge-path) {
  stroke: #f8fafc;
  stroke-width: 2.5;
}

:deep(.schema-node-selected) {
  filter: drop-shadow(0 0 8px rgba(129, 140, 248, 0.45));
}
</style>
