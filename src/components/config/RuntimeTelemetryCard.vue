<script setup lang="ts">
import { Activity, RefreshCw, Save } from 'lucide-vue-next';
import { onMounted, ref } from 'vue';
import { makeRequest } from '../../ui/makeRequest';
import type { RuntimeTelemetrySettings } from '../../types';

const settings = ref<RuntimeTelemetrySettings | null>(null);
const loading = ref(false);
const saving = ref(false);
const error = ref('');
const savedMessage = ref('');
const p95WindowHours = ref(24);
const refreshMinutes = ref(5);

const refresh = async () => {
  loading.value = true;
  error.value = '';
  try {
    const response = await makeRequest('/api/runtime/settings');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Runtime telemetry settings are unavailable.');
    settings.value = data as RuntimeTelemetrySettings;
    p95WindowHours.value = settings.value.p95_window_hours;
    refreshMinutes.value = settings.value.refresh_minutes;
  } catch (cause: any) {
    error.value = cause?.message || 'Runtime telemetry settings are unavailable.';
  } finally {
    loading.value = false;
  }
};

const savePolicy = async () => {
  saving.value = true;
  error.value = '';
  savedMessage.value = '';
  try {
    const response = await makeRequest('/api/runtime/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ p95_window_hours: p95WindowHours.value, refresh_minutes: refreshMinutes.value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Runtime sampling settings could not be saved.');
    settings.value = data as RuntimeTelemetrySettings;
    p95WindowHours.value = settings.value.p95_window_hours;
    refreshMinutes.value = settings.value.refresh_minutes;
    savedMessage.value = data.policy_refresh?.pending ? 'Saved; the Collector policy will refresh on its next cycle.' : 'Saved and applied to the Collector.';
  } catch (cause: any) {
    error.value = cause?.message || 'Runtime sampling settings could not be saved.';
  } finally {
    saving.value = false;
  }
};

onMounted(refresh);
</script>

<template>
  <section class="rounded-2xl border border-slate-800 bg-slate-900/30 p-5 space-y-4">
    <div class="flex items-start justify-between gap-3">
      <div>
        <h3 class="flex items-center gap-2 text-sm font-semibold text-white"><Activity class="h-4 w-4 text-cyan-400" /> Runtime telemetry</h3>
        <p class="mt-1 text-[11px] text-slate-500">Tune how the Collector keeps slow traces. Credentials stay in environment configuration.</p>
      </div>
      <button type="button" @click="refresh" :disabled="loading" class="rounded-lg border border-slate-700 px-2 py-1 text-[10px] text-slate-300 hover:bg-slate-800"><RefreshCw class="mr-1 inline h-3 w-3" :class="loading ? 'animate-spin' : ''" />Refresh</button>
    </div>
    <div v-if="error" class="rounded-xl border border-rose-500/20 bg-rose-500/5 p-3 text-xs text-rose-300">{{ error }}</div>
    <div v-else-if="settings" class="grid gap-3 sm:grid-cols-2">
      <div class="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
        <p class="text-[10px] uppercase tracking-wide text-slate-500">Span store</p>
        <p class="mt-1 text-xs text-slate-200">{{ settings.store }} · {{ settings.enabled ? 'configured' : 'not configured' }}</p>
        <p class="mt-1 break-all font-mono text-[10px] text-slate-500">{{ settings.connection || 'No ClickHouse endpoint' }}</p>
      </div>
      <div class="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
        <p class="text-[10px] uppercase tracking-wide text-slate-500">Retention</p>
        <p class="mt-1 text-xs text-slate-200">Sampled traces: {{ settings.retention_days }} days</p>
        <p class="mt-1 text-[10px] text-slate-500">Pre-tail aggregate input: {{ settings.aggregation_retention_days }} days.</p>
      </div>
      <div class="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
        <p class="text-[10px] uppercase tracking-wide text-slate-500">Configured recording ratio</p>
        <p class="mt-1 text-xs text-slate-200">{{ settings.collector_baseline_percent }}%</p>
        <p class="mt-1 text-[10px] text-slate-500">Baseline sampling after spans reach the Collector. PHP SDK sampling happens first and may reduce this input.</p>
      </div>
      <div class="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
        <p class="text-[10px] uppercase tracking-wide text-slate-500">OTLP receiver</p>
        <p class="mt-1 break-all font-mono text-[10px] text-slate-200">{{ settings.otlp_endpoint || 'Not configured' }}</p>
        <p class="mt-1 text-[10px] text-slate-500">Identity scope: {{ settings.scope }}</p>
      </div>
      <div class="rounded-xl border border-slate-800 bg-slate-950/40 p-3 sm:col-span-2">
        <p class="text-[10px] uppercase tracking-wide text-slate-500">Slow trace policy</p>
        <p class="mt-1 text-[10px] text-slate-400">Among received spans, errors and requests slower than the rolling p95 are retained, along with the baseline sample. Upstream SDK sampling may omit spans before they reach this policy.</p>
        <div class="mt-3 flex flex-wrap items-end gap-3">
          <label class="grid gap-1 text-[10px] text-slate-400">P95 window (hours)
            <input v-model.number="p95WindowHours" type="number" min="1" :max="settings.aggregation_retention_hours" class="w-28 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1.5 text-xs text-slate-100" />
          </label>
          <label class="grid gap-1 text-[10px] text-slate-400">Policy refresh (minutes)
            <input v-model.number="refreshMinutes" type="number" min="1" max="60" class="w-28 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1.5 text-xs text-slate-100" />
          </label>
          <button type="button" @click="savePolicy" :disabled="saving || loading" class="rounded-lg border border-cyan-500/30 px-3 py-1.5 text-[10px] text-cyan-200 hover:bg-cyan-500/10 disabled:opacity-50"><Save class="mr-1 inline h-3 w-3" />{{ saving ? 'Saving…' : 'Save policy' }}</button>
          <span v-if="savedMessage" class="text-[10px] text-emerald-300">{{ savedMessage }}</span>
        </div>
        <p class="mt-2 text-[10px] text-slate-500">Window: 1 to {{ settings.aggregation_retention_hours }} hours. Refresh: 1 to 60 minutes.</p>
      </div>
    </div>
  </section>
</template>
