<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { AlertTriangle, CheckCircle2, Download, FileCode2, Star, Trash2, XCircle } from 'lucide-vue-next';
import { download, ms, pct, perfRequest, projectPath, runCsv, shortRevision, workloadLabel, type RouteSuggestion, type Run } from './performance';
import { breakdownLegend, breakdownSegments, type TimeBreakdown } from './timeBreakdown';

const props = defineProps<{ projectId: string; run: Run; production: RouteSuggestion[]; baseline: boolean; canOperate: boolean }>();
const emit = defineEmits<{ baseline: []; deleted: [] }>();
type Traces = { available: boolean; reason: string | null; requests: { trace_id: string; route: string; method: string; status_code: number | null; duration_ms: number; timestamp: string }[];
  routes: { route: string; traced: number; p95_ms: number; failures: number }[]; breakdown?: TimeBreakdown[] };
const traces = ref<Traces | null>(null);
const tracesError = ref('');
const productionP95 = (template?: string) => props.production.find((item) => item.route === template)?.p95_ms ?? null;
const budgetCell = (value: boolean | null | undefined) => value === true ? 'pass' : value === false ? 'over budget' : '';
const hasBudgets = computed(() => Object.keys(props.run.thresholds || {}).length > 0);

const loadTraces = async () => {
  traces.value = null; tracesError.value = '';
  try { traces.value = await perfRequest<Traces>(projectPath(props.projectId, `/runs/${encodeURIComponent(props.run.run_id)}/traces`)); }
  catch (cause: any) { tracesError.value = cause?.message || 'Traced requests are unavailable.'; }
};
const exportJson = () => download(`benchmark-${props.run.run_id}.json`, JSON.stringify(props.run, null, 2), 'application/json');
const exportCsv = () => download(`benchmark-${props.run.run_id}.csv`, runCsv(props.run), 'text/csv');
const remove = async () => {
  if (!window.confirm('Delete this saved benchmark run?')) return;
  await perfRequest(projectPath(props.projectId, `/runs/${encodeURIComponent(props.run.run_id)}`), { method: 'DELETE' });
  emit('deleted');
};
watch(() => props.run.run_id, () => void loadTraces(), { immediate: true });
</script>

