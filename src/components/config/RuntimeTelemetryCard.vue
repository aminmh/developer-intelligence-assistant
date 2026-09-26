<script setup lang="ts">
import { Activity, RefreshCw } from 'lucide-vue-next';
import { onMounted, ref } from 'vue';
import { makeRequest } from '../../ui/makeRequest';
import type { RuntimeTelemetrySettings } from '../../types';

const settings = ref<RuntimeTelemetrySettings | null>(null);
const loading = ref(false);
const error = ref('');

const refresh = async () => {
  loading.value = true;
  error.value = '';
  try {
    const response = await makeRequest('/api/runtime/settings');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Runtime telemetry settings are unavailable.');
    settings.value = data as RuntimeTelemetrySettings;
  } catch (cause: any) {
    error.value = cause?.message || 'Runtime telemetry settings are unavailable.';
  } finally {
    loading.value = false;
  }
};

onMounted(refresh);
</script>

<template>
  <section class="rounded-2xl border border-slate-800 bg-slate-900/30 p-5 space-y-4">
    <div class="flex items-start justify-between gap-3">
      <div>
        <h3 class="flex items-center gap-2 text-sm font-semibold text-white"><Activity class="h-4 w-4 text-cyan-400" /> Runtime telemetry</h3>
        <p class="mt-1 text-[11px] text-slate-500">Read-only connection and recording status. Credentials stay in environment configuration.</p>
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
        <p class="mt-1 text-xs text-slate-200">{{ settings.retention_days }} days</p>
        <p class="mt-1 text-[10px] text-slate-500">Aggregates shown only for this retained span window.</p>
      </div>
      <div class="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
        <p class="text-[10px] uppercase tracking-wide text-slate-500">Configured recording ratio</p>
        <p class="mt-1 text-xs text-slate-200">{{ settings.recording_percent }}%</p>
        <p class="mt-1 text-[10px] text-slate-500">Collector rate from TELEMETRY_RECORDING_PERCENT. Align each app's SDK sampler with it.</p>
      </div>
      <div class="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
        <p class="text-[10px] uppercase tracking-wide text-slate-500">OTLP receiver</p>
        <p class="mt-1 break-all font-mono text-[10px] text-slate-200">{{ settings.otlp_endpoint || 'Not configured' }}</p>
        <p class="mt-1 text-[10px] text-slate-500">Identity scope: {{ settings.scope }}</p>
      </div>
    </div>
  </section>
</template>
