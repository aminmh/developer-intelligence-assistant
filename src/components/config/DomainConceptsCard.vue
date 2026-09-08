<script setup lang="ts">
import { Network } from 'lucide-vue-next';
import { RAGConfig } from '../../types';

const { config, updateField } = defineProps<{
  config: RAGConfig;
  updateField: <K extends keyof RAGConfig>(key: K, value: RAGConfig[K]) => void;
}>();
</script>

<template>
  <!-- ============ Domain Concepts ============ -->
  <div class="space-y-3">
    <div class="flex items-center gap-2">
      <span class="w-1 h-3.5 bg-cyan-500 rounded-full"></span>
      <h4 class="text-[11px] font-bold text-slate-400 uppercase tracking-widest">Domain Concepts</h4>
    </div>

    <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4 max-w-2xl">
      <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2 border-b border-slate-800 pb-3">
        <Network class="w-4 h-4 text-cyan-400" />
        Domain Concepts
      </h4>

      <p class="text-[11px] text-slate-400 leading-relaxed">
        Recognise the project's domain concepts — classes, routes, events — and index them into
        the canonical store so retrieval can match real code-level entities against answers.
      </p>

      <div class="flex items-center justify-between gap-3">
        <span class="text-xs text-slate-400 font-mono">Enable domain-concept indexing</span>
        <button
          type="button"
          role="switch"
          :aria-checked="!!config.domainConceptsEnabled"
          @click="updateField('domainConceptsEnabled', !config.domainConceptsEnabled)"
          class="relative w-9 h-5 rounded-full transition-colors shrink-0"
          :class="config.domainConceptsEnabled ? 'bg-cyan-500' : 'bg-slate-700'"
        >
          <span
            class="absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all"
            :class="config.domainConceptsEnabled ? 'left-[18px]' : 'left-0.5'"
          ></span>
        </button>
      </div>
    </div>
  </div>
</template>
