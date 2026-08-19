<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { 
  Sliders, 
  Cpu, 
  Database,
  SlidersHorizontal,
  RefreshCw,
  Server,
  Scissors,
} from 'lucide-vue-next';
import { RAGConfig } from '../types';

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

onMounted(fetchModels);
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
            </div>
          </div>
        </div>

        <!-- Column 2: chunking + Qdrant + sampling params -->
        <div class="space-y-6">

          <!-- Document chunking -->
          <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-5">
            <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2 border-b border-slate-800 pb-3">
              <Scissors class="w-4 h-4 text-indigo-400" />
              Document Chunking
            </h4>

            <div class="space-y-4">
              <!-- Chunk Size -->
              <div class="space-y-2">
                <div class="flex items-center justify-between text-xs">
                  <span class="text-slate-400">Chunk Size (characters)</span>
                  <span class="font-mono text-indigo-400 bg-indigo-500/10 px-2 py-0.5 rounded font-bold">{{ config.chunkSize }}</span>
                </div>
                <input 
                  type="range" 
                  min="100" 
                  max="2000" 
                  step="50" 
                  :value="config.chunkSize"
                  @input="updateField('chunkSize', parseInt(($event.target as HTMLInputElement).value))"
                  class="w-full h-1 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-indigo-500" 
                />
              </div>

              <!-- Chunk Overlap -->
              <div class="space-y-2">
                <div class="flex items-center justify-between text-xs">
                  <span class="text-slate-400">Chunk Overlap</span>
                  <span class="font-mono text-indigo-400 bg-indigo-500/10 px-2 py-0.5 rounded font-bold">{{ config.chunkOverlap }}</span>
                </div>
                <input 
                  type="range" 
                  min="0" 
                  max="400" 
                  step="10" 
                  :value="config.chunkOverlap"
                  @input="updateField('chunkOverlap', parseInt(($event.target as HTMLInputElement).value))"
                  class="w-full h-1 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-indigo-500" 
                />
              </div>
            </div>
          </div>

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
