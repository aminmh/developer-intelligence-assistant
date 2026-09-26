<script setup lang="ts">
import { Activity, AlertTriangle, ArrowUpRight, BookOpen, CheckCircle2, RefreshCw, Server, XCircle } from 'lucide-vue-next';
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { makeRequest } from '../ui/makeRequest';
import type { RuntimeProjectRecord, RuntimeSnapshotData } from '../types';

const emit = defineEmits<{
  (event: 'openDefinition', projectId: string, commitSha: string, symbolId: string): void;
}>();

const projects = ref<RuntimeProjectRecord[]>([]);
const selectedKey = ref('');
const detail = ref<RuntimeSnapshotData | null>(null);
const loadingProjects = ref(false);
const loadingDetail = ref(false);
const error = ref('');
const deadOnly = ref(false);
const showGuide = ref(false);
const dockerLabelSnippet = [
  'labels:',
  '  com.devassistant.telemetry.enabled: "true"',
  '  com.devassistant.telemetry.project: billing',
  '  com.devassistant.telemetry.environment: staging',
  '  com.devassistant.telemetry.commit: "${GIT_COMMIT_SHA}"',
  '  com.devassistant.telemetry.service: api',
].join('\n');
let refreshTimer: ReturnType<typeof setInterval> | undefined;

const options = computed(() => projects.value.filter((project) => project.commit_sha)
  .map((project) => ({ ...project, key: `${project.project_id}:${project.commit_sha}` })));
const selected = computed(() => options.value.find((item) => item.key === selectedKey.value) || null);
const metricsBySymbol = computed(() => new Map((detail.value?.telemetry.symbols || []).map((item) => [item.symbol_id, item])));
const symbols = computed(() => {
  const metrics = metricsBySymbol.value;
  const eligible = (detail.value?.definitions || []).filter((item) => ['method', 'function'].includes(item.kind) && item.symbol_id);
  const rows = eligible.map((item) => ({ ...item, metric: metrics.get(item.symbol_id!) || null }));
  return deadOnly.value ? rows.filter((item) => !item.metric) : rows;
});
const deadCount = computed(() => (detail.value?.definitions || []).filter((item) =>
  ['method', 'function'].includes(item.kind) && item.symbol_id && !metricsBySymbol.value.has(item.symbol_id),
).length);
const hotCount = computed(() => (detail.value?.telemetry.symbols || []).filter((item) => Number(item.runs) >= 100).length);

const loadProjects = async () => {
  loadingProjects.value = true;
  try {
    const response = await makeRequest('/api/runtime/projects');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not load runtime project inventory.');
    projects.value = data.projects || [];
    if (!selectedKey.value || !options.value.some((item) => item.key === selectedKey.value)) {
      selectedKey.value = options.value[0]?.key || '';
    }
  } catch (cause: any) {
    error.value = cause?.message || 'Could not load runtime project inventory.';
  } finally {
    loadingProjects.value = false;
  }
};

const loadDetail = async () => {
  if (!selected.value?.commit_sha) { detail.value = null; return; }
  loadingDetail.value = true;
  error.value = '';
  try {
    const { project_id, commit_sha } = selected.value;
    const path = `/api/runtime/projects/${encodeURIComponent(project_id)}/snapshots/${encodeURIComponent(commit_sha)}`;
    const response = await makeRequest(path);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not load runtime snapshot.');
    detail.value = data as RuntimeSnapshotData;
  } catch (cause: any) {
    detail.value = null;
    error.value = cause?.message || 'Could not load runtime snapshot.';
  } finally {
    loadingDetail.value = false;
  }
};

const refresh = async () => { await loadProjects(); await loadDetail(); };
const formatCount = (value: number | string) => Number(value || 0).toLocaleString();
const formatTime = (value?: string | null) => value ? new Date(value).toLocaleString() : 'No observed calls';
const symbolMetric = (symbolId: string) => metricsBySymbol.value.get(symbolId);
const callersFor = (symbolId: string) => (detail.value?.telemetry.callers || []).filter((row) => row.symbol_id === symbolId);
const routesFor = (symbolId: string) => (detail.value?.telemetry.symbol_routes || []).filter((row) => row.symbol_id === symbolId);
const tablesFor = (symbolId: string) => (detail.value?.telemetry.symbol_tables || []).filter((row) => row.symbol_id === symbolId);
const openDefinition = (symbolId?: string | null) => {
  if (selected.value && symbolId) emit('openDefinition', selected.value.project_id, selected.value.commit_sha!, symbolId);
};

