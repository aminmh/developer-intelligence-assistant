<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { Server, Cpu, RefreshCw, Gauge, Zap, SlidersHorizontal } from 'lucide-vue-next';
import { RAGConfig } from '../../types';
import { makeRequest } from '../../ui/makeRequest';

const { config, updateField } = defineProps<{
  config: RAGConfig;
  updateField: <K extends keyof RAGConfig>(key: K, value: RAGConfig[K]) => void;
}>();

const ollamaModels = ref<string[]>([]);
const loadingModels = ref(false);

const fetchModels = async () => {
  loadingModels.value = true;
  try {
    const res = await makeRequest('/api/ollama/models');
    const data = await res.json();
    if (data.success) ollamaModels.value = data.models || [];
  } catch {
    // leave the list empty; the current value is still shown as a fallback option
  } finally {
    loadingModels.value = false;
  }
};

const llmModelOptions = computed(() => {
  const list = [...ollamaModels.value];
  if (config.ollamaLlmModel && !list.includes(config.ollamaLlmModel)) list.unshift(config.ollamaLlmModel);
  return list;
});
const embedModelOptions = computed(() => {
  const list = [...ollamaModels.value];
  if (config.ollamaEmbedModel && !list.includes(config.ollamaEmbedModel)) list.unshift(config.ollamaEmbedModel);
  return list;
});
const evalModelOptions = computed(() => {
  const list = [...ollamaModels.value];
  if (config.ollamaEvalModel && !list.includes(config.ollamaEvalModel)) list.unshift(config.ollamaEvalModel);
  return list;
});

// --- LLM provider status ----------------------------------------------------

const llmHealth = ref<any>(null);
const loadingHealth = ref(false);

const fetchHealth = async () => {
  loadingHealth.value = true;
  try {
    const res = await makeRequest('/api/health/llm');
    const data = await res.json();
    llmHealth.value = res.ok ? data : null;
  } catch {
    llmHealth.value = null;
  } finally {
    loadingHealth.value = false;
  }
};

// Backend may return health nested ({ ollama, remote }) or flat — normalize both.
const ollamaHealth = computed(() => {
  const h = llmHealth.value;
  if (!h) return null;
  return h.ollama ?? h.local ?? h;
});
const ollamaOnline = computed(() => {
  const h = ollamaHealth.value;
  if (!h) return false;
  return Boolean(h.online ?? h.reachable ?? h.connected ?? h.ollama_online);
});
const ollamaModelCount = computed(() => {
  const h = ollamaHealth.value;
  if (!h) return 0;
  if (Array.isArray(h.models)) return h.models.length;
  const n = Number(h.models ?? h.modelCount ?? h.count ?? h.ollama_models ?? 0);
  return Number.isFinite(n) ? n : 0;
});
const remoteHealth = computed(() => {
  const h = llmHealth.value;
  if (!h) return null;
  return h.remote ?? h.gpu ?? null;
});
const remoteConfigured = computed(() => {
  const h = remoteHealth.value;
  if (!h) return false;
  return Boolean(
    h.configured ?? h.online != null ?? h.reachable != null ?? Boolean(h.model ?? h.serving_model ?? h.baseUrl ?? h.llmRemoteBaseUrl),
  );
});
const remoteOnline = computed(() => {
  const h = remoteHealth.value;
  if (!h) return false;
  return Boolean(h.online ?? h.reachable ?? h.remote_online);
});
const remoteServingModel = computed(() => {
  const h = remoteHealth.value;
  if (!h) return '';
  return String(h.model ?? h.serving_model ?? h.remote_model ?? '');
});
const runpodHealth = computed(() => {
  const h = llmHealth.value;
  if (!h) return null;
  return h.runpod ?? null;
});
const runpodConfigured = computed(() => Boolean((config.runpodApiKey ?? '').trim()));
const runpodReachable = computed(() => Boolean(runpodHealth.value?.reachable));
const runpodEndpointCount = computed(() => {
  const n = Number(runpodHealth.value?.endpoint_count ?? 0);
  return Number.isFinite(n) ? n : 0;
});

onMounted(() => {
  fetchModels();
  fetchHealth();
  window.addEventListener('ollama-models-changed', fetchModels);
});
onUnmounted(() => {
  window.removeEventListener('ollama-models-changed', fetchModels);
});
</script>

