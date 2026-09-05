<script setup lang="ts">
import { ref, computed } from 'vue';
import { Zap, CheckCircle, AlertTriangle, RefreshCw } from 'lucide-vue-next';
import { RAGConfig } from '../../types';
import { makeRequest } from '../../ui/makeRequest';

const { config, updateField } = defineProps<{
  config: RAGConfig;
  updateField: <K extends keyof RAGConfig>(key: K, value: RAGConfig[K]) => void;
}>();

const runpodResult = ref<{ ok: boolean; latency_ms: number; detail: string } | null>(null);
const testingRunpod = ref(false);

const runpodConfigured = computed(() => Boolean((config.runpodApiKey ?? '').trim()));
const runpodLabel = computed(() => {
  const r = runpodResult.value;
  if (!r) return runpodConfigured.value ? 'Not tested' : 'No API key';
  return r.ok ? `Connected · ${r.latency_ms}ms` : r.detail || 'Failed';
});
const runpodConnCls = computed(() => {
  const r = runpodResult.value;
  if (!r) return runpodConfigured.value ? 'text-slate-500' : 'text-slate-600';
  return r.ok ? 'text-emerald-300' : 'text-rose-300';
});

const testRunpodConnection = async () => {
  testingRunpod.value = true;
  runpodResult.value = null;
  try {
    const res = await makeRequest('/api/health/test/runpod', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const data = await res.json();
    runpodResult.value = {
      ok: Boolean(data.ok),
      latency_ms: Number(data.latency_ms) || 0,
      detail: String(data.detail ?? data.error ?? 'request failed'),
    };
  } catch (err) {
    runpodResult.value = { ok: false, latency_ms: 0, detail: String(err) };
  } finally {
    testingRunpod.value = false;
  }
};
</script>

<template>
  <!-- ============ GPU Offload ============ -->
  <div class="space-y-3">
    <div class="flex items-center gap-2">
      <span class="w-1 h-3.5 bg-amber-500 rounded-full"></span>
      <h4 class="text-[11px] font-bold text-slate-400 uppercase tracking-widest">GPU Offload</h4>
    </div>

    <!-- RunPod Serverless (GPU LLM) -->
    <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4 max-w-2xl">
      <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2 border-b border-slate-800 pb-3">
        <Zap class="w-4 h-4 text-amber-400" />
        RunPod Serverless (GPU LLM)
      </h4>

      <div class="space-y-1.5">
        <label class="text-xs text-slate-400 font-mono">API Key</label>
        <input
          :value="config.runpodApiKey"
          @input="updateField('runpodApiKey', ($event.target as HTMLInputElement).value)"
          type="password"
          class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-amber-500 text-slate-200"
          placeholder="rpa_..."
        />
        <p class="text-[9px] text-slate-600">Serverless API key from runpod.io. Used to manage serverless endpoints.</p>
      </div>

      <div class="pt-3 border-t border-slate-800 flex items-center justify-between gap-2">
        <div class="min-w-0 flex-1 truncate text-[10px] font-mono" :class="runpodConnCls">
          <CheckCircle v-if="runpodResult?.ok" class="w-3 h-3 inline mr-1 shrink-0" />
          <AlertTriangle v-else-if="runpodResult" class="w-3 h-3 inline mr-1 shrink-0" />
          {{ runpodLabel }}
        </div>
        <button
          type="button"
          @click="testRunpodConnection"
          :disabled="testingRunpod"
          class="text-[10px] font-mono text-slate-400 hover:text-amber-300 flex items-center gap-1 disabled:opacity-50 shrink-0"
        >
          <RefreshCw class="w-3 h-3" :class="testingRunpod ? 'animate-spin' : ''" />
          {{ testingRunpod ? 'Testing…' : 'Test connection' }}
        </button>
      </div>
    </div>
  </div>
</template>
