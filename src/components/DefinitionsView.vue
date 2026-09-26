<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { ChevronDown, ChevronRight, Code2, RefreshCw, Search } from 'lucide-vue-next';
import { makeRequest } from '../ui/makeRequest';
import type { DefinitionKind, ExtractedDefinition, ExtractedDefinitionsResponse, IndexedSymbolProject, RuntimeSnapshotData } from '../types';

const projects = ref<IndexedSymbolProject[]>([]);
const selectedStem = ref('');
const definitions = ref<ExtractedDefinition[]>([]);
const search = ref('');
const kind = ref('');
const file = ref('');
const loading = ref(false);
const error = ref('');
const total = ref(0);
const offset = ref(0);
const limit = 100;
const expanded = ref(new Set<string>());
const runtimeData = ref<RuntimeSnapshotData | null>(null);
const runtimeFilter = ref<'all' | 'unobserved' | 'hot'>('all');
const runtimeScopeKey = ref('');
const runtimeMetrics = computed(() => new Map((runtimeData.value?.telemetry.symbols || []).map((item) => [item.symbol_id, item])));
const selectedProject = computed(() => projects.value.find((project) => project.collection === selectedStem.value) || null);

const definitionMetric = (definition: ExtractedDefinition) => {
  if (definition.symbol_id && runtimeMetrics.value.has(definition.symbol_id)) return runtimeMetrics.value.get(definition.symbol_id)!;
  if (definition.kind !== 'class') return null;
  const children = definitions.value.filter((item) => item.kind === 'method' && item.owner === definition.fqn)
    .map((item) => item.symbol_id ? runtimeMetrics.value.get(item.symbol_id) : undefined).filter(Boolean) as NonNullable<ReturnType<typeof runtimeMetrics.value.get>>[];
  if (!children.length) return null;
  return {
    symbol_id: definition.symbol_id || '',
    runs: children.reduce((total, row) => total + Number(row.runs || 0), 0),
    last_seen: children.map((row) => row.last_seen).filter(Boolean).sort().at(-1) || null,
    p95_ms: Math.max(...children.map((row) => Number(row.p95_ms || 0))),
    failures: children.reduce((total, row) => total + Number(row.failures || 0), 0),
  };
};
const visibleDefinitions = computed(() => definitions.value.filter((definition) => {
  const text = `${definition.name} ${definition.fqn} ${definition.owner || ''} ${definition.file}`.toLowerCase();
  if (search.value.trim() && !text.includes(search.value.trim().toLowerCase())) return false;
  if (kind.value && definition.kind !== kind.value) return false;
  if (file.value.trim() && !definition.file.toLowerCase().includes(file.value.trim().toLowerCase())) return false;
  if (runtimeFilter.value === 'all' || !runtimeData.value || !['class', 'method', 'function'].includes(definition.kind)) return true;
  const metric = definitionMetric(definition);
  return runtimeFilter.value === 'unobserved' ? !metric : Number(metric?.runs || 0) >= 100;
}));
const pageDefinitions = computed(() => runtimeScopeKey.value
  ? visibleDefinitions.value.slice(offset.value, offset.value + limit)
  : visibleDefinitions.value);

const kinds: Array<{ value: DefinitionKind | ''; label: string }> = [
  { value: '', label: 'All kinds' },
  { value: 'class', label: 'Classes' },
  { value: 'interface', label: 'Interfaces' },
  { value: 'trait', label: 'Traits' },
  { value: 'enum', label: 'Enums' },
  { value: 'method', label: 'Methods' },
  { value: 'property', label: 'Properties' },
  { value: 'class_const', label: 'Class constants' },
  { value: 'function', label: 'Functions' },
  { value: 'const', label: 'Constants' },
];

const grouped = computed(() => {
  const groups = new Map<string, { owner: string; file: string; items: ExtractedDefinition[] }>();
  for (const definition of pageDefinitions.value) {
    const owner = definition.owner || (['class', 'interface', 'trait', 'enum'].includes(definition.kind) ? definition.fqn : 'Global');
    const key = `${owner}|${definition.file}`;
    const group = groups.get(key) || { owner, file: definition.file, items: [] };
    group.items.push(definition);
    groups.set(key, group);
  }
  return [...groups.values()];
});