watch(selectedKey, loadDetail);
onMounted(async () => {
  await refresh();
  refreshTimer = setInterval(refresh, 30_000);
});
onUnmounted(() => { if (refreshTimer) clearInterval(refreshTimer); });
</script>

<template>
  <section class="flex-1 overflow-y-auto p-6 space-y-5">
    <header class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h3 class="flex items-center gap-2 text-lg font-bold text-white"><Activity class="h-5 w-5 text-cyan-400" />Runtime</h3>
        <p class="mt-1 text-xs text-slate-500">Live deployments, span flow, code matching, routes, and database tables.</p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <button type="button" @click="showGuide = !showGuide" class="rounded-xl border border-cyan-500/30 px-3 py-2 text-xs text-cyan-200 hover:bg-cyan-500/10"><BookOpen class="mr-1 inline h-3.5 w-3.5" />{{ showGuide ? 'Hide guide' : 'Get Started' }}</button>
        <select v-model="selectedKey" class="max-w-[24rem] rounded-xl border border-slate-800 bg-[#11162d] px-3 py-2 text-xs text-slate-200">
          <option value="">Select project commit</option>
          <option v-for="item in options" :key="item.key" :value="item.key">{{ item.project_id }} · {{ item.commit_sha?.slice(0, 12) }}</option>
        </select>
        <button type="button" @click="refresh" :disabled="loadingProjects || loadingDetail" class="rounded-xl border border-slate-700 px-3 py-2 text-xs text-slate-300 hover:bg-slate-800"><RefreshCw class="mr-1 inline h-3.5 w-3.5" :class="loadingProjects || loadingDetail ? 'animate-spin' : ''" />Refresh</button>
      </div>
    </header>

    <div v-if="error" class="rounded-xl border border-rose-500/25 bg-rose-950/20 p-4 text-xs text-rose-300">{{ error }}</div>
    <div v-else-if="!options.length" class="rounded-2xl border border-dashed border-slate-700 p-10 text-center">
      <Server class="mx-auto h-7 w-7 text-slate-600" />
      <p class="mt-3 text-sm text-slate-300">No project snapshots are registered yet.</p>
      <p class="mt-1 text-xs text-slate-500">Open Get Started to learn how to register a snapshot, report labeled Docker containers, and send application spans.</p>
    </div>

    <section v-if="showGuide" class="rounded-2xl border border-cyan-500/20 bg-cyan-500/[0.04] p-5">
      <div class="flex items-start gap-3">
        <BookOpen class="mt-0.5 h-5 w-5 shrink-0 text-cyan-300" />
        <div class="min-w-0 flex-1 space-y-4">
          <div>
            <h4 class="text-sm font-semibold text-white">Get started with Runtime</h4>
            <p class="mt-1 text-xs leading-5 text-slate-400">Runtime combines host-reported Docker deployments with sampled OpenTelemetry spans. Container reports show what is running; spans add code, route, and database activity. The app does not discover arbitrary containers or instrument application code automatically.</p>
          </div>
          <ol class="grid gap-4 lg:grid-cols-3">
            <li class="rounded-xl border border-slate-800 bg-slate-950/40 p-4">
              <p class="text-xs font-semibold text-cyan-200">1. Register the project version</p>
              <p class="mt-2 text-[11px] leading-5 text-slate-400">Upload and index a clean source snapshot using the project ID and full commit SHA used to build the Docker image. Runtime is selected by that exact project and commit.</p>
            </li>
            <li class="rounded-xl border border-slate-800 bg-slate-950/40 p-4">
              <p class="text-xs font-semibold text-cyan-200">2. Report running containers</p>
              <p class="mt-2 text-[11px] leading-5 text-slate-400">Deploy the per-host telemetry agent and authorize its certificate for the project/environment. Add these opt-in labels to each Compose service you want listed:</p>
              <pre class="mt-2 overflow-x-auto rounded-lg bg-black/30 p-2 text-[10px] leading-4 text-slate-300"><code>{{ dockerLabelSnippet }}</code></pre>
              <p class="mt-2 text-[10px] leading-4 text-slate-500">Project and commit must match the indexed snapshot. Host agent setup and mTLS scope are documented in <code>api/telemetry-agent/README.md</code>.</p>
            </li>
            <li class="rounded-xl border border-slate-800 bg-slate-950/40 p-4">
              <p class="text-xs font-semibold text-cyan-200">3. Send application spans (optional)</p>
              <p class="mt-2 text-[11px] leading-5 text-slate-400">Start the local Collector and instrument the app to export OTLP/HTTP to <code>http://localhost:24318/v1/traces</code> from the host or <code>http://host.docker.internal:24318/v1/traces</code> from a container.</p>
              <pre class="mt-2 overflow-x-auto rounded-lg bg-black/30 p-2 text-[10px] leading-4 text-slate-300"><code>cd api
