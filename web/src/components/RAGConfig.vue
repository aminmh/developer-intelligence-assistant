<script setup lang="ts">
import { computed } from 'vue';
import { Activity, Check, Database, KeyRound, Layers3, Palette, Save, Wrench } from 'lucide-vue-next';
import type { AppearancePreferences, RAGConfig } from '../types';
import { useUrlTab } from '../composables/useUrlTab';
import AppearanceCard from './config/AppearanceCard.vue';
import LlmProvidersCard from './config/LlmProvidersCard.vue';
import ExternalModelCard from './config/ExternalModelCard.vue';
import VectorStoreCard from './config/VectorStoreCard.vue';
import DocRetrievalCard from './config/DocRetrievalCard.vue';
import DomainConceptsCard from './config/DomainConceptsCard.vue';
import PreviewCard from './config/PreviewCard.vue';
import RerankerCard from './config/RerankerCard.vue';
import LlmToolsCard from './config/LlmToolsCard.vue';
import RuntimeTelemetryCard from './config/RuntimeTelemetryCard.vue';
import IdeTokensCard from './config/IdeTokensCard.vue';

const props = defineProps<{
  config: RAGConfig;
  appearance: AppearancePreferences;
}>();

type ConfigSection = 'appearance' | 'models' | 'retrieval' | 'tools' | 'runtime' | 'ide';
const activeSection = useUrlTab<ConfigSection>({ values: ['appearance', 'models', 'retrieval', 'tools', 'runtime', 'ide'], fallback: 'appearance' });

const emit = defineEmits<{
  (e: 'saveConfig'): void;
  (e: 'updateAppearance', key: keyof AppearancePreferences, value: AppearancePreferences[keyof AppearancePreferences]): void;
}>();

const sections: Array<{ value: ConfigSection; label: string; description: string; summary: string; icon: typeof Palette }> = [
  { value: 'appearance', label: 'Appearance', description: 'Theme, colours, fonts, panels', summary: 'Changes apply instantly and are saved in this browser only.', icon: Palette },
  { value: 'models', label: 'Models and providers', description: 'Chat, embedding and external models', summary: 'Local providers, sampling, and the external OpenAI-compatible model.', icon: Layers3 },
  { value: 'retrieval', label: 'Retrieval and context', description: 'Vector store, reranker, preview', summary: 'Where code is stored and how context is found, ranked and previewed.', icon: Database },
  { value: 'tools', label: 'LLM tools', description: 'Tool access and approvals', summary: '', icon: Wrench },
  { value: 'runtime', label: 'Runtime telemetry', description: 'ClickHouse and collectors', summary: '', icon: Activity },
  { value: 'ide', label: 'IDE extension', description: 'Tokens for API change alerts', summary: '', icon: KeyRound },
];

const current = computed(() => sections.find((section) => section.value === activeSection.value) ?? sections[0]);
// Only these sections edit the server config that the page-level save writes.
const needsSave = computed(() => activeSection.value === 'models' || activeSection.value === 'retrieval');

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
  <div class="st-page">
    <nav class="st-nav" aria-label="Settings sections">
      <p class="st-nav-title">settings</p>
      <button
        v-for="section in sections"
        :key="section.value"
        type="button"
        class="st-nav-item"
        :title="section.label"
        :aria-label="section.label"
        :aria-current="activeSection === section.value ? 'page' : undefined"
        @click="activeSection = section.value"
      >
        <span class="st-nav-icon"><component :is="section.icon" /></span>
        <span class="st-nav-text">
          <b>{{ section.label }}</b>
          <span>{{ section.description }}</span>
        </span>
      </button>
    </nav>

    <div class="st-content">
      <Transition name="app-panel" mode="out-in">
        <div :key="activeSection" class="st-section">
          <header class="st-head">
            <h2><component :is="current.icon" />{{ current.label }}</h2>
            <p v-if="current.summary">{{ current.summary }}</p>
          </header>

          <AppearanceCard
            v-if="activeSection === 'appearance'"
            :appearance="appearance"
            @update="updateAppearance"
          />

          <div v-else-if="activeSection === 'models'" class="st-legacy">
            <LlmProvidersCard :config="config" :update-field="updateField" />
            <ExternalModelCard :config="config" :update-field="updateField" />
          </div>

          <div v-else-if="activeSection === 'retrieval'" class="st-legacy">
            <VectorStoreCard :config="config" :update-field="updateField" />
            <DocRetrievalCard :config="config" :update-field="updateField" />
            <DomainConceptsCard :config="config" :update-field="updateField" />
            <RerankerCard :config="config" :update-field="updateField" />
            <PreviewCard :config="config" :update-field="updateField" />
          </div>

          <div v-else-if="activeSection === 'tools'" class="st-legacy" data-own-head><LlmToolsCard /></div>
          <div v-else-if="activeSection === 'runtime'" class="st-legacy" data-own-head><RuntimeTelemetryCard /></div>
          <div v-else-if="activeSection === 'ide'" class="st-legacy" data-own-head><IdeTokensCard /></div>

          <div v-if="needsSave" class="st-savebar">
            <Save class="st-savebar-icon" />
            <span>Model and retrieval settings are saved to the server.</span>
            <button type="button" class="st-btn" data-variant="primary" @click="emit('saveConfig')">
              <Check />
              Apply and save
            </button>
          </div>
        </div>
      </Transition>
    </div>
  </div>
</template>
