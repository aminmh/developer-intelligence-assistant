<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue';
import { Zap, RefreshCw, CheckCircle, AlertTriangle } from 'lucide-vue-next';
import { RAGConfig } from '../../types';
import { makeRequest } from '../../ui/makeRequest';
import { toast } from '../../composables/useToasts';

const { config, updateField } = defineProps<{
  config: RAGConfig;
  updateField: <K extends keyof RAGConfig>(key: K, value: RAGConfig[K]) => void;
}>();

const embeddingProviders = [
  { id: 'ollama', label: 'Ollama' },
  { id: 'openAICompatible', label: 'OpenAI-compatible' },
  { id: 'cohere', label: 'Cohere' },
  { id: 'cloudflareWorkersAI', label: 'Cloudflare Workers AI' },
] as const;
const feedbackRebuild = ref<{ job_id?: string; status: string; stage?: string; total: number; indexed: number; error?: string } | null>(null);
const rebuildingFeedback = ref(false);
const embeddingResult = ref<{ ok: boolean; latency_ms: number; detail: string; provider?: string; model?: string | null; dimensions?: number; status_code?: number | null; retry_after_seconds?: number | null } | null>(null);
const testingEmbedding = ref(false);

const embeddingLabel = computed(() => {
  const result = embeddingResult.value;
  if (!result) return 'Not tested';
  return result.ok
    ? `Connected · ${result.latency_ms}ms${result.model ? ` · ${result.model}` : ''}`
    : result.detail || 'Failed';
});

const testEmbeddingConnection = async () => {
  testingEmbedding.value = true;
  embeddingResult.value = null;
  try {
    const response = await makeRequest('/api/health/test/embedding', { method: 'POST' });
    const body = await response.text();
    let data: any = null;
    try {
      data = body ? JSON.parse(body) : null;
    } catch {
      data = null;
    }
    if (!data || typeof data !== 'object') {
      throw new Error(`Embedding test returned an invalid response (${response.status}).`);
    }
    embeddingResult.value = {
      ok: Boolean(data.ok),
      latency_ms: Number(data.latency_ms) || 0,
      detail: String(data.detail ?? data.error ?? 'request failed'),
      provider: String(data.provider ?? ''),
      model: data.model ? String(data.model) : null,
      dimensions: Number(data.dimensions) || 0,
      status_code: Number(data.status_code) || null,
      retry_after_seconds: Number(data.retry_after_seconds) || null,
    };
    if (embeddingResult.value.ok) toast.success(embeddingLabel.value);
    else toast.error(embeddingLabel.value);
  } catch (error: any) {
    embeddingResult.value = { ok: false, latency_ms: 0, detail: String(error) };
    toast.error(embeddingLabel.value);
  } finally {
    testingEmbedding.value = false;
  }
};

const rebuildRouteFeedback = async () => {
  rebuildingFeedback.value = true;
  feedbackRebuild.value = { status: 'queued', stage: 'queued', total: 0, indexed: 0 };
  try {
    const response = await makeRequest('/api/embeddings/rebuild-route-feedback', { method: 'POST' });
    const created = await response.json();
    if (!response.ok || !created.job_id) throw new Error(created.detail || 'Could not start route-feedback rebuild.');
    for (let attempt = 0; attempt < 300; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      const statusResponse = await makeRequest(`/api/embeddings/rebuild/${encodeURIComponent(created.job_id)}`);
      const status = await statusResponse.json();
      if (!statusResponse.ok) throw new Error(status.detail || 'Could not read rebuild progress.');
      feedbackRebuild.value = status;
      if (status.status === 'done') {
        if (status.stage !== 'empty') toast.success(`Rebuilt ${status.indexed} routing corrections using Cohere.`);
        return;
      }
      if (status.status === 'error') throw new Error(status.error || 'Route-feedback rebuild failed.');
    }
    throw new Error('Route-feedback rebuild timed out. Check the API service before retrying.');
  } catch (error: any) {
    feedbackRebuild.value = {
      ...(feedbackRebuild.value || { total: 0, indexed: 0 }),
      status: 'error', stage: 'error', error: error?.message || 'Route-feedback rebuild failed.',
    };
    toast.error(error?.message || 'Route-feedback rebuild failed.');
  } finally {
    rebuildingFeedback.value = false;
  }
};

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
      // The values currently typed, saved or not. The API key is never sent: the test uses the saved one.
      body: JSON.stringify({ base_url: config.llmRemoteBaseUrl ?? '', model: config.llmRemoteModel ?? '' }),
    });
    const data = await res.json();
    remoteResult.value = {
      ok: Boolean(data.ok),
      latency_ms: Number(data.latency_ms) || 0,
      detail: String(data.detail ?? data.error ?? 'request failed'),
      model: String(data.model ?? ''),
    };
    if (remoteResult.value.ok) toast.success(remoteLabel.value);
    else toast.error(remoteLabel.value);
  } catch (err) {
    remoteResult.value = { ok: false, latency_ms: 0, detail: String(err), model: '' };
    toast.error(remoteLabel.value);
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
  try {
    const res = await makeRequest('/api/lmstudio/models');
    const data = await res.json();
    if (!data.success) {
      toast.error(data.detail || 'Unable to list models.');
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
      toast.error('No models found on the LM Studio server.');
    }
  } catch (err) {
    toast.error(String(err));
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
  toast.success(`Embedding model set to ${value} — embeddings now served by this server.`);
});