<template>
  <section class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5 sm:p-6" aria-label="Benchmark results">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h3 class="text-lg font-semibold">{{ run.target_name }} <span class="text-sm font-normal text-slate-500">· revision {{ shortRevision(run) }}</span></h3>
        <p class="mt-1 text-xs text-slate-500">{{ run.target_origin }} · {{ workloadLabel(run.workload) }}{{ run.workload.warmup_seconds ? ` · ${run.workload.warmup_seconds}s warm-up` : '' }} · k6 {{ run.runner_version }} · {{ new Date(run.created_at).toLocaleString() }}</p>
      </div>
      <div class="flex flex-wrap items-center gap-1.5">
        <span class="inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs" :class="run.complete ? 'bg-emerald-500/10 text-emerald-200' : 'bg-amber-500/10 text-amber-200'">
          <CheckCircle2 v-if="run.complete" :size="12" /><AlertTriangle v-else :size="12" />{{ run.cancelled ? 'Cancelled (partial)' : run.complete ? 'Complete' : 'Partial' }}</span>
        <span v-if="hasBudgets" class="inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs" :class="run.thresholds_failed ? 'bg-rose-500/10 text-rose-200' : 'bg-emerald-500/10 text-emerald-200'">
          <XCircle v-if="run.thresholds_failed" :size="12" /><CheckCircle2 v-else :size="12" />{{ run.thresholds_failed ? 'Over budget' : 'Within budget' }}</span>
        <button type="button" class="inline-flex min-h-8 items-center gap-1 rounded-lg border px-2 text-xs" :class="baseline ? 'border-indigo-400/50 text-indigo-200' : 'border-slate-700 text-slate-300'" :aria-pressed="baseline" @click="emit('baseline')"><Star :size="12" />{{ baseline ? 'Baseline' : 'Set as baseline' }}</button>
        <button type="button" class="inline-flex min-h-8 items-center gap-1 rounded-lg border border-slate-700 px-2 text-xs text-slate-300" @click="exportCsv"><Download :size="12" />CSV</button>
        <button type="button" class="inline-flex min-h-8 items-center gap-1 rounded-lg border border-slate-700 px-2 text-xs text-slate-300" @click="exportJson"><Download :size="12" />JSON</button>
        <a :href="projectPath(projectId, `/runs/${encodeURIComponent(run.run_id)}/script`)" class="inline-flex min-h-8 items-center gap-1 rounded-lg border border-slate-700 px-2 text-xs text-slate-300"><FileCode2 :size="12" />k6 script</a>
        <button v-if="canOperate" type="button" class="inline-flex min-h-8 items-center gap-1 rounded-lg border border-rose-500/30 px-2 text-xs text-rose-200" aria-label="Delete run" @click="remove"><Trash2 :size="12" /></button>
      </div>
    </div>
    <ul v-if="run.partial_reasons?.length" class="mt-3 space-y-1 rounded-lg border border-amber-500/20 bg-amber-500/5 p-3 text-xs text-amber-100">
      <li v-for="reason in run.partial_reasons" :key="reason">{{ reason }}</li>
    </ul>
    <div class="mt-4 overflow-x-auto rounded-xl border border-slate-800">
      <table class="w-full min-w-[46rem] whitespace-nowrap text-left text-xs">
        <thead class="bg-slate-950/60 text-xs text-slate-400">
          <tr><th class="px-2.5 py-2">GET route</th><th class="px-2.5 py-2">Req/s</th><th class="px-2.5 py-2">Requests</th><th class="px-2.5 py-2">Status 2xx / 3xx / 4xx / 5xx</th><th class="px-2.5 py-2">Errors</th><th class="px-2.5 py-2">p50</th><th class="px-2.5 py-2">p90</th><th class="px-2.5 py-2">p95</th><th class="px-2.5 py-2">p99</th><th class="px-2.5 py-2" title="p95 of real traffic in Runtime">Prod p95</th><th v-if="hasBudgets" class="px-2.5 py-2">Budget</th></tr>
        </thead>
        <tbody class="divide-y divide-slate-800">
          <tr v-for="item in run.route_results" :key="item.path">
            <td class="max-w-56 whitespace-normal break-all px-2.5 py-2 font-mono text-xs">{{ item.path }}<span v-if="item.template && item.template !== item.path" class="block text-[10px] text-slate-500">{{ item.template }}</span></td>
            <td class="px-2.5 py-2">{{ Number(item.throughput_rps || 0).toFixed(1) }}</td>
            <td class="px-2.5 py-2">{{ (item.count || 0).toLocaleString() }}</td>
            <td class="px-2.5 py-2 font-mono text-xs"><template v-if="item.statuses"><span class="text-emerald-300">{{ item.statuses['2xx'] }}</span> / <span :class="item.statuses['3xx'] ? 'text-amber-300' : ''">{{ item.statuses['3xx'] }}</span> / <span :class="item.statuses['4xx'] ? 'text-rose-300' : ''">{{ item.statuses['4xx'] }}</span> / <span :class="item.statuses['5xx'] ? 'text-rose-300' : ''">{{ item.statuses['5xx'] }}</span><span v-if="item.network_errors" class="text-rose-300"> · {{ item.network_errors }} net</span></template><span v-else class="text-slate-500">—</span></td>
            <td class="px-2.5 py-2" :class="item.error_rate ? 'text-rose-300' : 'text-emerald-300'">{{ pct(item.error_rate) }}</td>
            <td class="px-2.5 py-2">{{ ms(item.latency_ms?.med) }}</td>
            <td class="px-2.5 py-2">{{ ms(item.latency_ms?.['p(90)']) }}</td>
            <td class="px-2.5 py-2 font-medium">{{ ms(item.latency_ms?.['p(95)']) }}</td>
            <td class="px-2.5 py-2">{{ ms(item.latency_ms?.['p(99)']) }}</td>
            <td class="px-2.5 py-2 text-slate-400">{{ ms(productionP95(item.template)) }}</td>
            <td v-if="hasBudgets" class="px-2.5 py-2 text-xs" :class="item.thresholds?.latency === false || item.thresholds?.errors === false ? 'text-rose-300' : 'text-emerald-300'">{{ budgetCell(item.thresholds?.latency === false || item.thresholds?.errors === false ? false : item.thresholds?.latency ?? item.thresholds?.errors) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="mt-2 text-xs text-slate-500">Redirects (3xx) are not counted as successes. Latency is the time to the full response, excluding connection setup.</p>

    <section class="mt-5 border-t border-slate-800 pt-4" aria-label="Traced requests during this run">
      <h4 class="text-sm font-semibold text-slate-200">What the application did during this run</h4>
      <p v-if="tracesError" class="mt-2 text-xs text-rose-200">{{ tracesError }}</p>
      <p v-else-if="!traces" class="mt-2 text-xs text-slate-500">Looking up traced requests…</p>
      <p v-else-if="!traces.available" class="mt-2 text-xs text-slate-500">{{ traces.reason }}</p>
      <template v-else>
        <div v-if="traces.breakdown?.length" class="mt-2" aria-label="Where response time went">
          <p class="text-xs text-slate-400">Where the average response time went, per route. Measured from sampled traces of this run, so treat it as an estimate. Network &amp; queueing is k6's average latency minus the time the server spent on the request (proxy, waiting for a worker, transfer).</p>
          <ul class="mt-2 flex flex-wrap gap-3 text-[11px] text-slate-400">
            <li v-for="item in breakdownLegend" :key="item.key" class="inline-flex items-center gap-1"><span class="h-2 w-2 rounded-sm" :class="item.color" />{{ item.label }}</li>
          </ul>
          <div class="mt-2 overflow-x-auto rounded-lg border border-slate-800">
            <table class="w-full min-w-[44rem] whitespace-nowrap text-left text-xs">
              <thead class="bg-slate-950/60 text-slate-400"><tr><th class="px-3 py-2">Route</th><th class="px-3 py-2">Traced</th><th class="px-3 py-2 w-56">Split</th><th class="px-3 py-2">Database</th><th class="px-3 py-2">Cache</th><th class="px-3 py-2">Outbound</th><th class="px-3 py-2" title="CPU time in brackets">App code</th><th class="px-3 py-2">Network &amp; queue</th><th class="px-3 py-2" title="k6 average latency">Total</th></tr></thead>
              <tbody class="divide-y divide-slate-800">
                <tr v-for="row in traces.breakdown" :key="row.route">
                  <td class="px-3 py-2 font-mono">{{ row.route }}</td>
                  <td class="px-3 py-2">{{ row.traced }}</td>
                  <td class="px-3 py-2"><div class="flex h-2.5 w-52 overflow-hidden rounded-full bg-slate-800" role="img" :aria-label="breakdownSegments(row).map((part) => `${part.label} ${Math.round(part.share * 100)}%`).join(', ')">
                    <span v-for="part in breakdownSegments(row)" :key="part.key" :class="part.color" :style="{ width: `${part.share * 100}%` }" :title="`${part.label}: ${ms(part.ms)} (${Math.round(part.share * 100)}%)`" /></div></td>
                  <td class="px-3 py-2">{{ ms(row.database_ms) }}</td>
                  <td class="px-3 py-2">{{ ms(row.cache_ms) }}</td>
                  <td class="px-3 py-2">{{ ms(row.external_ms) }}</td>
                  <td class="px-3 py-2">{{ ms(row.app_ms) }}<span v-if="row.app_cpu_ms != null" class="text-slate-500"> ({{ ms(row.app_cpu_ms) }} CPU)</span></td>
                  <td class="px-3 py-2">{{ ms(row.network_queue_ms) }}</td>
                  <td class="px-3 py-2 font-medium">{{ ms(row.client_ms ?? row.server_ms) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
        <p class="mt-3 text-xs text-slate-500">Sampled requests that carried this run's id. Find them in <a href="/?tab=runtime" class="text-indigo-200 hover:underline">Runtime</a> (Slow filter) to see their database queries and method activity.</p>
        <div class="mt-2 overflow-x-auto rounded-lg border border-slate-800">
          <table class="w-full min-w-[36rem] text-left text-xs">
            <thead class="bg-slate-950/60 text-slate-400"><tr><th class="px-3 py-2">Slowest traced request</th><th class="px-3 py-2">Status</th><th class="px-3 py-2">Duration</th><th class="px-3 py-2">Trace</th></tr></thead>
            <tbody class="divide-y divide-slate-800">
              <tr v-for="item in traces.requests" :key="item.trace_id"><td class="px-3 py-2 font-mono">{{ item.method }} {{ item.route }}</td><td class="px-3 py-2">{{ item.status_code ?? '—' }}</td><td class="px-3 py-2">{{ ms(item.duration_ms) }}</td>
                <td class="px-3 py-2"><code class="text-slate-300" :title="item.trace_id">{{ item.trace_id.slice(0, 16) }}</code></td></tr>
            </tbody>
          </table>
        </div>
      </template>
    </section>
  </section>
</template>
