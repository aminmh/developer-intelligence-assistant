<script setup lang="ts">
import { Sparkles } from 'lucide-vue-next';

defineProps<{
  label?: string;
}>();
</script>

<template>
  <div class="flex items-center gap-3 rounded-2xl px-5 py-4 max-w-3xl bg-slate-900/60 border border-slate-800 rounded-tl-none">
    <div class="relative shrink-0 w-8 h-8 flex items-center justify-center">
      <span class="absolute inset-0 rounded-full bg-indigo-500/20 animate-ping"></span>
      <span class="relative w-8 h-8 rounded-full bg-indigo-600/20 border border-indigo-500/30 flex items-center justify-center">
        <Sparkles class="w-4 h-4 text-indigo-400" />
      </span>
    </div>
    <div class="flex items-center gap-2 font-mono text-[13px] text-indigo-200">
      <span>{{ label || 'Working' }}</span>
      <span class="inline-flex items-end gap-1 h-3">
        <span class="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-bounce" style="animation-delay: 0ms"></span>
        <span class="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-bounce" style="animation-delay: 150ms"></span>
        <span class="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-bounce" style="animation-delay: 300ms"></span>
      </span>
    </div>
  </div>
</template>
