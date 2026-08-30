<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import {
  Sliders,
  Cpu,
  Database,
  SlidersHorizontal,
  RefreshCw,
  Server,
  FolderSearch,
  Upload,
  CheckCircle,
  AlertTriangle,
  Gauge,
  Zap,
  Shield,
} from 'lucide-vue-next';
import { RAGConfig, OllamaImportFile, OllamaImportJob } from '../types';

const props = defineProps<{
  config: RAGConfig;
}>();

const emit = defineEmits<{
  (e: 'saveConfig'): void;
}>();

const updateField = <K extends keyof RAGConfig>(key: K, value: RAGConfig[K]) => {
  props.config[key] = value;
};

const ollamaModels = ref<string[]>([]);
const loadingModels = ref(false);
const importRoot = ref('');
const ggufFiles = ref<OllamaImportFile[]>([]);
const selectedGgufFile = ref('');
const importModelName = ref('');
const importModelfile = ref('');
const scanningImportDirectory = ref(false);
const startingImport = ref(false);
const importJob = ref<OllamaImportJob | null>(null);
const importError = ref('');
const importSuccess = ref('');
let importPollTimer: ReturnType<typeof setTimeout> | null = null;

const llmHealth = ref<any>(null);
const loadingHealth = ref(false);

const apiBaseUrl = import.meta.env.VITE_API_URL || '';

const makeRequest = async (path: string, options: RequestInit = {}) => {
  const base = apiBaseUrl.replace(/\/+$/, '');
  const url = base ? `${base}${path}` : path;
  return fetch(url, options);
};

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

// The backend may return health as nested ({ ollama, remote }) or flat
// ({ ollama_online, remote_configured, ... }) payloads — normalize both at
// the boundary so the template only ever sees parsed, trusted values.
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
    h.configured ??
      h.online != null ??
      h.reachable != null ??
      Boolean(h.model ?? h.serving_model ?? h.baseUrl ?? h.llmRemoteBaseUrl),
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

// --- RunPod serverless connection -----------------------------------------

const runpodResult = ref<{ ok: boolean; latency_ms: number; detail: string } | null>(null);
const testingRunpod = ref(false);

const runpodConfigured = computed(() => Boolean((props.config.runpodApiKey ?? '').trim()));
const runpodHealth = computed(() => {
  const h = llmHealth.value;
  if (!h) return null;
  return h.runpod ?? null;
});
const runpodReachable = computed(() => Boolean(runpodHealth.value?.reachable));
const runpodEndpointCount = computed(() => {
  const n = Number(runpodHealth.value?.endpoint_count ?? 0);
  return Number.isFinite(n) ? n : 0;
});
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

// --- Sandbox runner (nested gVisor DinD) -----------------------------------

const sandboxStatus = ref<{
  available: boolean;
  backend: string;
  runsc: boolean;
  detail: string;
  dockerHost: string;
  jobsRoot: string;
} | null>(null);
const loadingSandbox = ref(false);

const fetchSandboxStatus = async () => {
  loadingSandbox.value = true;
  try {
    const res = await makeRequest('/api/sandbox/status');
    const data = await res.json();
    if (res.ok && data.success) {
      sandboxStatus.value = data;
    } else {
      sandboxStatus.value = null;
    }
  } catch {
    sandboxStatus.value = null;
  } finally {
    loadingSandbox.value = false;
  }
};

// --- External OpenAI-compatible endpoint -----------------------------------

// Preset base URLs deliberately omit "/v1" — the backend appends it.
const remotePresets = [
  { id: 'openai', label: 'OpenAI', baseUrl: 'https://api.openai.com' },
  { id: 'groq', label: 'Groq', baseUrl: 'https://api.groq.com/openai' },
  { id: 'openrouter', label: 'OpenRouter', baseUrl: 'https://openrouter.ai/api' },
  { id: 'together', label: 'Together AI', baseUrl: 'https://api.together.xyz' },
  { id: 'lmstudio', label: 'LM Studio', baseUrl: 'http://localhost:1234' },
  { id: 'custom', label: 'Custom', baseUrl: '' },
];

