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
});

onUnmounted(stopImportPolling);
</script>

<template>
  <div class="flex-1 overflow-y-auto p-6 space-y-6 text-left">
    <div class="max-w-4xl mx-auto space-y-6">
      
      <div class="bg-slate-900/40 rounded-2xl border border-slate-800/80 p-6">
        <h3 class="text-lg font-bold text-white flex items-center gap-2">
          <Sliders class="w-5 h-5 text-indigo-400" />
          RAG Pipeline Configuration
        </h3>
        <p class="text-xs text-slate-400 mt-1">Configure Ollama models and Qdrant connection details.</p>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
        
        <!-- Column 1: Ollama connection + models -->
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
              <div class="space-y-1.5">
                <label class="text-xs text-slate-400 font-mono">LLM (Chat) Model</label>
                <select
                  :value="config.ollamaLlmModel"
                  @change="updateField('ollamaLlmModel', ($event.target as HTMLSelectElement).value)"
                  class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-emerald-500 text-slate-200"
                >
                  <option v-for="m in llmModelOptions" :key="m" :value="m">{{ m }}</option>
                </select>
              </div>

              <div class="space-y-1.5">
                <label class="text-xs text-slate-400 font-mono">Embedding Model</label>
                <select
                  :value="config.ollamaEmbedModel"
                  @change="updateField('ollamaEmbedModel', ($event.target as HTMLSelectElement).value)"
                  class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-emerald-500 text-slate-200"
                >
                  <option v-for="m in embedModelOptions" :key="m" :value="m">{{ m }}</option>
                </select>
              </div>

              <div class="space-y-1.5">
                <div class="flex items-center justify-between">
                  <label class="text-xs text-slate-400 font-mono">Evaluation Judge Model</label>
                  <span class="text-[9px] text-slate-500">RAGAS</span>
                </div>
                <select
                  :value="config.ollamaEvalModel"
                  @change="updateField('ollamaEvalModel', ($event.target as HTMLSelectElement).value)"
                  class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-emerald-500 text-slate-200"
                >
                  <option v-for="m in evalModelOptions" :key="m" :value="m">{{ m }}</option>
                </select>
                <p class="text-[9px] text-slate-600">Used only to score answers; it does not generate chat responses.</p>
              </div>
            </div>
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
              <label class="text-xs text-slate-400 font-mono">Ollama model name</label>
              <input
                v-model="importModelName"
                type="text"
                class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-4 py-2.5 text-xs font-mono focus:outline-none focus:border-indigo-500 text-slate-200"
                placeholder="e.g. my-coder-model"
              />
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
              class="w-full flex items-center justify-center gap-2 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed text-white font-semibold text-xs px-4 py-2.5 rounded-xl transition-all"
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

        <!-- Column 2: Qdrant + sampling params -->
        <div class="space-y-6">

          <!-- Qdrant -->
          <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4">
            <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2 border-b border-slate-800 pb-3">
              <Database class="w-4 h-4 text-indigo-400" />
              Qdrant Vector Database
            </h4>

            <div class="space-y-3">
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
