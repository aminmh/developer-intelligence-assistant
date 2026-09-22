<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue';

const TYPE_DELAY_MS = 28;
const PAUSE_DELAY_MS = 1800;
const ERASE_DELAY_MS = 12;
const RESTART_DELAY_MS = 700;

const terminalContent = `$ developer-assistant --help

Developer Assistant
Codebase intelligence for understanding, searching, and explaining projects.

[ok] Upload documents and project ZIPs
[ok] Search indexed code with semantic RAG
[ok] Analyze Laravel structure, routes, models, and migrations
[ok] Explore schema, domain knowledge, definitions, and logs
[ok] Inspect project structure and runtime behavior
[ok] Tune configuration and model controls

status: ready`;

const displayedText = ref('');
const reducedMotion = ref(false);

defineProps<{
  generatingAnswer: boolean;
}>();

let animationTimer: ReturnType<typeof setTimeout> | null = null;
let mediaQuery: MediaQueryList | null = null;

const clearAnimationTimer = () => {
  if (animationTimer) {
    clearTimeout(animationTimer);
    animationTimer = null;
  }
};

const setStaticContent = () => {
  clearAnimationTimer();
  displayedText.value = terminalContent;
};

const schedule = (callback: () => void, delay: number) => {
  clearAnimationTimer();
  animationTimer = setTimeout(() => {
    animationTimer = null;
    callback();
  }, delay);
};

const startAnimation = () => {
  if (reducedMotion.value) {
    setStaticContent();
    return;
  }

  const type = (index: number) => {
    if (reducedMotion.value) {
      setStaticContent();
      return;
    }

    displayedText.value = terminalContent.slice(0, index);
    if (index < terminalContent.length) {
      schedule(() => type(index + 1), TYPE_DELAY_MS);
    } else {
      schedule(() => erase(terminalContent.length), PAUSE_DELAY_MS);
    }
  };

  const erase = (index: number) => {
    if (reducedMotion.value) {
      setStaticContent();
      return;
    }

    displayedText.value = terminalContent.slice(0, index);
    if (index > 0) {
      schedule(() => erase(index - 1), ERASE_DELAY_MS);
    } else {
      schedule(() => type(1), RESTART_DELAY_MS);
    }
  };

  displayedText.value = '';
  schedule(() => type(1), TYPE_DELAY_MS);
};

const handleMotionPreferenceChange = (event: MediaQueryListEvent) => {
  reducedMotion.value = event.matches;
  if (reducedMotion.value) {
    setStaticContent();
  } else {
    startAnimation();
  }
};

onMounted(() => {
  mediaQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
  reducedMotion.value = mediaQuery.matches;
  mediaQuery.addEventListener('change', handleMotionPreferenceChange);
  startAnimation();
});

onUnmounted(() => {
  clearAnimationTimer();
  mediaQuery?.removeEventListener('change', handleMotionPreferenceChange);
  mediaQuery = null;
});
</script>

<template>
  <section
    class="w-full overflow-hidden rounded-2xl border border-slate-800 bg-[#080b14] shadow-lg shadow-slate-950/20 transition-opacity duration-500 ease-in-out"
    :class="generatingAnswer ? 'opacity-0' : 'opacity-100'"
    aria-label="Developer Assistant terminal welcome"
  >
    <div class="flex items-center gap-2 border-b border-slate-800 bg-slate-900/50 px-4 py-2.5">
      <span class="h-2.5 w-2.5 rounded-full bg-rose-400/80"></span>
      <span class="h-2.5 w-2.5 rounded-full bg-amber-400/80"></span>
      <span class="h-2.5 w-2.5 rounded-full bg-emerald-400/80"></span>
      <span class="ml-2 text-[10px] font-mono uppercase tracking-wider text-slate-500">developer-assistant</span>
      <span class="ml-auto flex items-center gap-1.5 text-[10px] font-mono text-emerald-400/80">
        <span class="h-1.5 w-1.5 rounded-full bg-emerald-400" :class="reducedMotion ? '' : 'animate-pulse'"></span>
        ready
      </span>
    </div>

    <div class="relative p-3 text-left text-[11px] leading-5 text-slate-300 sm:p-4 sm:text-xs">
      <pre class="invisible m-0 whitespace-pre-wrap break-words font-mono" aria-hidden="true">{{ terminalContent }}</pre>
      <pre class="absolute inset-3 m-0 whitespace-pre-wrap break-words font-mono sm:inset-4" aria-hidden="true">{{ displayedText }}<span class="text-emerald-400" :class="reducedMotion ? '' : 'animate-pulse'">▋</span></pre>
    </div>
  </section>
</template>