const toggle = (definition: ExtractedDefinition) => {
  const key = `${definition.kind}|${definition.fqn}|${definition.file}|${definition.line}`;
  const next = new Set(expanded.value);
  if (next.has(key)) next.delete(key); else next.add(key);
  expanded.value = next;
  if (selectedProject.value?.project_id && selectedProject.value.commit_sha && definition.symbol_id) {
    const url = new URL(window.location.href);
    url.searchParams.set('tab', 'definitions');
    url.searchParams.set('project_id', selectedProject.value.project_id);
    url.searchParams.set('commit_sha', selectedProject.value.commit_sha);
    if (next.has(key)) url.searchParams.set('symbol_id', definition.symbol_id);
    else if (url.searchParams.get('symbol_id') === definition.symbol_id) url.searchParams.delete('symbol_id');
    window.history.pushState(window.history.state, '', url);
  }
};

const isExpanded = (definition: ExtractedDefinition) =>
  expanded.value.has(`${definition.kind}|${definition.fqn}|${definition.file}|${definition.line}`);

const loadProjects = async () => {
  try {
    const response = await makeRequest('/api/inspect/projects');
    const data = await response.json() as IndexedSymbolProject[] & { detail?: string };
    if (!response.ok) throw new Error(data.detail || 'Failed to load indexed projects.');
    projects.value = Array.isArray(data) ? data : [];
    if (!selectedStem.value && projects.value.length) {
      const params = new URLSearchParams(window.location.search);
      const deepLinked = projects.value.find((project) => project.project_id === params.get('project_id')
        && project.commit_sha === params.get('commit_sha'));
      selectedStem.value = deepLinked?.collection || projects.value[0].collection;
    }
  } catch (cause: any) {
    error.value = cause?.message || 'Failed to load indexed projects.';
  }
};

const loadDefinitions = async () => {
  if (!selectedStem.value) { definitions.value = []; total.value = 0; return; }
  const project = selectedProject.value;
  if (project?.project_id && project.commit_sha) {
    const scopeKey = `${project.project_id}:${project.commit_sha}`;
    if (runtimeScopeKey.value !== scopeKey) {
      loading.value = true;
      error.value = '';
      try {
        const path = `/api/runtime/projects/${encodeURIComponent(project.project_id)}/snapshots/${encodeURIComponent(project.commit_sha)}`;
        const response = await makeRequest(path);
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || 'Failed to load runtime symbol data.');
        runtimeData.value = data as RuntimeSnapshotData;
        definitions.value = runtimeData.value.definitions || [];
        runtimeScopeKey.value = scopeKey;
        const linkedSymbol = new URLSearchParams(window.location.search).get('symbol_id');
        if (linkedSymbol) {
          const index = definitions.value.findIndex((item) => item.symbol_id === linkedSymbol);
          if (index >= 0) {
            offset.value = Math.floor(index / limit) * limit;
            const definition = definitions.value[index];
            expanded.value = new Set([`${definition.kind}|${definition.fqn}|${definition.file}|${definition.line}`]);
          }
        }
      } catch (cause: any) {
        error.value = cause?.message || 'Failed to load runtime symbol data.';
        definitions.value = [];
        runtimeData.value = null;
      } finally {
        loading.value = false;
      }
    }
    total.value = visibleDefinitions.value.length;
    return;
  }
  runtimeData.value = null;
  runtimeScopeKey.value = '';
  loading.value = true;
  error.value = '';
  try {
    const query = new URLSearchParams({ collection: selectedStem.value, offset: String(offset.value), limit: String(limit) });
    if (search.value.trim()) query.set('search', search.value.trim());
    if (kind.value) query.set('kind', kind.value);
    if (file.value.trim()) query.set('file', file.value.trim());
    const response = await makeRequest(`/api/inspect/definitions?${query.toString()}`);
    const data = await response.json() as ExtractedDefinitionsResponse & { detail?: string };
    if (!response.ok) throw new Error(data.detail || 'Failed to load extracted definitions.');
    definitions.value = data.items || [];
    total.value = data.total || 0;
  } catch (cause: any) {
    error.value = cause?.message || 'Failed to load extracted definitions.';
    definitions.value = [];
  } finally {
    loading.value = false;
  }
};

const refresh = async () => { runtimeScopeKey.value = ''; await loadProjects(); await loadDefinitions(); };
const resetPage = () => { offset.value = 0; if (selectedProject.value?.project_id) runtimeScopeKey.value = ''; loadDefinitions(); };
const previousPage = () => { offset.value = Math.max(0, offset.value - limit); loadDefinitions(); };
const nextPage = () => { if (offset.value + limit < total.value) { offset.value += limit; loadDefinitions(); } };
const kindLabel = (value: string) => kinds.find((item) => item.value === value)?.label || value.replace('_', ' ');
const formatRuns = (value?: number) => Number(value || 0).toLocaleString();
const formatLastSeen = (value?: string | null) => value ? new Date(value).toLocaleString() : 'No observed calls';

