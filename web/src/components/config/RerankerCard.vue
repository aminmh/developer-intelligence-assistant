<script setup lang="ts">
import { Sparkles } from 'lucide-vue-next';
import { RAGConfig } from '../../types';

const { config, updateField } = defineProps<{
  config: RAGConfig;
  updateField: <K extends keyof RAGConfig>(key: K, value: RAGConfig[K]) => void;
}>();
</script>

<template>
  <div class="space-y-3">
    <div class="flex items-center gap-2">
      <span class="w-1 h-3.5 bg-violet-500 rounded-full"></span>
      <h4 class="text-[11px] font-bold text-slate-400 uppercase tracking-widest">Reranking</h4>
    </div>
    <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4">
      <div class="flex items-start justify-between gap-4">
        <div>
          <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2">
            <Sparkles class="w-4 h-4 text-violet-400" /> Local cross-encoder
          </h4>
          <p class="text-[10px] text-slate-500 mt-2 max-w-xl">
            Reorders vector, graph, and project-knowledge candidates using a local relevance model.
            The model is loaded only when reranking is enabled and first used.
          </p>
        </div>
        <button
          type="button"
          role="switch"
          :aria-checked="config.rerankerEnabled"
          @click="updateField('rerankerEnabled', !config.rerankerEnabled)"
          :class="config.rerankerEnabled ? 'bg-violet-500' : 'bg-slate-700'"
          class="relative w-9 h-5 rounded-full transition-colors shrink-0"
        >
          <span :class="config.rerankerEnabled ? 'left-[18px]' : 'left-0.5'" class="absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all" />
        </button>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
        <label class="space-y-1.5">
          <span class="text-xs text-slate-400 font-mono">Model</span>
          <input :value="config.rerankerModel" @input="updateField('rerankerModel', ($event.target as HTMLInputElement).value)" type="text" class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-3 py-2.5 text-xs font-mono text-slate-200 focus:outline-none focus:border-violet-500" />
        </label>
        <label class="space-y-1.5">
          <span class="text-xs text-slate-400 font-mono">Max candidates</span>
          <input :value="config.rerankerMaxCandidates" @input="updateField('rerankerMaxCandidates', Number(($event.target as HTMLInputElement).value))" type="number" min="1" max="200" class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-3 py-2.5 text-xs font-mono text-slate-200 focus:outline-none focus:border-violet-500" />
        </label>
        <label class="space-y-1.5">
          <span class="text-xs text-slate-400 font-mono">Timeout (seconds)</span>
          <input :value="config.rerankerTimeoutS" @input="updateField('rerankerTimeoutS', Number(($event.target as HTMLInputElement).value))" type="number" min="1" max="120" class="w-full bg-[#11162d] border border-slate-800 rounded-xl px-3 py-2.5 text-xs font-mono text-slate-200 focus:outline-none focus:border-violet-500" />
        </label>
      </div>
    </div>
  </div>
</template>
