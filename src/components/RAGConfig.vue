<script setup lang="ts">
import { Sliders } from 'lucide-vue-next';
import { RAGConfig } from '../types';
import LlmProvidersCard from './config/LlmProvidersCard.vue';
import GpuOffloadCard from './config/GpuOffloadCard.vue';
import ExternalModelCard from './config/ExternalModelCard.vue';
import VectorStoreCard from './config/VectorStoreCard.vue';
import ModelImportCard from './config/ModelImportCard.vue';
import DocRetrievalCard from './config/DocRetrievalCard.vue';
import SandboxRunnerCard from './config/SandboxRunnerCard.vue';

const props = defineProps<{
  config: RAGConfig;
}>();

const emit = defineEmits<{
  (e: 'saveConfig'): void;
}>();

// Shared field setter passed to every card — cards mutate the parent-owned
// reactive config object in place, exactly like the old single-file version.
const updateField = <K extends keyof RAGConfig>(key: K, value: RAGConfig[K]) => {
  props.config[key] = value;
};
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

      <LlmProvidersCard :config="config" :update-field="updateField" />
      <GpuOffloadCard :config="config" :update-field="updateField" />
      <ExternalModelCard :config="config" :update-field="updateField" />
      <VectorStoreCard :config="config" :update-field="updateField" />
      <ModelImportCard :config="config" :update-field="updateField" />
      <DocRetrievalCard :config="config" :update-field="updateField" />
      <SandboxRunnerCard :config="config" :update-field="updateField" />

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
