<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { FileCode, X, Loader2 } from 'lucide-vue-next';
import type { CitationPreview } from '../types';

const props = defineProps<{ preview: CitationPreview }>();
const emit = defineEmits<{ (e: 'close'): void }>();

const closeButton = ref<HTMLButtonElement | null>(null);
const highlighted = ref<HTMLElement | null>(null);

// The API returns `N: text` lines; keep numbers and text separate for display.
const lines = computed(() =>
  (props.preview.content ?? '').split('\n').filter((l) => l !== '').map((raw) => {
    const match = /^(\d+): ?(.*)$/.exec(raw);
    return match ? { number: Number(match[1]), text: match[2] } : { number: 0, text: raw };
  }),
);

const onKey = (event: KeyboardEvent) => {
  if (event.key === 'Escape') emit('close');
};

watch(
  () => props.preview.loading,
  async (loading) => {
    if (!loading) {
      await nextTick();
      highlighted.value?.scrollIntoView({ block: 'center' });
    }
  },
  { immediate: true },
);

onMounted(() => {
  document.addEventListener('keydown', onKey);
  closeButton.value?.focus();
});
onBeforeUnmount(() => document.removeEventListener('keydown', onKey));
</script>

<template>
  <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" @click.self="emit('close')">
    <div
      role="dialog"
      aria-modal="true"
      :aria-label="`Source ${preview.path}:${preview.line}`"
      class="w-full max-w-3xl max-h-[80vh] flex flex-col rounded-xl border border-slate-700 bg-[#0b1020] shadow-2xl"
    >
      <div class="flex items-center gap-2 px-4 py-2.5 border-b border-slate-800">
        <FileCode class="w-4 h-4 text-cyan-400 shrink-0" />
        <span class="font-mono text-xs text-slate-200 truncate">{{ preview.path }}<span class="text-cyan-300">:{{ preview.line }}</span></span>
        <span v-if="preview.totalLines" class="font-mono text-[10px] text-slate-500 shrink-0">{{ preview.totalLines }} lines</span>
        <button ref="closeButton" type="button" class="ml-auto p-1 rounded text-slate-400 hover:text-white" aria-label="Close" @click="emit('close')">
          <X class="w-4 h-4" />
        </button>
      </div>
      <div class="overflow-auto">
        <div v-if="preview.loading" class="flex items-center gap-2 p-4 text-xs text-slate-400 font-mono">
          <Loader2 class="w-3.5 h-3.5 animate-spin" /> Loading source…
        </div>
        <div v-else-if="preview.error" class="p-4 text-xs text-rose-300 font-mono">{{ preview.error }}</div>
        <pre v-else class="py-2 text-[12px] leading-5 font-mono text-slate-300"><div
          v-for="line in lines"
          :key="line.number"
          :ref="(el) => { if (line.number === preview.line) highlighted = el as HTMLElement }"
          class="flex px-4"
          :class="line.number === preview.line ? 'bg-cyan-400/10 border-l-2 border-cyan-400' : 'border-l-2 border-transparent'"
        ><span class="w-12 shrink-0 pr-3 text-right select-none text-slate-600">{{ line.number }}</span><span class="whitespace-pre">{{ line.text }}</span></div></pre>
      </div>
    </div>
  </div>
</template>
