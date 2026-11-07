<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { AlertTriangle, ChevronDown, ChevronRight, Database, Download, Focus, KeyRound, Link2, Maximize2, Minus, PanelRightClose, PanelRightOpen, Plus, RefreshCw, Search, Sparkles, Trash2, X } from 'lucide-vue-next';
import { makeRequest } from '../ui/makeRequest';
import { subscribeRuntimeUpdates } from '../ui/runtimeEvents';
import type { AnalysisSession, ErdRef, ErdTable, NormalizedSchema, RuntimeProjectRecord, RuntimeSnapshotData, SchemaVisualizationResponse } from '../types';
import { layoutSchema, normalizeSavedSchema, parseSchemaInput, schemaToDbml, schemaToJson, type SchemaInputFormat } from '../utils/erdTransformer';
import ConfirmModal from './ConfirmModal.vue';
import ErdCanvas from './ErdCanvas.vue';
import SchemaDraftPanel from './SchemaDraftPanel.vue';
import RuleLinksPanel from './code-quality/RuleLinksPanel.vue';

const analyses = ref<AnalysisSession[]>([]);
const selectedAnalysisId = ref('');
const sourceType = ref<'saved' | SchemaInputFormat>('saved');
const inputFormat = ref<SchemaInputFormat>('sql');
const inputText = ref('');
const schema = ref<NormalizedSchema | null>(null);
let schemaRequest = 0;
const search = ref('');
const loading = ref(false);
const loadingAnalyses = ref(false);
const parsing = ref(false);
const error = ref('');
const selectedTableId = ref('');
const selectedRefId = ref('');
const rightPanelOpen = ref(true);
const warningsExpanded = ref(true);
const deleteTarget = ref<AnalysisSession | null>(null);
const deletingAnalysis = ref(false);
const canvas = ref<InstanceType<typeof ErdCanvas> | null>(null);
const runtimeProjects = ref<RuntimeProjectRecord[]>([]);
const runtimeScope = ref('');
const runtimeData = ref<RuntimeSnapshotData | null>(null);
const runtimeError = ref('');
const runtimeOptions = computed(() => runtimeProjects.value.filter((item) => item.commit_sha && item.revision_id));
const tableRuntime = computed(() => runtimeData.value?.telemetry.tables.filter((row) => row.table_name.toLowerCase() === selectedTable.value?.name.toLowerCase()) || []);
const tableSymbols = computed(() => runtimeData.value?.telemetry.symbol_tables.filter((row) => row.table_name.toLowerCase() === selectedTable.value?.name.toLowerCase()) || []);
// Business rules linked to the selected table or to the code that uses it.
const runtimeProject = computed(() => { const [project_id = '', revision_id = ''] = runtimeScope.value.split(':'); return { project_id, revision_id }; });
const tableRuleIds = computed(() => [...new Set([
  ...((runtimeData.value as any)?.telemetry.tables || []).filter((row: any) => row.table_name.toLowerCase() === selectedTable.value?.name.toLowerCase()).map((row: any) => row.table_id),
  ...tableSymbols.value.map((row) => row.symbol_id)])].filter(Boolean));
const tableRoutes = computed(() => {
  const ids = new Set(tableSymbols.value.map((row) => row.symbol_id));
  return runtimeData.value?.telemetry.symbol_routes.filter((row) => ids.has(row.symbol_id)) || [];
});
const busyTableCounts = computed(() => Object.fromEntries((runtimeData.value?.telemetry.tables || []).reduce((counts, row) => {
  const key = row.table_name.toLowerCase();
  counts.set(key, (counts.get(key) || 0) + Number(row.operations || 0));
  return counts;
}, new Map<string, number>())));

