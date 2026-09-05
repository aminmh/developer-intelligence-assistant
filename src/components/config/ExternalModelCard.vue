<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue';
import { Zap, RefreshCw, CheckCircle, AlertTriangle } from 'lucide-vue-next';
import { RAGConfig } from '../../types';
import { makeRequest } from '../../ui/makeRequest';

const { config, updateField } = defineProps<{
  config: RAGConfig;
  updateField: <K extends keyof RAGConfig>(key: K, value: RAGConfig[K]) => void;
}>();

// Preset base URLs deliberately omit "/v1" — the backend appends it.
const remotePresets = [
  { id: 'openai', label: 'OpenAI', baseUrl: 'https://api.openai.com' },
  { id: 'groq', label: 'Groq', baseUrl: 'https://api.groq.com/openai' },
  { id: 'openrouter', label: 'OpenRouter', baseUrl: 'https://openrouter.ai/api' },
  { id: 'together', label: 'Together AI', baseUrl: 'https://api.together.xyz' },
  { id: 'lmstudio', label: 'LM Studio', baseUrl: 'http://host.docker.internal:1234' },
  { id: 'custom', label: 'Custom', baseUrl: '' },
];

const remotePresetId = computed({
  get: () => {
    const url = (config.llmRemoteBaseUrl ?? '').trim().replace(/\/+$/, '');
    const hit = remotePresets.find((p) => p.baseUrl === url);
    return hit ? hit.id : 'custom';
  },
  set: (id: string) => {
    const preset = remotePresets.find((p) => p.id === id);
    if (preset) updateField('llmRemoteBaseUrl', preset.baseUrl);
  },
});

const remoteResult = ref<{ ok: boolean; latency_ms: number; detail: string; model: string } | null>(null);
const testingRemote = ref(false);

const remoteLabel = computed(() => {
  const r = remoteResult.value;
  if (!r) return remoteConfigured.value ? 'Not tested' : 'Not configured';
  return r.ok ? `Connected · ${r.latency_ms}ms${r.model ? ` · ${r.model}` : ''}` : r.detail || 'Failed';
});
const remoteConnCls = computed(() => {
  const r = remoteResult.value;
  if (!r) return remoteConfigured.value ? 'text-slate-500' : 'text-slate-600';
  return r.ok ? 'text-emerald-300' : 'text-rose-300';
});
const remoteConfigured = computed(() => Boolean((config.llmRemoteBaseUrl ?? '').trim()));

const testRemoteConnection = async () => {
  testingRemote.value = true;
  remoteResult.value = null;
  try {
    const res = await makeRequest('/api/health/test/remote', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const data = await res.json();
    remoteResult.value = {
      ok: Boolean(data.ok),
      latency_ms: Number(data.latency_ms) || 0,
      detail: String(data.detail ?? data.error ?? 'request failed'),
      model: String(data.model ?? ''),
    };
  } catch (err) {
    remoteResult.value = { ok: false, latency_ms: 0, detail: String(err), model: '' };
  } finally {
    testingRemote.value = false;
  }
};

// --- LM Studio (MLX engine) -------------------------------------------------

const isLmStudio = computed(() =>
  /(localhost|host\.docker\.internal):1234/i.test(config.llmRemoteBaseUrl ?? ''),
);

const lmstudioModels = ref<{ id: string; engine: string; type?: string; quantization?: string | null }[]>([]);
const lmSelectedModel = ref('');
const lmEmbeddingModels = computed(() => lmstudioModels.value.filter((m) => m.type === 'embeddings'));
const lmSelectedEmbedding = ref('');
const lmStatus = ref<{ ok: boolean; text: string } | null>(null);
const loadingLmModels = ref(false);
const loadingLmLoad = ref(false);
// True while fetchLmModels() is auto-selecting a default that was NOT the
// configured chat model — skip auto-load so we don't surprise-load a model.
const lmSuppressAutoLoad = ref(false);
// Same for the embedding select.
const lmSuppressEmbedAutoLoad = ref(false);

watch(lmSelectedModel, (value, oldValue) => {
  if (lmSuppressAutoLoad.value) {
    lmSuppressAutoLoad.value = false;
    return;
  }
  if (value && value !== oldValue) loadLmModel();
});

watch(
  () => config.llmRemoteBaseUrl,
  (url) => {
    if (/(localhost|host\.docker\.internal):1234/i.test(url ?? '')) fetchLmModels();
  },
);

const fetchLmModels = async () => {
  loadingLmModels.value = true;
  lmStatus.value = null;
  try {
    const res = await makeRequest('/api/lmstudio/models');
    const data = await res.json();
    if (!data.success) {
      lmStatus.value = { ok: false, text: data.detail || 'Unable to list models.' };
      lmstudioModels.value = [];
      return;
    }
    lmstudioModels.value = data.models || [];
    const chatModels = lmstudioModels.value.filter((m) => m.type !== 'embeddings');
    const configured = config.llmRemoteModel;
    const hasConfigured = chatModels.some((m) => m.id === configured);
    const autoPick = hasConfigured
      ? configured
      : (chatModels.find((m) => m.engine === 'mlx') || chatModels[0])?.id || '';
    if (autoPick && !lmstudioModels.value.some((m) => m.id === lmSelectedModel.value)) {
      lmSuppressAutoLoad.value = !hasConfigured;
      lmSelectedModel.value = autoPick;
    }
    const embed = config.ollamaEmbedModel;
    const hasEmbed = lmEmbeddingModels.value.some((m) => m.id === embed);
    const embedPick = hasEmbed
      ? embed
      : (lmEmbeddingModels.value.find((m) => m.type === 'embeddings') || lmEmbeddingModels.value[0])?.id || '';
    if (embedPick && embedPick !== lmSelectedEmbedding.value) {
      lmSuppressEmbedAutoLoad.value = !hasEmbed;
      lmSelectedEmbedding.value = embedPick;
    }
    if (!lmstudioModels.value.length) {
      lmStatus.value = { ok: false, text: 'No models found on the LM Studio server.' };
    }
  } catch (err) {
    lmStatus.value = { ok: false, text: String(err) };
    lmstudioModels.value = [];
  } finally {
    loadingLmModels.value = false;
  }
};

watch(lmSelectedEmbedding, (value, oldValue) => {
  if (lmSuppressEmbedAutoLoad.value) {
    lmSuppressEmbedAutoLoad.value = false;
    return;
  }
  if (!value || value === oldValue) return;
  updateField('ollamaEmbedModel', value);
  updateField('embeddingBaseUrl', config.llmRemoteBaseUrl || config.embeddingBaseUrl);
  lmStatus.value = { ok: true, text: `Embedding model set to ${value} — embeddings now served by this server.` };
});

const loadLmModel = async () => {
  if (!lmSelectedModel.value) {
    lmStatus.value = { ok: false, text: 'Select a model first.' };
    return;
  }
  loadingLmLoad.value = true;
  lmStatus.value = null;
  try {
    const res = await makeRequest('/api/lmstudio/load', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: lmSelectedModel.value }),
    });
    const data = await res.json();
    if (!data.success) {
      lmStatus.value = { ok: false, text: data.detail || 'Load failed.' };
      return;
    }
    updateField('llmRemoteModel', lmSelectedModel.value);
    lmStatus.value = { ok: true, text: `Loaded into engine${data.status ? ` (${data.status})` : ''} — set as chat model.` };
  } catch (err) {
    lmStatus.value = { ok: false, text: String(err) };
  } finally {
    loadingLmLoad.value = false;
  }
};

