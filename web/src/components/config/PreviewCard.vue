<script setup lang="ts">
import { Eye } from 'lucide-vue-next';
import { RAGConfig } from '../../types';

const { config, updateField } = defineProps<{
  config: RAGConfig;
  updateField: <K extends keyof RAGConfig>(key: K, value: RAGConfig[K]) => void;
}>();

type PreviewMode = RAGConfig['contextPreviewMode'];

const MODE_OPTIONS: { value: PreviewMode; label: string; detail: string }[] = [
  {
    value: 'off',
    label: 'Off (default)',
    detail: 'Answers run as before — no context window is shown.',
  },
  {
    value: 'inline',
    label: 'Inline — attach preview with each answer',
    detail: 'Each answer carries a collapsible context window.',
  },
  {
    value: 'gate',
    label: 'Gate — review preview before the model runs',
    detail: 'The context window is shown and confirmed before generation.',
  },
];

const MODE_HINTS: Record<PreviewMode, string> = {
  off: 'Nothing changes in chat: the model answers immediately and no context window is attached.',
  inline: 'After each answer you can expand “Context window sent to model” to inspect the exact markdown the model saw.',
  gate: 'Chat will show the context window and ask you to confirm before generating.',
};

const selectMode = (mode: PreviewMode) => {
  updateField('contextPreviewMode', mode);
};
</script>

<template>
  <!-- ============ Context Preview ============ -->
  <div class="space-y-3">
    <div class="flex items-center gap-2">
      <span class="w-1 h-3.5 bg-violet-500 rounded-full"></span>
      <h4 class="text-[11px] font-bold text-slate-400 uppercase tracking-widest">Context Preview</h4>
    </div>

    <div class="bg-slate-900/30 border border-slate-800 rounded-2xl p-6 space-y-4 max-w-2xl">
      <h4 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2 border-b border-slate-800 pb-3">
        <Eye class="w-4 h-4 text-violet-400" />
        Context Preview
      </h4>

      <p class="text-[11px] text-slate-400 leading-relaxed">
        Show the exact markdown context window (system prompt + retrieved context)
        that is sent to the model — inspect before or alongside the answer.
      </p>

      <div class="space-y-2">
        <button
          v-for="opt in MODE_OPTIONS"
          :key="opt.value"
          type="button"
          @click="selectMode(opt.value)"
          class="w-full flex items-center justify-between gap-3 border rounded-xl px-3.5 py-2.5 text-left transition-all"
          :class="config.contextPreviewMode === opt.value
            ? 'border-violet-500/50 bg-violet-500/10'
            : 'border-slate-800 bg-slate-900/40 hover:border-slate-600'"
          :aria-pressed="config.contextPreviewMode === opt.value"
        >
          <span>
            <span class="block text-xs text-slate-200 font-mono" :class="config.contextPreviewMode === opt.value ? 'text-violet-200' : ''">
              {{ opt.label }}
            </span>
            <span class="block text-[9px] text-slate-500 mt-0.5">{{ opt.detail }}</span>
          </span>
          <span
            class="shrink-0 w-3.5 h-3.5 rounded-full border transition-all"
            :class="config.contextPreviewMode === opt.value
              ? 'border-violet-400 bg-violet-500'
              : 'border-slate-600'"
          ></span>
        </button>
      </div>

      <p
        class="text-[10px] font-mono rounded-lg border px-3 py-2 leading-relaxed"
        :class="config.contextPreviewMode === 'gate'
          ? 'border-amber-500/25 bg-amber-500/5 text-amber-300/90'
          : config.contextPreviewMode === 'inline'
            ? 'border-violet-500/25 bg-violet-500/5 text-violet-300/90'
            : 'border-slate-800 bg-slate-900/60 text-slate-500'"
      >
        {{ MODE_HINTS[config.contextPreviewMode] }}
      </p>
    </div>
  </div>
</template>