watch(selectedStem, () => { offset.value = 0; expanded.value = new Set(); runtimeScopeKey.value = ''; runtimeData.value = null; loadDefinitions(); });
watch([search, kind, file], resetPage);
watch(runtimeFilter, resetPage);
onMounted(refresh);
</script>

<template>
  <section class="flex-1 overflow-y-auto p-6 space-y-5">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h3 class="text-lg font-bold text-white flex items-center gap-2"><Code2 class="w-5 h-5 text-indigo-400" /> Extracted Definitions</h3>
        <p class="text-xs text-slate-500 mt-1">Browse definitions recovered from the indexed symbol graph.</p>
      </div>
      <button @click="refresh" class="px-3 py-2 rounded-xl border border-slate-700 text-xs text-slate-300 hover:bg-slate-800"><RefreshCw :class="loading ? 'animate-spin' : ''" class="w-3.5 h-3.5 inline mr-1" />Refresh</button>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-4 gap-2 rounded-2xl border border-slate-800 bg-slate-900/30 p-4">
      <select v-model="selectedStem" class="bg-[#11162d] border border-slate-800 rounded-xl px-3 py-2 text-xs text-slate-300 lg:col-span-1">
        <option value="">Select indexed project</option>
        <option v-for="project in projects" :key="project.collection" :value="project.collection">{{ project.stem }} · {{ project.definitions }} definitions</option>
      </select>
      <div class="relative"><Search class="absolute left-3 top-2.5 w-3.5 h-3.5 text-slate-500" /><input v-model="search" placeholder="Search name, FQN, owner…" class="w-full bg-[#11162d] border border-slate-800 rounded-xl pl-9 pr-3 py-2 text-xs text-slate-200" /></div>
      <select v-model="kind" class="bg-[#11162d] border border-slate-800 rounded-xl px-3 py-2 text-xs text-slate-300"><option v-for="item in kinds" :key="item.value" :value="item.value">{{ item.label }}</option></select>
      <input v-model="file" placeholder="Filter file path…" class="bg-[#11162d] border border-slate-800 rounded-xl px-3 py-2 text-xs text-slate-200" />
      <select v-if="runtimeData?.telemetry" v-model="runtimeFilter" class="bg-[#11162d] border border-slate-800 rounded-xl px-3 py-2 text-xs text-slate-300"><option value="all">All runtime activity</option><option value="unobserved">No observed calls</option><option value="hot">Hot · 100+ calls</option></select>
    </div>

    <p v-if="runtimeData" class="text-[10px] text-slate-500">Runtime counts cover {{ runtimeData.telemetry.retention_hours / 24 }} retained days. No observed calls do not prove dead code.</p>
    <p v-if="runtimeData?.symbol_index_available === false" class="text-[10px] text-amber-300">{{ runtimeData.symbol_index_reason || 'The symbol index is unavailable; runtime code joins may be incomplete.' }}</p>

    <p v-if="selectedStem" class="text-xs text-slate-500">{{ total }} definition(s) in <span class="text-slate-300 font-mono">{{ selectedStem }}</span></p>
    <div v-if="error" class="rounded-xl border border-rose-500/30 bg-rose-950/20 p-4 text-xs text-rose-300">{{ error }}</div>
    <div v-else-if="!selectedStem" class="rounded-2xl border border-dashed border-slate-700 p-10 text-center text-sm text-slate-500">Choose an indexed project to view its extracted definitions.</div>
    <div v-else-if="!loading && !pageDefinitions.length" class="rounded-2xl border border-dashed border-slate-700 p-10 text-center text-sm text-slate-500">No definitions match these filters, or this project has not been symbol-indexed.</div>
    <div v-else class="space-y-3">
      <div v-for="group in grouped" :key="`${group.owner}|${group.file}`" class="rounded-2xl border border-slate-800 bg-slate-900/30 overflow-hidden">
        <div class="px-4 py-3 border-b border-slate-800 flex items-center justify-between"><div><p class="text-sm font-semibold text-white">{{ group.owner }}</p><p class="text-[11px] text-slate-500 font-mono mt-1">{{ group.file }}</p></div><span class="text-[10px] text-slate-500">{{ group.items.length }} item(s)</span></div>
        <div v-for="definition in group.items" :key="`${definition.kind}|${definition.fqn}|${definition.line}`" class="border-b last:border-b-0 border-slate-800/70">
          <button @click="toggle(definition)" class="w-full px-4 py-3 text-left flex items-center gap-3 hover:bg-slate-800/30"><component :is="isExpanded(definition) ? ChevronDown : ChevronRight" class="w-4 h-4 text-slate-500 shrink-0" /><span class="px-2 py-1 rounded-md bg-indigo-500/10 text-indigo-300 text-[10px] font-semibold uppercase">{{ kindLabel(definition.kind) }}</span><span class="text-sm text-slate-200 truncate">{{ definition.name }}</span><span v-if="runtimeData && ['class','method','function'].includes(definition.kind)" class="hidden md:inline text-[10px] text-cyan-300">{{ formatRuns(definitionMetric(definition)?.runs) }} runs</span><span class="text-[10px] text-slate-500 font-mono">{{ runtimeData && ['class','method','function'].includes(definition.kind) ? `${Number(definitionMetric(definition)?.p95_ms || 0).toFixed(1)} ms · ${formatRuns(definitionMetric(definition)?.failures)} fails` : '' }}</span><span class="ml-auto text-[10px] text-slate-500 font-mono">L{{ definition.line || '?' }}{{ definition.end_line ? `–${definition.end_line}` : '' }}</span></button>
          <div v-if="isExpanded(definition)" class="px-11 pb-4 space-y-2 text-xs"><div class="text-slate-400 break-all"><span class="text-slate-500">FQN:</span> <span class="font-mono text-slate-300">{{ definition.fqn }}</span></div><div class="grid grid-cols-2 md:grid-cols-4 gap-2 text-slate-400"><span v-if="definition.visibility">Visibility: <b class="text-slate-200">{{ definition.visibility }}</b></span><span v-if="definition.static !== null && definition.static !== undefined">Static: <b class="text-slate-200">{{ definition.static ? 'yes' : 'no' }}</b></span><span v-if="definition.abstract !== null && definition.abstract !== undefined">Abstract: <b class="text-slate-200">{{ definition.abstract ? 'yes' : 'no' }}</b></span><span v-if="definition.owner">Owner: <b class="text-slate-200">{{ definition.owner }}</b></span></div><div v-if="runtimeData && definitionMetric(definition)" class="text-slate-400">{{ formatRuns(definitionMetric(definition)?.runs) }} observed calls · last ran {{ formatLastSeen(definitionMetric(definition)?.last_seen) }} · p95 {{ Number(definitionMetric(definition)?.p95_ms || 0).toFixed(1) }} ms · {{ formatRuns(definitionMetric(definition)?.failures) }} failures</div><div v-if="definition.symbol_id && runtimeData" class="grid gap-2 md:grid-cols-3"><div><b class="text-slate-500">Called by</b><p v-for="row in runtimeData.telemetry.callers.filter((item) => item.symbol_id === definition.symbol_id)" :key="row.caller_symbol_id" class="mt-1 font-mono text-slate-300">{{ row.caller_symbol_id }} · {{ formatRuns(row.calls) }}</p></div><div><b class="text-slate-500">Routes</b><p v-for="row in runtimeData.telemetry.symbol_routes.filter((item) => item.symbol_id === definition.symbol_id)" :key="row.route" class="mt-1 font-mono text-slate-300">{{ row.route }} · {{ formatRuns(row.calls) }}</p></div><div><b class="text-slate-500">Database tables</b><p v-for="row in runtimeData.telemetry.symbol_tables.filter((item) => item.symbol_id === definition.symbol_id)" :key="row.table_name" class="mt-1 font-mono text-slate-300">{{ row.table_name }} · {{ formatRuns(row.calls) }}</p></div></div><div class="text-slate-500 font-mono break-all">{{ definition.file }}:{{ definition.line || '?' }}</div></div>
        </div>
      </div>
    </div>
    <div v-if="selectedStem && total > limit" class="flex items-center justify-between text-xs text-slate-400"><button @click="previousPage" :disabled="offset === 0" class="px-3 py-2 rounded-lg border border-slate-700 disabled:opacity-40">Previous</button><span>{{ offset + 1 }}–{{ Math.min(offset + limit, total) }} of {{ total }}</span><button @click="nextPage" :disabled="offset + limit >= total" class="px-3 py-2 rounded-lg border border-slate-700 disabled:opacity-40">Next</button></div>
  </section>
</template>