onMounted(() => {
  if (isLmStudio.value) fetchLmModels();
});
</script>

<template>
  <!-- ============ External OpenAI-Compatible Models ============ -->
  <div class="space-y-3">
    <div class="flex items-center gap-2">
      <span class="w-1 h-3.5 bg-sky-500 rounded-full"></span>
      <h4 class="text-[11px] font-bold text-slate-400 uppercase tracking-widest">External OpenAI-Compatible Models</h4>
    </div>

    <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4 max-w-2xl">
      <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2 border-b border-slate-800 pb-3">
        <Zap class="w-4 h-4 text-sky-400" />
        OpenAI-Compatible Endpoint
      </h4>

      <div class="flex items-center justify-between gap-4">
        <label class="text-xs text-slate-400 font-mono shrink-0">Provider</label>
        <select
          v-model="remotePresetId"
          class="w-[15rem] max-w-full bg-[#11162d] border border-slate-800 rounded-xl px-3 py-2.5 text-xs font-mono focus:outline-none focus:border-sky-500 text-slate-200"
        >
          <option v-for="p in remotePresets" :key="p.id" :value="p.id">{{ p.label }}</option>
        </select>
      </div>

      <div class="space-y-1.5">
        <label class="text-xs text-slate-400 font-mono">Base URL</label>
        <input
          :value="config.llmRemoteBaseUrl"
          @input="updateField('llmRemoteBaseUrl', ($event.target as HTMLInputElement).value)"
          type="text"
          class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-sky-500 text-slate-200"
          placeholder="e.g. https://api.openai.com"
        />
        <p class="text-[9px] text-slate-600">Any OpenAI-compatible endpoint — OpenAI, Groq, OpenRouter, Together, LM Studio, vLLM. Leave empty to disable.</p>
      </div>

      <div class="space-y-1.5">
        <label class="text-xs text-slate-400 font-mono">Model</label>
        <input
          :value="config.llmRemoteModel"
          @input="updateField('llmRemoteModel', ($event.target as HTMLInputElement).value)"
          type="text"
          class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-sky-500 text-slate-200"
          placeholder="e.g. gpt-4o-mini"
        />
      </div>

      <div class="space-y-1.5">
        <label class="text-xs text-slate-400 font-mono">API Key</label>
        <input
          :value="config.llmRemoteApiKey"
          @input="updateField('llmRemoteApiKey', ($event.target as HTMLInputElement).value)"
          type="password"
          class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-sky-500 text-slate-200"
          placeholder="sk-..."
        />
        <p class="text-[9px] text-slate-600">Optional — local endpoints (LM Studio, vLLM) usually need none.</p>
      </div>

      <div class="pt-3 border-t border-slate-800 flex items-center justify-between gap-2">
        <div class="min-w-0 flex-1 truncate text-[10px] font-mono" :class="remoteConnCls">
          <CheckCircle v-if="remoteResult?.ok" class="w-3 h-3 inline mr-1 shrink-0" />
          <AlertTriangle v-else-if="remoteResult" class="w-3 h-3 inline mr-1 shrink-0" />
          {{ remoteLabel }}
        </div>
        <button
          type="button"
          @click="testRemoteConnection"
          :disabled="testingRemote"
          class="text-[10px] font-mono text-slate-400 hover:text-sky-300 flex items-center gap-1 disabled:opacity-50 shrink-0"
        >
          <RefreshCw class="w-3 h-3" :class="testingRemote ? 'animate-spin' : ''" />
          {{ testingRemote ? 'Testing…' : 'Test connection' }}
        </button>
      </div>

      <!-- LM Studio MLX models -->
      <div v-if="isLmStudio" class="pt-4 border-t border-slate-800 space-y-3">
        <div class="flex items-center justify-between gap-2">
          <label class="text-xs text-slate-400 font-mono shrink-0">MLX Models</label>
          <button
            type="button"
            @click="fetchLmModels"
            :disabled="loadingLmModels"
            class="text-[10px] font-mono text-slate-400 hover:text-sky-300 flex items-center gap-1 disabled:opacity-50 shrink-0"
          >
            <RefreshCw class="w-3 h-3" :class="loadingLmModels ? 'animate-spin' : ''" />
            {{ lmstudioModels.length ? 'refresh' : 'list' }}
          </button>
        </div>

        <div v-if="lmstudioModels.length" class="flex items-center justify-between gap-2">
          <select
            v-model="lmSelectedModel"
            class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-3 py-2 text-xs font-mono focus:outline-none focus:border-sky-500 text-slate-200"
          >
            <option v-for="m in lmstudioModels.filter((x) => x.type !== 'embeddings')" :key="m.id" :value="m.id">
              {{ m.id }} · {{ m.engine === 'mlx' ? 'MLX' : m.engine }}<template v-if="m.quantization"> · {{ m.quantization }}</template>
            </option>
          </select>
          <button
            type="button"
            @click="loadLmModel"
            :disabled="loadingLmLoad"
            class="text-[10px] font-mono text-sky-300 hover:text-sky-200 flex items-center gap-1 disabled:opacity-50 shrink-0 border border-sky-500/30 rounded-lg px-3 py-2"
          >
            <RefreshCw class="w-3 h-3" :class="loadingLmLoad ? 'animate-spin' : ''" />
            {{ loadingLmLoad ? 'Loading…' : 'Load' }}
          </button>
        </div>

        <!-- Embedding model on the same server -->
        <div class="space-y-1.5">
          <div class="flex items-center justify-between gap-2">
            <label class="text-xs text-slate-400 font-mono shrink-0">Embedding Model</label>
            <span
              v-if="config.embeddingBaseUrl"
              class="text-[9px] font-mono text-emerald-400 bg-emerald-500/10 px-1.5 py-0.5 rounded"
            >via LM Studio</span>
            <span
              v-else
              class="text-[9px] font-mono text-slate-500 bg-slate-800/60 px-1.5 py-0.5 rounded"
            >via Ollama</span>
          </div>
          <select
            v-model="lmSelectedEmbedding"
            class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-3 py-2 text-xs font-mono focus:outline-none focus:border-sky-500 text-slate-200"
          >
            <option value="" disabled>Select an embedding model…</option>
            <option v-for="m in lmEmbeddingModels" :key="m.id" :value="m.id">
              {{ m.id }}<template v-if="m.quantization"> · {{ m.quantization }}</template>
            </option>
          </select>
          <button
            v-if="config.embeddingBaseUrl"
            type="button"
            @click="updateField('embeddingBaseUrl', '')"
            class="text-[9px] font-mono text-slate-500 hover:text-amber-300"
          >
            use Ollama embeddings instead
          </button>
          <p class="text-[9px] text-slate-600">
            Embeddings for ingestion, retrieval and routing. Selecting one serves them from this server
            (<code class="text-slate-400">{{ config.llmRemoteBaseUrl || 'base url' }}</code> via
            <code class="text-slate-400">/v1/embeddings</code>).
          </p>
        </div>

        <p v-if="lmStatus" class="text-[10px] font-mono" :class="lmStatus.ok ? 'text-emerald-300' : 'text-amber-300'">
          <CheckCircle v-if="lmStatus.ok" class="w-3 h-3 inline mr-1" />
          <AlertTriangle v-else class="w-3 h-3 inline mr-1" />
          {{ lmStatus.text }}
        </p>
        <p class="text-[9px] text-slate-600">
          Selecting a model pre-loads it into the LM Studio engine (MLX for mlx-format models) and sets it as the chat model. The configured chat model is pre-loaded automatically when the list refreshes.
        </p>
      </div>
    </div>
  </div>
</template>
