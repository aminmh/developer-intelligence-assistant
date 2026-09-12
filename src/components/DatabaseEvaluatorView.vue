<script setup lang="ts">
import { ref } from 'vue';
import { makeRequest } from '../ui/makeRequest';
import type { EvaluatorRecommendation, EvaluatorReport, EvaluatorRunStatus } from '../types';

const props = defineProps<{ filename: string }>();
const slowLog = ref('');
const database = ref<'mysql' | 'postgresql'>('mysql');
const run = ref<EvaluatorRunStatus | null>(null);
const recommendation = ref<EvaluatorRecommendation | null>(null);
const busy = ref(false);
const error = ref('');

const start = async () => {
  if (!props.filename) { error.value = 'Select an uploaded project first.'; return; }
  busy.value = true; error.value = ''; recommendation.value = null;
  try {
    const response = await makeRequest('/api/evaluator/runs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: props.filename, database: database.value, slow_log: slowLog.value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || data.error || 'Evaluator start failed');
    run.value = data;
    for (let i = 0; i < 80 && run.value && !['completed', 'error'].includes(run.value.status); i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 100));
      const status = await makeRequest(`/api/evaluator/runs/${encodeURIComponent(run.value.run_id)}`);
      run.value = await status.json();
    }
  } catch (cause: any) { error.value = cause?.message || 'Evaluator failed'; }
  finally { busy.value = false; }
};

const requestRecommendation = async () => {
  if (!run.value) return;
  const response = await makeRequest(`/api/evaluator/runs/${encodeURIComponent(run.value.run_id)}/recommendation`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  const data = await response.json(); recommendation.value = data.recommendation;
};
</script>

<template>
  <section class="p-6 space-y-4 overflow-auto">
    <div class="rounded-2xl border border-slate-800 bg-slate-900/50 p-5">
      <h3 class="text-base font-semibold text-white">Database evaluator</h3>
      <p class="mt-1 text-xs text-slate-400">Native slow-log and EXPLAIN ingestion. Reports and patches remain review-only.</p>
      <div class="mt-4 grid gap-3 md:grid-cols-[180px_1fr]">
        <select v-model="database" class="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-200"><option value="mysql">MySQL</option><option value="postgresql">PostgreSQL</option></select>
        <textarea v-model="slowLog" rows="5" placeholder="Paste a bounded slow-log excerpt (optional)" class="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-xs text-slate-200" />
      </div>
      <button class="mt-3 rounded-lg bg-indigo-600 px-4 py-2 text-sm text-white disabled:opacity-50" :disabled="busy" @click="start">{{ busy ? 'Evaluating…' : 'Start evaluator run' }}</button>
      <p v-if="error" class="mt-3 text-xs text-rose-300">{{ error }}</p>
    </div>

    <div v-if="run" class="space-y-3">
      <div class="text-xs text-slate-400">Run {{ run.run_id }} · {{ run.status }} · {{ run.reports?.length || 0 }} report(s)</div>
      <article v-for="report in run.reports as EvaluatorReport[]" :key="report.query_fingerprint" class="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
        <div class="flex items-center justify-between gap-3"><code class="text-xs text-cyan-200">{{ report.query_fingerprint }}</code><span class="text-[10px] uppercase text-slate-500">{{ report.availability }}</span></div>
        <pre class="mt-2 whitespace-pre-wrap text-xs text-slate-300">{{ report.query }}</pre>
        <div class="mt-2 text-xs text-amber-200">{{ (report.issues || []).join(', ') || 'No evidence-backed issue detected' }}</div>
        <div v-if="report.source_trace?.file" class="mt-1 text-[11px] text-slate-400">{{ report.source_trace.file }}:{{ report.source_trace.line }}</div>
      </article>
      <button v-if="run.status === 'completed'" class="rounded-lg border border-indigo-500/40 px-3 py-2 text-xs text-indigo-200" @click="requestRecommendation">Request recommendation</button>
      <article v-if="recommendation" class="rounded-xl border border-emerald-500/20 bg-emerald-950/10 p-4 text-sm text-slate-200">
        <div class="font-semibold text-emerald-200">{{ recommendation.root_cause_analysis }}</div>
        <p class="mt-2">{{ recommendation.application_optimization }}</p>
        <p class="mt-2">{{ recommendation.database_recommendation }}</p>
        <pre v-if="recommendation.migration_or_ddl" class="mt-2 whitespace-pre-wrap text-xs text-slate-300">{{ recommendation.migration_or_ddl }}</pre>
        <p class="mt-3 text-xs text-amber-200">Review required: {{ recommendation.review_required ? 'yes' : 'no' }} · automatic apply is disabled.</p>
      </article>
    </div>
  </section>
</template>
