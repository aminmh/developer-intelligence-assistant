<script setup lang="ts">
import { Cpu, CheckCircle2, AlertTriangle, Loader2 } from 'lucide-vue-next';

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
    <div class="flex items-center gap-2 mb-4">
      <Cpu class="w-4 h-4 text-indigo-400 shrink-0" />
      <span class="text-xs font-semibold text-slate-200 font-mono">Processing {{ fileName }}</span>
    </div>

    <!-- Steps -->
    <div class="space-y-2">
      <div
        v-for="step in steps"
        :key="step.id"
        class="flex flex-col"
      >
        <div class="flex items-center gap-2.5">
          <!-- Status icon -->
          <div class="shrink-0 w-5 h-5 flex items-center justify-center">
            <CheckCircle2
              v-if="step.status === 'done'"
              class="w-4 h-4 text-emerald-400 transition-all duration-300"
            />
            <Loader2
              v-else-if="step.status === 'running'"
              class="w-4 h-4 text-indigo-400 animate-spin"
            />
            <AlertTriangle
              v-else-if="step.status === 'error'"
              class="w-4 h-4 text-red-400"
            />
            <div
              v-else
              class="w-2 h-2 rounded-full bg-slate-600"
            />
          </div>

          <!-- Label + detail -->
          <div class="flex-1 min-w-0 flex items-center justify-between gap-2">
            <span
              class="text-[11px] font-mono truncate"
              :class="{
                'text-slate-400': step.status === 'pending',
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

        <!-- Progress bar (only when running with progress data) -->
        <div
          v-if="step.status === 'running' && step.progress && step.progress.total > 0"
          class="ml-7 mt-1.5"
        >
          <div class="flex items-center justify-between text-[9px] text-slate-500 mb-1">
            <span class="font-mono">{{ step.progress.current }}/{{ step.progress.total }}</span>
            <span class="font-mono tabular-nums">{{ step.progress.percent }}%</span>
          </div>
          <div class="h-1 bg-slate-800 rounded-full overflow-hidden">
            <div
              class="h-full bg-indigo-500 rounded-full transition-all duration-700 ease-out"
              :style="{ width: `${Math.min(step.progress.percent, 100)}%` }"
            />
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
