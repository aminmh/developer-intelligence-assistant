<script setup lang="ts">
import { ref, watch } from 'vue';
import { ms, pct, perfRequest, projectPath, shortRevision, type Comparison, type Run } from './performance';

const props = defineProps<{ projectId: string; history: Run[]; baselineId: string }>();
const left = ref('');
const right = ref('');
const comparison = ref<Comparison | null>(null);
const error = ref('');
const label = (run: Run) => `${new Date(run.created_at).toLocaleString()} · ${run.target_name} · rev ${shortRevision(run)}${run.run_id === props.baselineId ? ' · baseline' : ''}`;
const change = (value?: number | null, lowerIsBetter = true) => {
  if (value == null) return { text: '—', cls: 'text-slate-500' };
  const better = lowerIsBetter ? value < 0 : value > 0;
  return { text: `${value > 0 ? '+' : ''}${value.toFixed(1)}%`, cls: Math.abs(value) < 5 ? 'text-slate-400' : better ? 'text-emerald-300' : 'text-rose-300' };
};
const compare = async () => {
  comparison.value = null; error.value = '';
  if (!left.value || !right.value || left.value === right.value) return;
  try {
    const data = await perfRequest<{ comparison: Comparison }>(projectPath(props.projectId, '/compare'), { method: 'POST', json: { left_run_id: left.value, right_run_id: right.value } });
    comparison.value = data.comparison;
  } catch (cause: any) { error.value = cause?.message || 'The runs could not be compared.'; }
};
// Default: baseline (or the previous run) against the latest run.
watch(() => [props.history.map((run) => run.run_id).join(), props.baselineId], () => {
  const ids = props.history.map((run) => run.run_id);
  right.value = ids[0] || '';
  left.value = props.baselineId && props.baselineId !== right.value && ids.includes(props.baselineId) ? props.baselineId : ids[1] || '';
  void compare();
}, { immediate: true });
watch([left, right], () => void compare());
</script>

<template>
  <section class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5 sm:p-6" aria-label="Compare benchmark runs">
    <h3 class="text-lg font-semibold">Compare runs</h3>
    <p class="mt-1 text-xs text-slate-500">Typical use: the baseline on the previous revision against the latest run on the new one.</p>
    <div class="mt-3 grid gap-3 sm:grid-cols-2">
      <label class="text-xs text-slate-400">Before<select v-model="left" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm"><option value="">Select a run</option><option v-for="run in history" :key="run.run_id" :value="run.run_id">{{ label(run) }}</option></select></label>
      <label class="text-xs text-slate-400">After<select v-model="right" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm"><option value="">Select a run</option><option v-for="run in history" :key="run.run_id" :value="run.run_id">{{ label(run) }}</option></select></label>
    </div>
    <p v-if="error" role="alert" class="mt-3 text-sm text-rose-200">{{ error }}</p>
    <template v-if="comparison">
      <p class="mt-3 text-sm" :class="comparison.equivalent ? 'text-emerald-200' : 'text-amber-200'">
        {{ comparison.equivalent ? 'Same target, routes, authentication, workload and runner.' : `Different ${comparison.differences.join(', ').replace(/_/g, ' ')}: compare with care.` }}
        <span class="text-slate-400">{{ comparison.revisions.changed ? ' Code revision changed.' : ' Same code revision.' }}</span>
        <strong v-if="comparison.regressions" class="ml-1 text-rose-300">{{ comparison.regressions }} route{{ comparison.regressions === 1 ? '' : 's' }} regressed.</strong>
      </p>
      <div class="mt-3 overflow-x-auto rounded-lg border border-slate-800">
        <table class="w-full min-w-[44rem] text-left text-xs">
          <thead class="bg-slate-950/60 text-slate-400"><tr><th class="px-3 py-2">Route</th><th class="px-3 py-2">p50</th><th class="px-3 py-2">p95</th><th class="px-3 py-2">p99</th><th class="px-3 py-2">Req/s</th><th class="px-3 py-2">Errors</th><th class="px-3 py-2"></th></tr></thead>
          <tbody class="divide-y divide-slate-800">
            <tr v-for="row in comparison.routes" :key="row.path" :class="row.regression ? 'bg-rose-500/5' : ''">
              <td class="max-w-56 break-all px-3 py-2 font-mono">{{ row.path }}</td>
              <td v-for="key in (['p50', 'p95', 'p99'] as const)" :key="key" class="px-3 py-2">{{ ms(row[key].before) }} → {{ ms(row[key].after) }} <span :class="change(row[key].change_pct).cls">{{ change(row[key].change_pct).text }}</span></td>
              <td class="px-3 py-2">{{ row.throughput_rps.before?.toFixed(1) ?? '—' }} → {{ row.throughput_rps.after?.toFixed(1) ?? '—' }} <span :class="change(row.throughput_rps.change_pct, false).cls">{{ change(row.throughput_rps.change_pct, false).text }}</span></td>
              <td class="px-3 py-2">{{ pct(row.error_rate.before) }} → {{ pct(row.error_rate.after) }}</td>
              <td class="px-3 py-2"><span v-if="row.regression" class="rounded-full bg-rose-500/15 px-2 py-0.5 text-rose-200">regression</span><span v-else-if="row.improvement" class="rounded-full bg-emerald-500/15 px-2 py-0.5 text-emerald-200">faster</span></td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="mt-2 text-[11px] text-slate-500">A regression means p95 rose by more than 10% or the error rate went up, with equivalent inputs.</p>
    </template>
    <p v-else-if="history.length < 2" class="mt-3 text-sm text-slate-500">Run at least two benchmarks to compare them.</p>
  </section>
</template>