const loadLmModel = async () => {
  if (!lmSelectedModel.value) {
    toast.error('Select a model first.');
    return;
  }
  loadingLmLoad.value = true;
  try {
    const res = await makeRequest('/api/lmstudio/load', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: lmSelectedModel.value }),
    });
    const data = await res.json();
    if (!data.success) {
      toast.error(data.detail || 'Load failed.');
      return;
    }
    updateField('llmRemoteModel', lmSelectedModel.value);
    toast.success(`Loaded into engine${data.status ? ` (${data.status})` : ''} — set as chat model.`);
  } catch (err) {
    toast.error(String(err));
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
        Embedding Provider
      </h4>
      <div class="space-y-1.5">
        <label for="embedding-provider" class="text-xs text-slate-400 font-mono">Provider</label>
        <select
          id="embedding-provider"
          :value="config.embeddingProvider"
          @change="updateField('embeddingProvider', ($event.target as HTMLSelectElement).value as RAGConfig['embeddingProvider'])"
          class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-3 py-2.5 text-xs font-mono focus:outline-none focus:border-sky-500 text-slate-200"
        >
          <option v-for="provider in embeddingProviders" :key="provider.id" :value="provider.id">{{ provider.label }}</option>
        </select>
      </div>
      <div v-if="config.embeddingProvider === 'cohere'" class="space-y-1.5">
        <label for="cohere-api-key" class="text-xs text-slate-400 font-mono">Cohere API key</label>
        <input
          id="cohere-api-key"
          :value="config.cohereApiKey"
          @input="updateField('cohereApiKey', ($event.target as HTMLInputElement).value)"
          type="password"
          autocomplete="new-password"
          class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-sky-500 text-slate-200"
          placeholder="Leave blank to keep the saved key"
        />
        <p class="text-[9px] text-slate-500">Stored by the API service and never returned to the browser. Cohere uses Embed v5 Pro for indexing and Fast for queries.</p>
        <div class="pt-3 border-t border-slate-800 space-y-2">
          <div class="flex items-center justify-between gap-3">
            <div>
              <p class="text-[10px] text-slate-300">Route feedback vectors</p>
              <p class="text-[9px] text-slate-500">Rebuild routing corrections saved through Chat using Cohere. No rebuild is needed until you have saved feedback.</p>
            </div>
            <button
              type="button"
              @click="rebuildRouteFeedback"
              :disabled="rebuildingFeedback"
              class="shrink-0 px-3 py-2 text-[10px] font-mono text-cyan-100 bg-cyan-700 hover:bg-cyan-600 rounded-lg disabled:opacity-50"
            >
              {{ rebuildingFeedback ? 'Rebuilding…' : 'Rebuild feedback' }}
            </button>
          </div>
          <div v-if="feedbackRebuild && feedbackRebuild.status !== 'error'" role="status" aria-live="polite" class="space-y-1.5">
            <p v-if="feedbackRebuild.stage === 'empty'" class="text-[10px] text-slate-400">No routing feedback saved yet. There’s nothing to rebuild. To save a correction in Chat, select “wrong route?” below a response and choose the correct route.</p>
            <div v-else-if="feedbackRebuild.status !== 'done'" class="flex justify-between text-[9px] text-slate-400">
              <span>{{ feedbackRebuild.stage === 'upserting' ? 'Saving feedback vectors…' : (feedbackRebuild.stage === 'activating' ? 'Activating feedback vectors…' : `Embedding feedback: ${feedbackRebuild.indexed}/${feedbackRebuild.total}`) }}</span>
              <span v-if="feedbackRebuild.total">{{ Math.round((feedbackRebuild.indexed / feedbackRebuild.total) * 100) }}%</span>
            </div>
            <div v-if="feedbackRebuild.total" class="h-1.5 bg-slate-800 rounded-full overflow-hidden" role="progressbar" :aria-valuenow="Math.min(100, Math.round((feedbackRebuild.indexed / feedbackRebuild.total) * 100))" aria-valuemin="0" aria-valuemax="100" aria-label="Rebuilding route-feedback embeddings">
              <div class="h-full bg-cyan-500 transition-all duration-300" :style="{ width: `${(feedbackRebuild.indexed / feedbackRebuild.total) * 100}%` }"></div>
            </div>
          </div>
        </div>
      </div>
      <div v-else-if="config.embeddingProvider === 'cloudflareWorkersAI'" class="space-y-3">
        <div class="space-y-1.5">
          <label for="cloudflare-account-id" class="text-xs text-slate-400 font-mono">Cloudflare account ID</label>
          <input
            id="cloudflare-account-id"
            :value="config.cloudflareAccountId"
            @input="updateField('cloudflareAccountId', ($event.target as HTMLInputElement).value)"
            type="text"
            autocomplete="off"
            class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-sky-500 text-slate-200"
            placeholder="Your Cloudflare account ID"
          />
        </div>
        <div class="space-y-1.5">
          <label for="cloudflare-api-token" class="text-xs text-slate-400 font-mono">Cloudflare API token</label>
          <input
            id="cloudflare-api-token"
            :value="config.cloudflareApiToken"
            @input="updateField('cloudflareApiToken', ($event.target as HTMLInputElement).value)"
            type="password"
            autocomplete="new-password"
            class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-sky-500 text-slate-200"
            placeholder="Leave blank to keep the saved token"
          />
        </div>
        <div class="space-y-1.5">
          <label for="cloudflare-embedding-model" class="text-xs text-slate-400 font-mono">Embedding model</label>
          <input
            id="cloudflare-embedding-model"
            :value="config.cloudflareEmbeddingModel"
            @input="updateField('cloudflareEmbeddingModel', ($event.target as HTMLInputElement).value)"
            type="text"
            class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-sky-500 text-slate-200"
            placeholder="@cf/baai/bge-base-en-v1.5"
          />
          <p class="text-[9px] text-slate-500">Use a token with Workers AI Read and Edit permissions scoped to this account. Switching providers requires reindexing the revision.</p>
        </div>
      </div>
      <p v-else-if="config.embeddingProvider === 'openAICompatible'" class="text-[9px] text-slate-500">Uses the configured embedding base URL and model.</p>
      <p v-else class="text-[9px] text-slate-500">Ollama remains the default embedding provider.</p>
      <div class="pt-3 border-t border-slate-800 flex items-center justify-between gap-2">
        <div class="min-w-0 flex-1 truncate text-[10px] font-mono" :class="embeddingResult?.ok ? 'text-emerald-300' : embeddingResult ? 'text-rose-300' : 'text-slate-500'">
          <CheckCircle v-if="embeddingResult?.ok" class="w-3 h-3 inline mr-1 shrink-0" />
          <AlertTriangle v-else-if="embeddingResult" class="w-3 h-3 inline mr-1 shrink-0" />
          <span :title="embeddingLabel">{{ embeddingLabel }}</span>
        </div>
        <button
          type="button"
          @click="testEmbeddingConnection"
          :disabled="testingEmbedding"
          class="text-[10px] font-mono text-slate-400 hover:text-sky-300 flex items-center gap-1 disabled:opacity-50 shrink-0"
        >
          <RefreshCw class="w-3 h-3" :class="testingEmbedding ? 'animate-spin' : ''" />
          {{ testingEmbedding ? 'Testing…' : 'Test embedding' }}
        </button>
      </div>
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
        <label for="assessment-token-limit" class="text-xs text-slate-400 font-mono">Assessment request token limit</label>
        <input
          id="assessment-token-limit"
          :value="config.assessmentRequestTokenLimit"
          @input="updateField('assessmentRequestTokenLimit', ($event.target as HTMLInputElement).valueAsNumber)"
          type="number" min="0" max="10000000" step="1"
          aria-describedby="assessment-token-limit-help"
          class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-sky-500 text-slate-200"
        />
        <p id="assessment-token-limit-help" class="text-xs text-slate-400">Maximum input and output tokens per external-model assessment request. Use 0 for no additional limit; the model context limit still applies. Check your provider's request and rate limits.</p>
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
        <p class="text-[9px] text-slate-600">Optional — local endpoints (LM Studio, vLLM) usually need none. Test connection uses the saved key; the other fields are tested as typed.</p>
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

        <p class="text-[9px] text-slate-600">
          Selecting a model pre-loads it into the LM Studio engine (MLX for mlx-format models) and sets it as the chat model. The configured chat model is pre-loaded automatically when the list refreshes.
        </p>
      </div>
    </div>
  </div>
</template>