const loadRuntime = async () => {
  runtimeError.value = '';
  try {
    if (!runtimeProjects.value.length) {
      const response = await makeRequest('/api/runtime/projects');
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Runtime project list unavailable.');
      runtimeProjects.value = data.projects || [];
    }
    if (!runtimeScope.value && runtimeOptions.value.length) {
      const params = new URLSearchParams(window.location.search);
      const project = runtimeOptions.value.find((item) => item.project_id === params.get('project_id')
        && (params.get('revision_id')
          ? item.revision_id === params.get('revision_id')
          : item.commit_sha === params.get('commit_sha'))) || runtimeOptions.value[0];
      runtimeScope.value = `${project.project_id}:${project.revision_id}`;
    }
    const [projectId, revisionId] = runtimeScope.value.split(':');
    if (!projectId || !revisionId) { runtimeData.value = null; return; }
    const response = await makeRequest(`/api/runtime/projects/${encodeURIComponent(projectId)}/revisions/${encodeURIComponent(revisionId)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Runtime table usage unavailable.');
    runtimeData.value = data;
  } catch (cause: any) {
    runtimeError.value = cause?.message || 'Runtime table usage unavailable.';
    runtimeData.value = null;
  }
};
const selectedTable = computed(() => schema.value?.tables.find((table) => table.id === selectedTableId.value) || null);
const selectedRef = computed(() => schema.value?.refs.find((item) => item.id === selectedRefId.value) || null);
const visibleTables = computed(() => {
  const query = search.value.trim().toLowerCase();
  if (!schema.value || !query) return schema.value?.tables || [];
  return schema.value.tables.filter((table) => `${table.name} ${table.fields.map((field) => field.name).join(' ')}`.toLowerCase().includes(query));
});
const selectedAnalysis = computed(() => analyses.value.find((analysis) => analysis.id === selectedAnalysisId.value) || null);

const loadAnalyses = async () => {
  loadingAnalyses.value = true;
  error.value = '';
  try {
    const response = await makeRequest('/api/analysis?limit=100');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || data.error || 'Failed to load saved analyses.');
    analyses.value = (data.analyses || []).filter((item: AnalysisSession) => item.kind !== 'code_index');
    if (!analyses.value.some((analysis) => analysis.id === selectedAnalysisId.value)) selectedAnalysisId.value = analyses.value[0]?.id || '';
  } catch (cause: any) {
    error.value = cause?.message || 'Failed to load saved analyses.';
  } finally {
    loadingAnalyses.value = false;
  }
};

const loadSaved = async () => {
  const requestId = ++schemaRequest;
  const analysisId = selectedAnalysisId.value;
  schema.value = null;
  if (!selectedAnalysisId.value) {
    loading.value = false;
    return;
  }
  loading.value = true;
  error.value = '';
  selectedTableId.value = '';
  selectedRefId.value = '';
  try {
    const response = await makeRequest(`/api/analysis/${encodeURIComponent(analysisId)}/schema-visualization`);
    const data = await response.json() as SchemaVisualizationResponse & { normalizedSchema?: NormalizedSchema; detail?: string };
    if (!response.ok) throw new Error(data.detail || 'Failed to load schema visualization.');
    const result = await layoutSchema(data.normalizedSchema || normalizeSavedSchema(data));
    if (requestId === schemaRequest) schema.value = result;
  } catch (cause: any) {
    if (requestId === schemaRequest) { schema.value = null; error.value = cause?.message || 'Failed to load schema visualization.'; }
  } finally {
    if (requestId === schemaRequest) loading.value = false;
  }
};

const parseImport = async () => {
  if (!inputText.value.trim()) {
    error.value = 'Paste SQL, DBML, or normalized JSON before importing.';
    return;
  }
  const requestId = ++schemaRequest;
  schema.value = null;
  parsing.value = true;
  error.value = '';
  selectedTableId.value = '';
  selectedRefId.value = '';
  try {
    const parsed = await parseSchemaInput(inputText.value, { format: inputFormat.value, source: { kind: inputFormat.value } });
    const result = await layoutSchema(parsed);
    if (requestId === schemaRequest) schema.value = result;
  } catch (cause: any) {
    if (requestId === schemaRequest) { schema.value = null; error.value = cause?.message || 'Could not parse schema input.'; }
  } finally {
    if (requestId === schemaRequest) parsing.value = false;
  }
};

const loadSample = () => {
  inputFormat.value = 'sql';
  sourceType.value = 'sql';
  inputText.value = 'CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE);\nCREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT, FOREIGN KEY (user_id) REFERENCES users(id));';
  void parseImport();
};

const clearImport = () => {
  schemaRequest++;
  parsing.value = false;
  inputText.value = '';
  if (sourceType.value !== 'saved') schema.value = null;
  error.value = '';
};

const selectTable = (table: ErdTable) => {
  selectedTableId.value = table.id;
  selectedRefId.value = '';
};

const selectRef = (item: ErdRef) => {
  selectedRefId.value = item.id;
  selectedTableId.value = '';
};

const formatDefault = (value: unknown) => value === null || value === undefined || value === '' ? '—' : String(value);
const download = (content: string, name: string, type: string) => {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  URL.revokeObjectURL(url);
};
const exportDbml = () => schema.value && download(schemaToDbml(schema.value), 'schema.dbml', 'text/plain');
const exportJson = () => schema.value && download(schemaToJson(schema.value), 'schema.json', 'application/json');
const requestDeleteAnalysis = () => { if (selectedAnalysis.value) deleteTarget.value = selectedAnalysis.value; };

const deleteAnalysis = async () => {
  const target = deleteTarget.value;
  if (!target || deletingAnalysis.value) return;
  deletingAnalysis.value = true;
  error.value = '';
  try {
    const response = await makeRequest(`/api/analysis/${encodeURIComponent(target.id)}`, { method: 'DELETE' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.success) throw new Error(data.error || data.detail || 'Failed to delete saved analysis.');
    deleteTarget.value = null;
    selectedAnalysisId.value = '';
    await loadAnalyses();
  } catch (cause: any) {
    error.value = cause?.message || 'Failed to delete saved analysis.';
  } finally {
    deletingAnalysis.value = false;
  }
};

watch(selectedAnalysisId, () => { if (sourceType.value === 'saved') void loadSaved(); });
watch(sourceType, (value) => {
  schemaRequest++;
  schema.value = null;
  loading.value = false;
  parsing.value = false;
  if (value === 'saved') void loadSaved();
}, { flush: 'sync' });
watch(runtimeScope, () => { void loadRuntime(); });
let stopRuntimeUpdates: (() => void) | undefined;
onMounted(async () => {
  await Promise.all([loadAnalyses(), loadRuntime()]);
  stopRuntimeUpdates = subscribeRuntimeUpdates(() => { void loadRuntime(); });
});
onUnmounted(() => stopRuntimeUpdates?.());
</script>

<template>
  <section class="flex flex-1 min-h-0 flex-col gap-4 overflow-hidden p-5">
    <div class="flex flex-wrap items-center justify-between gap-3"><div><h3 class="flex items-center gap-2 text-lg font-bold text-white"><Database class="h-5 w-5 text-indigo-400" /> Database Schema</h3><p class="mt-1 text-xs text-slate-500">Explore saved analyses and imported schemas; create a draft to save edits.</p></div><button type="button" @click="loadAnalyses" class="rounded-xl border border-slate-700 px-3 py-2 text-xs text-slate-300 hover:bg-slate-800"><RefreshCw :class="loadingAnalyses ? 'animate-spin' : ''" class="mr-1 inline h-3.5 w-3.5" />Refresh</button></div>
    <div class="grid grid-cols-1 gap-2 rounded-2xl border border-slate-800 bg-slate-900/30 p-3 xl:grid-cols-[250px_minmax(0,1fr)]"><div class="flex min-w-0 gap-1"><select v-model="selectedAnalysisId" class="min-w-0 flex-1 rounded-xl border border-slate-800 bg-[#11162d] px-3 py-2 text-xs text-slate-300"><option value="">Select saved analysis</option><option v-for="analysis in analyses" :key="analysis.id" :value="analysis.id">{{ analysis.label || analysis.filename }} · {{ analysis.created_at }}</option></select><button v-if="selectedAnalysis" type="button" @click="requestDeleteAnalysis" :disabled="deletingAnalysis" title="Remove selected analyzed project" class="shrink-0 rounded-xl border border-rose-500/30 px-2.5 py-2 text-rose-300 hover:bg-rose-500/10 disabled:opacity-50"><Trash2 class="h-3.5 w-3.5" /></button></div><div class="flex flex-wrap items-center gap-2"><select v-model="sourceType" class="rounded-xl border border-slate-800 bg-[#11162d] px-3 py-2 text-xs text-slate-300"><option value="saved">Saved analysis</option><option value="sql">SQL DDL</option><option value="dbml">DBML</option><option value="json">Normalized JSON</option></select><select v-if="sourceType !== 'saved'" v-model="inputFormat" class="rounded-xl border border-slate-800 bg-[#11162d] px-3 py-2 text-xs text-slate-300"><option value="sql">MySQL / PostgreSQL / SQLite</option><option value="dbml">DBML</option><option value="json">JSON</option></select><button v-if="sourceType !== 'saved'" type="button" @click="parseImport" :disabled="parsing" class="rounded-xl border border-indigo-400/30 bg-indigo-500/20 px-3 py-2 text-xs text-indigo-200">{{ parsing ? 'Parsing…' : 'Import' }}</button><button v-if="sourceType !== 'saved'" type="button" @click="loadSample" class="rounded-xl border border-slate-700 px-3 py-2 text-xs text-slate-300">Sample</button><button v-if="sourceType !== 'saved'" type="button" @click="clearImport" class="rounded-xl border border-slate-700 px-3 py-2 text-xs text-slate-300">Clear</button><select v-model="runtimeScope" class="max-w-full rounded-xl border border-slate-800 bg-[#11162d] px-3 py-2 text-xs text-slate-300"><option value="">Runtime project for table usage</option><option v-for="item in runtimeOptions" :key="item.project_id + item.revision_id" :value="item.project_id + ':' + item.revision_id">{{ item.project_id }} · {{ item.revision_id ? `Revision ${item.revision_id.slice(4, 12)}` : 'Revision unavailable' }}</option></select></div></div>
    <SchemaDraftPanel :schema="schema" :source-type="sourceType" :analysis-id="selectedAnalysisId" :runtime-counts="busyTableCounts" />
    <div v-if="runtimeError" class="rounded-xl border border-amber-500/20 bg-amber-500/5 p-3 text-xs text-amber-200">{{ runtimeError }}</div>
    <div v-if="sourceType !== 'saved'" class="grid grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1fr)_320px]"><textarea v-model="inputText" class="min-h-28 rounded-2xl border border-slate-800 bg-[#0b1020] p-3 text-xs font-mono text-slate-200 outline-none focus:border-indigo-400/60" :placeholder="inputFormat === 'dbml' ? 'Table users { id int [pk] }' : inputFormat === 'json' ? '{ &quot;tables&quot;: [], &quot;refs&quot;: [] }' : 'CREATE TABLE users (...);'" /><div class="rounded-2xl border border-slate-800 bg-slate-900/30 p-3 text-xs text-slate-500">Imports stay in this session until you create a draft. Unsupported or ambiguous references appear as warnings.</div></div>
    <div v-if="error" class="rounded-xl border border-rose-500/30 bg-rose-950/20 p-4 text-xs text-rose-300">{{ error }}</div><div v-else-if="loading" class="flex flex-1 items-center justify-center rounded-2xl border border-slate-800 bg-slate-900/30 text-sm text-slate-500">Loading schema graph…</div><div v-else-if="!schema" class="flex flex-1 flex-col items-center justify-center gap-3 rounded-2xl border border-dashed border-slate-700 p-10 text-center text-sm text-slate-500">Choose a saved analysis or import SQL, DBML, or normalized JSON.<button type="button" @click="loadSample" class="rounded-xl border border-slate-700 px-3 py-2 text-xs text-slate-300">Load sample schema</button></div>
    <div v-else class="schema-layout flex flex-1 min-h-0 grid grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1fr)_var(--schema-rail)]" :style="{ '--schema-rail': rightPanelOpen ? '320px' : '48px' }"><div class="relative min-h-0 overflow-hidden rounded-2xl border border-slate-800 bg-[#0b1020]"><div class="absolute left-3 top-3 z-10 rounded-lg border border-slate-800 bg-slate-950/80 px-2 py-1 text-[10px] text-slate-400">{{ visibleTables.length }} table(s) · {{ schema.refs.length }} relationship(s)</div><div class="absolute right-3 top-3 z-10 flex items-center gap-1 rounded-lg border border-slate-800 bg-slate-950/80 px-2 py-1"><input v-model="search" class="w-28 bg-transparent text-[10px] text-slate-200 outline-none" placeholder="Search tables…" /><Search class="h-3.5 w-3.5 text-slate-500" /><button type="button" @click="canvas?.fit()" title="Fit to view" class="p-1 text-slate-300"><Maximize2 class="h-3.5 w-3.5" /></button><button type="button" @click="canvas?.zoomIn()" title="Zoom in" class="p-1 text-slate-300"><Plus class="h-3.5 w-3.5" /></button><button type="button" @click="canvas?.zoomOut()" title="Zoom out" class="p-1 text-slate-300"><Minus class="h-3.5 w-3.5" /></button><button type="button" @click="canvas?.reset()" title="Reset layout" class="p-1 text-slate-300"><Focus class="h-3.5 w-3.5" /></button><button type="button" @click="canvas?.prettify()" title="Prettify layout" class="p-1 text-slate-300"><Sparkles class="h-3.5 w-3.5" /></button><button type="button" @click="canvas?.exportSvg()" title="Export SVG" class="p-1 text-slate-300"><Download class="h-3.5 w-3.5" /></button><button type="button" @click="canvas?.exportPng()" title="Export PNG" class="p-1 text-slate-300"><span class="text-[9px]">PNG</span></button><button type="button" @click="exportDbml" title="Export DBML" class="p-1 text-slate-300"><span class="text-[9px]">DBML</span></button><button type="button" @click="exportJson" title="Export JSON" class="p-1 text-slate-300"><span class="text-[9px]">JSON</span></button></div><ErdCanvas ref="canvas" :schema="schema" :busy-table-counts="busyTableCounts" :search="search" :selected-table-id="selectedTableId" :selected-ref-id="selectedRefId" @select-table="selectTable" @select-ref="selectRef" /></div>
      <aside class="min-h-0 overflow-hidden rounded-2xl border border-slate-800 bg-slate-900/30"><div class="flex h-full flex-col"><div class="flex items-center justify-between border-b border-slate-800 px-3 py-2"><h4 v-if="rightPanelOpen" class="text-xs font-semibold uppercase tracking-wide text-slate-400">Schema details</h4><button type="button" @click.stop="rightPanelOpen = !rightPanelOpen" class="rounded-lg p-1.5 text-slate-400 hover:bg-slate-800 hover:text-white" :title="rightPanelOpen ? 'Collapse details panel' : 'Expand details panel'"><PanelRightClose v-if="rightPanelOpen" class="h-4 w-4" /><PanelRightOpen v-else class="h-4 w-4" /></button></div><div v-if="rightPanelOpen" class="min-h-0 flex-1 space-y-3 overflow-y-auto p-3"><div v-if="schema.warnings.length" class="rounded-2xl border border-amber-500/30 bg-amber-950/20 p-3"><button type="button" @click="warningsExpanded = !warningsExpanded" class="flex w-full items-center justify-between text-left"><span class="flex items-center gap-2 text-xs font-semibold text-amber-200"><AlertTriangle class="h-3.5 w-3.5" /> Warnings ({{ schema.warnings.length }})</span><ChevronDown v-if="warningsExpanded" class="h-3.5 w-3.5 text-amber-300" /><ChevronRight v-else class="h-3.5 w-3.5 text-amber-300" /></button><div v-if="warningsExpanded" class="mt-2 space-y-2"><div v-for="warning in schema.warnings" :key="warning.code + warning.message" class="rounded-lg border border-amber-500/10 bg-black/10 p-2 text-[11px] text-amber-100/80">{{ warning.message }}</div></div></div><div v-if="selectedTable" class="rounded-2xl border border-slate-800 bg-slate-900/40 p-3"><div class="flex items-center justify-between"><h4 class="text-sm font-semibold text-white">{{ selectedTable.name }}</h4><button type="button" @click="selectedTableId = ''" class="text-slate-500 hover:text-slate-300"><X class="h-3.5 w-3.5" /></button></div><div v-if="runtimeData" class="mt-3 rounded-xl border border-cyan-500/20 bg-cyan-500/5 p-3"><p class="text-[10px] font-semibold uppercase text-cyan-200">Runtime usage · {{ runtimeData.telemetry.retention_hours / 24 }} retained days</p><p v-if="tableRuntime.length" class="mt-2 text-[11px] text-slate-300">{{ tableRuntime.reduce((n, row) => n + Number(row.operations || 0), 0).toLocaleString() }} observed operations <span v-for="row in tableRuntime" :key="row.operation" class="ml-2 text-slate-400">{{ row.operation || 'other' }}: {{ row.operations }}</span></p><p v-else class="mt-2 text-[11px] text-slate-500">No operations observed for this table.</p><div v-if="tableSymbols.length" class="mt-2"><p class="text-[10px] uppercase text-slate-500">Code using table</p><span v-for="row in tableSymbols" :key="row.symbol_id" class="mr-2 mt-1 inline-block font-mono text-[10px] text-cyan-300">{{ row.symbol_id }} · {{ row.calls }}</span></div><p v-if="tableRoutes.length" class="mt-2 text-[10px] text-slate-400">Routes: {{ [...new Set(tableRoutes.map((row) => row.route))].join(', ') }}</p></div><RuleLinksPanel v-if="runtimeProject.project_id && tableRuleIds.length" class="mt-3" compact :project-id="runtimeProject.project_id" :revision-id="runtimeProject.revision_id" :symbol-ids="tableRuleIds" /><div v-for="field in selectedTable.fields" :key="field.id" class="mt-3 border-b border-slate-800 pb-2 text-[11px]"><div class="flex justify-between gap-2"><span class="font-mono text-slate-200"><KeyRound v-if="field.primaryKey" class="inline h-3 w-3 text-amber-300" /><Link2 v-else-if="field.foreignKey" class="inline h-3 w-3 text-indigo-300" /> {{ field.name }}</span><span class="text-slate-500">{{ field.type }}</span></div><div class="mt-1 text-slate-500">{{ field.primaryKey ? 'Primary key · ' : '' }}{{ field.references ? 'FK → ' + field.references.table + '.' + field.references.column + ' · ' : '' }}{{ field.nullable ? 'nullable' : 'required' }} · default {{ formatDefault(field.default) }}{{ field.unique ? ' · unique' : '' }}</div></div></div><div v-else-if="selectedRef" class="rounded-2xl border border-slate-800 bg-slate-900/40 p-3"><div class="flex items-center justify-between"><h4 class="text-sm font-semibold text-white">{{ selectedRef.cardinality }} relationship</h4><button type="button" @click="selectedRefId = ''" class="text-slate-500 hover:text-slate-300"><X class="h-3.5 w-3.5" /></button></div><p class="mt-3 text-xs text-slate-300">{{ selectedRef.description || `${selectedRef.sourceTable}.${selectedRef.sourceColumn} → ${selectedRef.targetTable}.${selectedRef.targetColumn}` }}</p><p class="mt-2 text-[11px] text-slate-500">{{ selectedRef.provenance || 'static' }}</p></div><div v-else class="rounded-2xl border border-slate-800 bg-slate-900/40 p-4 text-xs text-slate-500">Select a table or relationship for details.</div><div class="rounded-2xl border border-slate-800 bg-slate-900/40 p-3 text-[11px] text-slate-500"><p>Source: <span class="text-slate-300">{{ schema.source.framework || schema.source.kind || 'unknown' }}</span></p><p class="mt-1">{{ schema.tables.length }} tables · {{ schema.refs.length }} refs</p></div></div></div></aside>
    </div>
    <ConfirmModal :show="!!deleteTarget" title="Remove analyzed project?" :message="deleteTarget ? `This removes ${deleteTarget.label || deleteTarget.filename} and its saved analysis artifacts. This cannot be undone.` : ''" confirm-label="Remove project" :cancel-label="deletingAnalysis ? 'Please wait…' : 'Cancel'" @confirm="deleteAnalysis" @cancel="deleteTarget = null" />
  </section>
</template>

<style scoped>
@media (min-width: 1280px) {
  .schema-layout {
    grid-template-columns: minmax(0, 1fr) var(--schema-rail);
  }
}
</style>
