<script setup lang="ts">
import { CheckCircle2, AlertTriangle, Loader2, FileSearch } from 'lucide-vue-next';

export interface PipelineStep {
  id: string;
  label: string;
  status: 'pending' | 'running' | 'done' | 'error';
  detail?: string | null;
  progress?: { current: number; total: number; percent: number } | null;
}

defineProps<{
  fileName: string;
  steps: PipelineStep[];
}>();
</script>

<template>
  <div class="rounded-2xl px-5 py-4 max-w-3xl bg-slate-900/60 border border-slate-800 rounded-tl-none">
    <!-- Header -->
    <div class="flex items-center gap-2.5 mb-4">
      <div class="relative shrink-0 w-7 h-7 flex items-center justify-center">
        <span class="absolute inset-0 rounded-full bg-indigo-500/20 animate-ping"></span>
        <span class="relative w-7 h-7 rounded-full bg-indigo-600/20 border border-indigo-500/30 flex items-center justify-center">
          <FileSearch class="w-3.5 h-3.5 text-indigo-400" />
        </span>
      </div>
      <span class="text-xs font-semibold text-slate-200 font-mono truncate">Processing {{ fileName }}</span>
    </div>

    <!-- Timeline steps -->
    <div class="relative space-y-1.5">
      <div
        v-for="(step, i) in steps"
        :key="step.id"
        class="relative flex items-center gap-3 py-1.5"
      >
        <!-- Connector line -->
        <div
          v-if="i < steps.length - 1"
          class="absolute left-[9px] top-7 bottom-[-6px] w-px"
          :class="step.status === 'done' ? 'bg-emerald-500/40' : 'bg-slate-700/50'"
        ></div>

        <!-- Status icon -->
        <div class="relative shrink-0 w-[18px] h-[18px] flex items-center justify-center z-10">
          <CheckCircle2
            v-if="step.status === 'done'"
            class="w-[18px] h-[18px] text-emerald-400 transition-all duration-300"
          />
          <span v-else-if="step.status === 'running'" class="relative flex w-[18px] h-[18px] items-center justify-center">
            <span class="absolute inline-flex w-full h-full rounded-full bg-indigo-500/30 animate-ping"></span>
            <Loader2 class="relative w-4 h-4 text-indigo-400 animate-spin" />
          </span>
          <AlertTriangle
            v-else-if="step.status === 'error'"
            class="w-[18px] h-[18px] text-red-400"
          />
          <span v-else class="w-[9px] h-[9px] rounded-full bg-slate-600"></span>
        </div>

        <!-- Label + detail -->
        <div class="flex-1 min-w-0 flex items-center justify-between gap-2">
          <span
            class="text-[11px] font-mono truncate transition-colors duration-300"
            :class="{
              'text-slate-500': step.status === 'pending',
              'text-indigo-300': step.status === 'running',
              'text-emerald-400': step.status === 'done',
              'text-red-400': step.status === 'error',
            }"
          >{{ step.label }}</span>
          <span
            v-if="step.detail"
            class="text-[10px] text-slate-500 font-mono shrink-0"
          >{{ step.detail }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