docker compose --profile runtime up -d</code></pre>
              <p class="mt-2 text-[10px] leading-4 text-slate-500">Spans need project ID, full commit SHA, canonical symbol ID, <code>indexer.dirty=false</code>, and schema version <code>indexer-telemetry-v1</code>. See <code>api/docs/telemetry/attributes.md</code>; instrumentation is not bundled.</p>
            </li>
          </ol>
          <p class="border-t border-slate-800 pt-3 text-[10px] leading-4 text-slate-500">The host agent reconciles containers about every 60 seconds. This view refreshes every 30 seconds or when you select Refresh. Span counts are sampled observations within the configured retention window, not exact production call totals. Container visibility works without spans; code matching, routes, and table activity need spans.</p>
        </div>
      </div>
    </section>

    <template v-if="options.length && detail && !error">
      <div class="flex flex-wrap items-center gap-2 rounded-xl border border-slate-800 bg-slate-900/30 p-3 text-[11px]">
        <span class="font-mono text-slate-200">{{ detail.project_id }} @ {{ detail.commit_sha }}</span>
        <span class="ml-auto rounded-full border px-2 py-1" :class="detail.snapshot.state === 'ready' ? 'border-emerald-500/20 bg-emerald-500/5 text-emerald-300' : 'border-amber-500/20 bg-amber-500/5 text-amber-300'">{{ detail.snapshot.state }}</span>
        <span v-if="detail.deployment_mismatch" class="inline-flex items-center gap-1 rounded-full border border-amber-500/20 bg-amber-500/5 px-2 py-1 text-amber-300"><AlertTriangle class="h-3 w-3" />Running code uses another commit</span>
      </div>

      <div class="grid grid-cols-2 gap-3 xl:grid-cols-5">
        <article class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4"><p class="text-[10px] uppercase text-slate-500">Running containers</p><p class="mt-2 text-2xl font-semibold text-white">{{ detail.deployments.length }}</p><p class="mt-1 text-[10px] text-slate-500">{{ detail.matching_deployments.length }} match this commit</p></article>
        <article class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4"><p class="text-[10px] uppercase text-slate-500">Observed spans</p><p class="mt-2 text-2xl font-semibold text-white">{{ formatCount(detail.telemetry.total_spans) }}</p><p class="mt-1 text-[10px] text-slate-500">Last received {{ formatTime(detail.telemetry.last_seen) }}</p></article>
        <article class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4"><p class="text-[10px] uppercase text-slate-500">Matched to code</p><p class="mt-2 text-2xl font-semibold text-white">{{ detail.telemetry.match_percent }}%</p><p class="mt-1 text-[10px] text-slate-500">{{ formatCount(detail.telemetry.matched_spans) }} / {{ formatCount(detail.telemetry.total_spans) }} spans</p></article>
        <article class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4"><p class="text-[10px] uppercase text-slate-500">Possible dead code</p><p class="mt-2 text-2xl font-semibold text-amber-300">{{ formatCount(deadCount) }}</p><p class="mt-1 text-[10px] text-slate-500">No calls observed in {{ detail.telemetry.retention_hours / 24 }} retained days</p></article>
        <article class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4"><p class="text-[10px] uppercase text-slate-500">Hot symbols</p><p class="mt-2 text-2xl font-semibold text-orange-300">{{ formatCount(hotCount) }}</p><p class="mt-1 text-[10px] text-slate-500">100+ observed calls in window</p></article>
      </div>

      <div v-if="detail.telemetry.reason" class="rounded-xl border border-amber-500/20 bg-amber-500/5 p-4 text-xs text-amber-200">{{ detail.telemetry.reason }} Start the local runtime telemetry profile and instrument the PHP application with the Phase 1 identity attributes. The synthetic Phase 0 collector does not accept application spans.</div>
      <div v-if="detail.symbol_index_available === false" class="rounded-xl border border-amber-500/20 bg-amber-500/5 p-4 text-xs text-amber-200">{{ detail.symbol_index_reason || 'The symbol index is unavailable.' }} Runtime spans may still be present, but code-match and dead-code views require the indexed snapshot.</div>

      <section class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4 space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-2"><h4 class="text-xs font-semibold uppercase tracking-wide text-slate-300">Containers</h4><span class="text-[10px] text-slate-500">Deployment reports refresh every 60 seconds at the host agent</span></div>
        <div v-if="!detail.deployments.length" class="text-xs text-slate-500">No running containers have reported for this project.</div>
        <div v-for="container in detail.deployments" :key="container.container_id" class="flex flex-wrap items-center gap-2 rounded-xl border border-slate-800 p-3 text-[10px]">
          <CheckCircle2 class="h-3.5 w-3.5 text-emerald-400" /><span class="font-semibold text-slate-200">{{ container.service }}</span><span class="text-slate-500">{{ container.environment }}</span><span class="font-mono text-slate-500">{{ container.container_id.slice(0, 12) }}</span><span class="ml-auto max-w-full break-all font-mono text-slate-500">{{ container.commit_sha }}</span><XCircle v-if="container.commit_sha !== detail.commit_sha" class="h-3.5 w-3.5 text-amber-400" />
        </div>
      </section>

      <div class="grid gap-4 xl:grid-cols-2">
        <section class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4 space-y-3">
          <div class="flex items-center justify-between gap-2"><h4 class="text-xs font-semibold uppercase tracking-wide text-slate-300">Dead-code candidates</h4><label class="flex items-center gap-2 text-[10px] text-slate-400"><input v-model="deadOnly" type="checkbox" class="accent-amber-500" />Show only unobserved</label></div>
          <p class="text-[10px] text-slate-500">Absence here means no matching spans in retained telemetry; it does not prove code is unused.</p>
          <div class="max-h-80 space-y-1 overflow-auto">
            <button v-for="symbol in symbols.slice(0, 300)" :key="symbol.symbol_id" type="button" @click="openDefinition(symbol.symbol_id)" class="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left hover:bg-slate-800/50">
              <span class="min-w-0 flex-1 truncate font-mono text-[10px] text-slate-300">{{ symbol.fqn || symbol.name }}</span>
              <span v-if="symbol.metric" class="text-[10px] text-cyan-300">{{ formatCount(symbol.metric.runs) }} calls</span><span v-else class="text-[10px] text-amber-300">No observed calls</span><ArrowUpRight class="h-3 w-3 shrink-0 text-slate-600" />
            </button>
            <p v-if="!symbols.length" class="py-4 text-center text-xs text-slate-500">{{ deadOnly ? 'No symbols without observations.' : 'No indexed methods or functions in this snapshot.' }}</p>
          </div>
        </section>

        <section class="rounded-2xl border border-slate-800 bg-slate-900/30 p-4 space-y-3">
          <h4 class="text-xs font-semibold uppercase tracking-wide text-slate-300">Routes and database tables</h4>
          <div v-if="!detail.telemetry.routes.length && !detail.telemetry.tables.length" class="text-xs text-slate-500">No route or database-operation spans were observed.</div>
          <div v-if="detail.telemetry.routes.length" class="space-y-1"><p class="text-[10px] uppercase text-slate-500">Routes</p><div v-for="route in detail.telemetry.routes.slice(0, 30)" :key="`${route.method}|${route.route}`" class="flex gap-2 rounded-lg bg-slate-950/40 px-2 py-2 text-[10px]"><span class="text-indigo-300">{{ route.method }}</span><span class="min-w-0 flex-1 truncate font-mono text-slate-300">{{ route.route }}</span><span class="text-slate-500">{{ formatCount(route.requests) }}</span></div></div>
          <div v-if="detail.telemetry.tables.length" class="space-y-1"><p class="text-[10px] uppercase text-slate-500">Tables</p><div v-for="table in detail.telemetry.tables.slice(0, 30)" :key="`${table.table_name}|${table.operation}`" class="flex gap-2 rounded-lg bg-slate-950/40 px-2 py-2 text-[10px]"><span class="min-w-0 flex-1 truncate font-mono text-slate-300">{{ table.table_name }}</span><span class="text-cyan-300">{{ table.operation || 'operation' }}</span><span class="text-slate-500">{{ formatCount(table.operations) }}</span></div></div>
        </section>
      </div>
    </template>
  </section>
</template>