const remotePresetId = computed({
  get: () => {
    const url = (props.config.llmRemoteBaseUrl ?? '').trim().replace(/\/+$/, '');
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

const fetchModels = async () => {
  loadingModels.value = true;
  try {
    const res = await fetch('/api/ollama/models');
    const data = await res.json();
    if (data.success) {
      ollamaModels.value = data.models || [];
    }
  } catch {
    // leave the list empty; the current value is still shown as a fallback option
  } finally {
    loadingModels.value = false;
  }
};

const scanImportDirectory = async () => {
  scanningImportDirectory.value = true;
  importError.value = '';
  importSuccess.value = '';
  try {
    const res = await fetch('/api/ollama/import-directory');
    const data = await res.json();
    if (!res.ok || !data.success) throw new Error(data.error || 'Unable to scan the import directory.');
    importRoot.value = data.root || '';
    ggufFiles.value = (data.files || []).map((file: string) => ({ file }));
    if (!selectedGgufFile.value || !data.files.includes(selectedGgufFile.value)) {
      selectedGgufFile.value = data.files[0] || '';
    }
    if (!data.files.length) importError.value = 'No .gguf files were found in the configured directory.';
  } catch (error: any) {
    importError.value = error?.message || 'Unable to scan the import directory.';
  } finally {
    scanningImportDirectory.value = false;
  }
};

const stopImportPolling = () => {
  if (importPollTimer) {
    clearTimeout(importPollTimer);
    importPollTimer = null;
  }
};

const pollImport = async (jobId: string) => {
  try {
    const res = await fetch(`/api/ollama/import/${encodeURIComponent(jobId)}`);
    const data = await res.json();
    if (!res.ok || !data.success) throw new Error(data.error || 'Unable to read import status.');
    importJob.value = data.job;
    if (data.job.status === 'pending' || data.job.status === 'running') {
      importPollTimer = setTimeout(() => pollImport(jobId), 1200);
    } else if (data.job.status === 'success') {
      importSuccess.value = `${data.job.modelName} is now available in Ollama.`;
      await fetchModels();
    } else {
      importError.value = data.job.error || data.job.message || 'Ollama model import failed.';
    }
  } catch (error: any) {
    importError.value = error?.message || 'Unable to read import status.';
  }
};

const startImport = async () => {
  if (!selectedGgufFile.value || !importModelName.value.trim()) {
    importError.value = 'Select a GGUF file and enter an Ollama model name.';
    return;
  }
  stopImportPolling();
  startingImport.value = true;
  importError.value = '';
  importSuccess.value = '';
  importJob.value = null;
  try {
    const res = await fetch('/api/ollama/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        file: selectedGgufFile.value,
        modelName: importModelName.value.trim(),
        modelfile: importModelfile.value,
      }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) throw new Error(data.error || 'Unable to start Ollama import.');
    importJob.value = data.job;
    importPollTimer = setTimeout(() => pollImport(data.jobId), 500);
  } catch (error: any) {
    importError.value = error?.message || 'Unable to start Ollama import.';
  } finally {
    startingImport.value = false;
  }
};

// Ensure the currently-selected value is always present in the dropdown,
// even if it wasn't returned by /api/ollama/models.
const llmModelOptions = computed(() => {
  const list = [...ollamaModels.value];
  if (props.config.ollamaLlmModel && !list.includes(props.config.ollamaLlmModel)) {
    list.unshift(props.config.ollamaLlmModel);
  }
  return list;
});

const embedModelOptions = computed(() => {
  const list = [...ollamaModels.value];
  if (props.config.ollamaEmbedModel && !list.includes(props.config.ollamaEmbedModel)) {
    list.unshift(props.config.ollamaEmbedModel);
  }
  return list;
});

const evalModelOptions = computed(() => {
  const list = [...ollamaModels.value];
  if (props.config.ollamaEvalModel && !list.includes(props.config.ollamaEvalModel)) {
    list.unshift(props.config.ollamaEvalModel);
  }
  return list;
});

onMounted(() => {
  fetchModels();
  scanImportDirectory();
  fetchHealth();
  fetchSandboxStatus();
});

onUnmounted(stopImportPolling);
</script>

<template>
  <div class="flex-1 overflow-y-auto p-6 space-y-8 text-left">
    <div class="max-w-4xl mx-auto space-y-8">

      <div class="bg-slate-900/40 rounded-2xl border border-slate-800/80 p-6">
        <h3 class="text-lg font-bold text-white flex items-center gap-2">
          <Sliders class="w-5 h-5 text-indigo-400" />
          RAG Pipeline Configuration
        </h3>
        <p class="text-xs text-slate-400 mt-1">Configure LLM providers, GPU offload, the vector store and model import.</p>
      </div>

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
        </div>
      </div>

      <!-- ============ Vector Store ============ -->
      <div class="space-y-3">
        <div class="flex items-center gap-2">
          <span class="w-1 h-3.5 bg-emerald-500 rounded-full"></span>
          <h4 class="text-[11px] font-bold text-slate-400 uppercase tracking-widest">Vector Store</h4>
        </div>

        <!-- Qdrant -->
        <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4">
          <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2 border-b border-slate-800 pb-3">
            <Database class="w-4 h-4 text-indigo-400" />
            Qdrant Vector Database
          </h4>

          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div class="space-y-1.5">
              <div class="flex items-center justify-between">
                <label class="text-xs text-slate-400 font-mono">Server URL</label>
                <span class="text-[9px] text-slate-500">Optional</span>
              </div>
              <input
                :value="config.qdrantUrl"
                @input="updateField('qdrantUrl', ($event.target as HTMLInputElement).value)"
                type="text"
                class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-indigo-500 text-slate-200"
                placeholder="e.g. https://your-cluster.qdrant.tech"
              />
            </div>

            <div class="space-y-1.5">
              <div class="flex items-center justify-between">
                <label class="text-xs text-slate-400 font-mono">API Key</label>
                <span class="text-[9px] text-slate-500">Optional</span>
              </div>
              <input
                :value="config.qdrantApiKey"
                @input="updateField('qdrantApiKey', ($event.target as HTMLInputElement).value)"
                type="password"
                class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-indigo-500 text-slate-200"
                placeholder="Enter Qdrant cloud token"
              />
            </div>
          </div>
        </div>
      </div>

      <!-- ============ Model Import ============ -->
      <div class="space-y-3">
        <div class="flex items-center gap-2">
          <span class="w-1 h-3.5 bg-sky-500 rounded-full"></span>
          <h4 class="text-[11px] font-bold text-slate-400 uppercase tracking-widest">Model Import</h4>
        </div>

        <!-- GGUF import -->
        <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4">
          <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center justify-between gap-2 border-b border-slate-800 pb-3">
            <span class="flex items-center gap-2">
              <FolderSearch class="w-4 h-4 text-indigo-400" />
              Import GGUF Model
            </span>
            <button
              type="button"
              @click="scanImportDirectory"
              class="text-[10px] text-slate-400 hover:text-indigo-300 flex items-center gap-1 font-mono"
              :disabled="scanningImportDirectory"
            >
              <RefreshCw class="w-3 h-3" :class="scanningImportDirectory ? 'animate-spin' : ''" />
              scan
            </button>
          </h4>

          <p class="text-[10px] leading-relaxed text-slate-500">
            GGUF files must be placed in the shared directory configured for this deployment.
            The app uploads the selected file to Ollama and reports progress here.
          </p>

          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div class="space-y-1.5">
              <label class="text-xs text-slate-400 font-mono">Configured directory</label>
              <input
                :value="importRoot"
                readonly
                class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-[10px] font-mono text-slate-500"
                placeholder="Scan to load configured directory"
              />
            </div>

            <div class="space-y-1.5">
              <label class="text-xs text-slate-400 font-mono">Ollama model name</label>
              <input
                v-model="importModelName"
                type="text"
                class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-indigo-500 text-slate-200"
                placeholder="e.g. my-coder-model"
              />
            </div>
          </div>

          <div class="space-y-1.5">
            <label class="text-xs text-slate-400 font-mono">GGUF file</label>
            <select
              v-model="selectedGgufFile"
              class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-indigo-500 text-slate-200"
              :disabled="!ggufFiles.length"
            >
              <option value="" disabled>Select a GGUF file</option>
              <option v-for="entry in ggufFiles" :key="entry.file" :value="entry.file">{{ entry.file }}</option>
            </select>
          </div>

          <div class="space-y-1.5">
            <label class="text-xs text-slate-400 font-mono">Optional Modelfile settings</label>
            <textarea
              v-model="importModelfile"
              rows="4"
              class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-[10px] font-mono focus:outline-none focus:border-indigo-500 text-slate-200 resize-y"
              placeholder="SYSTEM You are a helpful coding assistant&#10;PARAMETER temperature 0.2"
            />
            <p class="text-[9px] text-slate-600">The app inserts the selected GGUF as the FROM source automatically.</p>
          </div>

          <button
            type="button"
            @click="startImport"
            :disabled="startingImport || importJob?.status === 'pending' || importJob?.status === 'running' || !ggufFiles.length"
            class="w-full md:w-auto flex items-center justify-center gap-2 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed text-white font-semibold text-xs px-6 py-2.5 rounded-xl transition-all"
          >
            <Upload class="w-3.5 h-3.5" />
            {{ startingImport ? 'Starting…' : importJob?.status === 'running' ? 'Importing…' : 'Import model' }}
          </button>

          <div v-if="importJob && (importJob.status === 'pending' || importJob.status === 'running')" class="flex items-center gap-2 text-[10px] text-indigo-300 font-mono">
            <RefreshCw class="w-3.5 h-3.5 animate-spin" />
            <span>{{ importJob.message }}</span>
          </div>
          <div v-if="importSuccess" class="flex items-start gap-2 text-[10px] text-emerald-300 font-mono">
            <CheckCircle class="w-3.5 h-3.5 shrink-0" />
            <span>{{ importSuccess }}</span>
          </div>
          <div v-if="importError" class="flex items-start gap-2 text-[10px] text-amber-300 font-mono">
            <AlertTriangle class="w-3.5 h-3.5 shrink-0" />
            <span>{{ importError }}</span>
          </div>
        </div>
      </div>

      <!-- ============ Sandbox Runner ============ -->
      <div class="space-y-3">
        <div class="flex items-center gap-2">
          <span class="w-1 h-3.5 bg-rose-500 rounded-full"></span>
          <h4 class="text-[11px] font-bold text-slate-400 uppercase tracking-widest">Sandbox Runner</h4>
        </div>

        <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4 max-w-2xl">
          <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center justify-between gap-2 border-b border-slate-800 pb-3">
            <span class="flex items-center gap-2">
              <Shield class="w-4 h-4 text-rose-400" />
              gVisor Sandbox (runsc)
            </span>
            <button
              type="button"
              @click="fetchSandboxStatus"
              class="text-[10px] text-slate-400 hover:text-indigo-300 flex items-center gap-1 font-mono"
              :disabled="loadingSandbox"
            >
              <RefreshCw class="w-3 h-3" :class="loadingSandbox ? 'animate-spin' : ''" />
              refresh
            </button>
          </h4>

          <div v-if="loadingSandbox" class="flex items-center gap-2 text-[10px] text-slate-400 font-mono">
            <RefreshCw class="w-3.5 h-3.5 animate-spin" />
            probing runner daemon…
          </div>
          <div v-else-if="!sandboxStatus" class="text-[10px] text-slate-500 font-mono">
            Status endpoint unavailable.
          </div>
          <div v-else class="space-y-3">
            <div class="flex items-center justify-between gap-3">
              <span class="text-xs text-slate-400 font-mono flex items-center gap-1.5">
                <Shield class="w-3.5 h-3.5 text-rose-400" />
                Sandbox backend
              </span>
              <span
                class="px-2 py-0.5 rounded-full border text-[10px] font-mono"
                :class="sandboxStatus.available
                  ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                  : 'bg-rose-500/10 text-rose-400 border-rose-500/30'"
              >
                {{ sandboxStatus.available ? `ready · ${sandboxStatus.backend}` : sandboxStatus.runsc ? 'runsc not registered' : 'unavailable' }}
              </span>
            </div>
            <div class="flex items-center justify-between gap-3">
              <span class="text-xs text-slate-400 font-mono">Docker host</span>
              <span class="text-[10px] font-mono text-slate-500 max-w-[60%] truncate" :title="sandboxStatus.dockerHost">{{ sandboxStatus.dockerHost }}</span>
            </div>
            <div class="flex items-center justify-between gap-3">
              <span class="text-xs text-slate-400 font-mono">Jobs root</span>
              <span class="text-[10px] font-mono text-slate-500">{{ sandboxStatus.jobsRoot }}</span>
            </div>
            <div class="text-[10px] font-mono text-slate-500 bg-slate-900/60 border border-slate-800 rounded-lg px-3 py-2 break-all">
              {{ sandboxStatus.detail }}
            </div>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-2 gap-4 pt-1">
            <div class="space-y-1.5">
              <label class="text-xs text-slate-400 font-mono">Docker Host</label>
              <input
                :value="config.sandboxDockerHost"
                @input="updateField('sandboxDockerHost', ($event.target as HTMLInputElement).value)"
                type="text"
                class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-rose-500 text-slate-200"
                placeholder="tcp://sandbox-runner:2375"
              />
              <p class="text-[9px] text-slate-600">Empty = host daemon socket. In the dev stack point this at the nested sandbox-runner daemon.</p>
            </div>

            <div class="space-y-1.5">
              <label class="text-xs text-slate-400 font-mono">Jobs Root</label>
              <input
                :value="config.sandboxJobsRoot"
                @input="updateField('sandboxJobsRoot', ($event.target as HTMLInputElement).value)"
                type="text"
                class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-rose-500 text-slate-200"
                placeholder="/jobs"
              />
              <p class="text-[9px] text-slate-600">Directory on the runner where untrusted project code lives.</p>
            </div>
          </div>
        </div>
      </div>

      <!-- Action Footer -->
      <div class="pt-4 border-t border-slate-800 flex justify-end">
        <button
          type="button"
          @click="emit('saveConfig')"
          class="bg-indigo-600 hover:bg-indigo-700 text-white font-semibold text-xs px-6 py-3 rounded-xl transition-all shadow-md shadow-indigo-600/10"
        >
          Apply & Save Configurations
        </button>
      </div>
    </div>
  </div>
</template>