<template>
  <!-- ============ LLM Providers ============ -->
  <div class="space-y-3">
    <div class="flex items-center gap-2">
      <span class="w-1 h-3.5 bg-indigo-500 rounded-full"></span>
      <h4 class="text-[11px] font-bold text-slate-400 uppercase tracking-widest">LLM Providers</h4>
    </div>
    <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
      <div class="space-y-6">

        <!-- Ollama connection -->
        <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4">
          <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2 border-b border-slate-800 pb-3">
            <Server class="w-4 h-4 text-indigo-400" />
            Ollama Server
          </h4>

          <div class="space-y-1.5">
            <label class="text-xs text-slate-400 font-mono">Base URL</label>
            <input
              :value="config.ollamaBaseUrl"
              @input="updateField('ollamaBaseUrl', ($event.target as HTMLInputElement).value)"
              type="text"
              class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-emerald-500 text-slate-200"
              placeholder="e.g. http://host.docker.internal:11434"
            />
          </div>
        </div>

        <!-- Model selection -->
        <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4">
          <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center justify-between gap-2 border-b border-slate-800 pb-3">
            <span class="flex items-center gap-2">
              <Cpu class="w-4 h-4 text-indigo-400" />
              Models
            </span>
            <button
              type="button"
              @click="fetchModels"
              class="text-[10px] text-slate-400 hover:text-indigo-300 flex items-center gap-1 font-mono"
            >
              <RefreshCw class="w-3 h-3" :class="loadingModels ? 'animate-spin' : ''" />
              refresh
            </button>
          </h4>

          <div class="space-y-3">
            <div class="flex items-center justify-between gap-4">
              <label class="text-xs text-slate-400 font-mono shrink-0">LLM (Chat) Model</label>
              <select
                :value="config.ollamaLlmModel"
                @change="updateField('ollamaLlmModel', ($event.target as HTMLSelectElement).value)"
                class="w-[15rem] max-w-full bg-[#11162d] border border-slate-800 rounded-xl px-3 py-2.5 text-xs font-mono focus:outline-none focus:border-emerald-500 text-slate-200"
              >
                <option v-for="m in llmModelOptions" :key="m" :value="m">{{ m }}</option>
              </select>
            </div>

            <div class="flex items-center justify-between gap-4">
              <label class="text-xs text-slate-400 font-mono shrink-0">Embedding Model</label>
              <select
                :value="config.ollamaEmbedModel"
                @change="updateField('ollamaEmbedModel', ($event.target as HTMLSelectElement).value)"
                class="w-[15rem] max-w-full bg-[#11162d] border border-slate-800 rounded-xl px-3 py-2.5 text-xs font-mono focus:outline-none focus:border-emerald-500 text-slate-200"
              >
                <option v-for="m in embedModelOptions" :key="m" :value="m">{{ m }}</option>
              </select>
            </div>

            <div class="flex items-center justify-between gap-4">
              <div class="flex items-center gap-2 shrink-0">
                <label class="text-xs text-slate-400 font-mono">Evaluation Judge Model</label>
                <span class="text-[9px] text-slate-500 bg-slate-800/60 px-1.5 py-0.5 rounded font-mono">RAGAS</span>
              </div>
              <select
                :value="config.ollamaEvalModel"
                @change="updateField('ollamaEvalModel', ($event.target as HTMLSelectElement).value)"
                class="w-[15rem] max-w-full bg-[#11162d] border border-slate-800 rounded-xl px-3 py-2.5 text-xs font-mono focus:outline-none focus:border-emerald-500 text-slate-200"
              >
                <option v-for="m in evalModelOptions" :key="m" :value="m">{{ m }}</option>
              </select>
            </div>
            <p class="text-[9px] text-slate-600">Judge model only scores answers; it never generates chat responses.</p>
          </div>
        </div>
      </div>

      <div class="space-y-6">
        <!-- LLM Provider Status -->
        <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4">
          <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center justify-between gap-2 border-b border-slate-800 pb-3">
            <span class="flex items-center gap-2">
              <Gauge class="w-4 h-4 text-indigo-400" />
              LLM Provider Status
            </span>
            <button
              type="button"
              @click="fetchHealth"
              class="text-[10px] text-slate-400 hover:text-indigo-300 flex items-center gap-1 font-mono"
              :disabled="loadingHealth"
            >
              <RefreshCw class="w-3 h-3" :class="loadingHealth ? 'animate-spin' : ''" />
              refresh
            </button>
          </h4>

          <div v-if="loadingHealth" class="flex items-center gap-2 text-[10px] text-slate-400 font-mono">
            <RefreshCw class="w-3.5 h-3.5 animate-spin" />
            checking…
          </div>
          <div v-else-if="!llmHealth" class="text-[10px] text-slate-500 font-mono">
            Health endpoint unavailable.
          </div>
          <div v-else class="space-y-3">
            <div class="flex items-center justify-between gap-3">
              <span class="text-xs text-slate-400 font-mono flex items-center gap-1.5">
                <Server class="w-3.5 h-3.5 text-indigo-400" />
                Local Ollama
              </span>
              <span
                class="px-2 py-0.5 rounded-full border text-[10px] font-mono"
                :class="ollamaOnline
                  ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                  : 'bg-rose-500/10 text-rose-400 border-rose-500/30'"
              >
                {{ ollamaOnline ? `connected (${ollamaModelCount} models)` : 'offline' }}
              </span>
            </div>
            <div class="flex items-center justify-between gap-3">
              <span class="text-xs text-slate-400 font-mono flex items-center gap-1.5">
                <Zap class="w-3.5 h-3.5 text-indigo-400" />
                External OpenAI-Compatible
              </span>
              <span
                v-if="!remoteConfigured"
                class="px-2 py-0.5 rounded-full border text-[10px] font-mono bg-slate-800/40 text-slate-400 border-slate-700"
              >
                not configured
              </span>
              <span
                v-else-if="remoteOnline"
                class="px-2 py-0.5 rounded-full border text-[10px] font-mono bg-emerald-500/10 text-emerald-400 border-emerald-500/30"
              >
                connected — serving {{ remoteServingModel || 'remote model' }}
              </span>
              <span
                v-else
                class="px-2 py-0.5 rounded-full border text-[10px] font-mono bg-amber-500/10 text-amber-400 border-amber-500/30"
              >
                configured but unreachable — check URL, key and model
              </span>
            </div>
            <div class="flex items-center justify-between gap-3">
              <span class="text-xs text-slate-400 font-mono flex items-center gap-1.5">
                <Zap class="w-3.5 h-3.5 text-amber-400" />
                RunPod Serverless
              </span>
              <span
                v-if="!runpodConfigured"
                class="px-2 py-0.5 rounded-full border text-[10px] font-mono bg-slate-800/40 text-slate-400 border-slate-700"
              >
                not configured
              </span>
              <span
                v-else-if="runpodReachable"
                class="px-2 py-0.5 rounded-full border text-[10px] font-mono bg-emerald-500/10 text-emerald-400 border-emerald-500/30"
              >
                connected — {{ runpodEndpointCount }} endpoint{{ runpodEndpointCount === 1 ? '' : 's' }}
              </span>
              <span
                v-else
                class="px-2 py-0.5 rounded-full border text-[10px] font-mono bg-rose-500/10 text-rose-400 border-rose-500/30"
              >
                auth failed
              </span>
            </div>
          </div>
        </div>

        <!-- Sampling parameters -->
        <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4">
          <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2 border-b border-slate-800 pb-3">
            <SlidersHorizontal class="w-4 h-4 text-indigo-400" />
            Sampling Parameters
          </h4>

          <div class="space-y-4">
            <!-- Temperature -->
            <div class="space-y-2">
              <div class="flex items-center justify-between text-xs">
                <span class="text-slate-400">Temperature</span>
                <span class="font-mono text-indigo-400 bg-indigo-500/10 px-2 py-0.5 rounded font-bold">{{ config.temperature }}</span>
              </div>
              <input
                type="range"
                min="0"
                max="1.2"
                step="0.05"
                :value="config.temperature"
                @input="updateField('temperature', parseFloat(($event.target as HTMLInputElement).value))"
                class="w-full h-1 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-indigo-500"
              />
            </div>

            <!-- Top P -->
            <div class="space-y-2">
              <div class="flex items-center justify-between text-xs">
                <span class="text-slate-400">Top-P Sampling</span>
                <span class="font-mono text-indigo-400 bg-indigo-500/10 px-2 py-0.5 rounded font-bold">{{ config.topP }}</span>
              </div>
              <input
                type="range"
                min="0"
                max="1"
                step="0.05"
                :value="config.topP"
                @input="updateField('topP', parseFloat(($event.target as HTMLInputElement).value))"
                class="w-full h-1 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-indigo-500"
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
