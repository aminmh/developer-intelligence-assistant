<script setup lang="ts">
import { Database, Layers3, Palette, Server, Sliders } from 'lucide-vue-next';
import { ref } from 'vue';
import type { AppearancePreferences, RAGConfig } from '../types';
import AppearanceCard from './config/AppearanceCard.vue';
import LlmProvidersCard from './config/LlmProvidersCard.vue';
import GpuOffloadCard from './config/GpuOffloadCard.vue';
import ExternalModelCard from './config/ExternalModelCard.vue';
import VectorStoreCard from './config/VectorStoreCard.vue';
import ModelImportCard from './config/ModelImportCard.vue';
import DocRetrievalCard from './config/DocRetrievalCard.vue';
import DomainConceptsCard from './config/DomainConceptsCard.vue';
import PreviewCard from './config/PreviewCard.vue';
import SandboxRunnerCard from './config/SandboxRunnerCard.vue';
import RerankerCard from './config/RerankerCard.vue';

const props = defineProps<{
  config: RAGConfig;
  appearance: AppearancePreferences;
}>();

const activeSection = ref<'appearance' | 'models' | 'retrieval' | 'sandbox'>('appearance');

const emit = defineEmits<{
  (e: 'saveConfig'): void;
  (e: 'updateAppearance', key: keyof AppearancePreferences, value: AppearancePreferences[keyof AppearancePreferences]): void;
}>();

// Shared field setter passed to every card — cards mutate the parent-owned
// reactive config object in place, exactly like the old single-file version.
const updateField = <K extends keyof RAGConfig>(key: K, value: RAGConfig[K]) => {
  props.config[key] = value;
};

const updateAppearance = <K extends keyof AppearancePreferences>(
  key: K,
  value: AppearancePreferences[K],
) => {
  emit('updateAppearance', key, value);
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

      <div class="grid grid-cols-2 lg:grid-cols-4 gap-2 rounded-2xl border border-slate-800 bg-slate-900/30 p-2">
        <button
          type="button"
          class="flex items-center justify-center gap-2 rounded-xl px-3 py-2.5 text-xs font-semibold transition-all"
          :class="activeSection === 'appearance' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:bg-slate-800/60 hover:text-white'"
          @click="activeSection = 'appearance'"
        >
          <Palette class="w-4 h-4" />
          Appearance
        </button>
        <button
          type="button"
          class="flex items-center justify-center gap-2 rounded-xl px-3 py-2.5 text-xs font-semibold transition-all"
          :class="activeSection === 'models' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:bg-slate-800/60 hover:text-white'"
          @click="activeSection = 'models'"
        >
          <Layers3 class="w-4 h-4" />
          Models & Providers
        </button>
        <button
          type="button"
          class="flex items-center justify-center gap-2 rounded-xl px-3 py-2.5 text-xs font-semibold transition-all"
          :class="activeSection === 'retrieval' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:bg-slate-800/60 hover:text-white'"
          @click="activeSection = 'retrieval'"
        >
          <Database class="w-4 h-4" />
          Retrieval & Context
        </button>
        <button
          type="button"
          class="flex items-center justify-center gap-2 rounded-xl px-3 py-2.5 text-xs font-semibold transition-all"
          :class="activeSection === 'sandbox' ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/20' : 'text-slate-400 hover:bg-slate-800/60 hover:text-white'"
          @click="activeSection = 'sandbox'"
        >
          <Server class="w-4 h-4" />
          Sandbox
        </button>
      </div>

      <AppearanceCard
        v-if="activeSection === 'appearance'"
        :appearance="appearance"
        @update="updateAppearance"
      />

      <template v-else-if="activeSection === 'models'">
        <LlmProvidersCard :config="config" :update-field="updateField" />
        <GpuOffloadCard :config="config" :update-field="updateField" />
        <ExternalModelCard :config="config" :update-field="updateField" />
        <ModelImportCard :config="config" :update-field="updateField" />
      </template>

      <template v-else-if="activeSection === 'retrieval'">
        <VectorStoreCard :config="config" :update-field="updateField" />
        <DocRetrievalCard :config="config" :update-field="updateField" />
        <DomainConceptsCard :config="config" :update-field="updateField" />
        <RerankerCard :config="config" :update-field="updateField" />
        <PreviewCard :config="config" :update-field="updateField" />
      </template>

      <SandboxRunnerCard v-else :config="config" :update-field="updateField" />

      <!-- Action Footer -->
      <div class="pt-4 border-t border-slate-800 flex justify-start">
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
