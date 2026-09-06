<script setup lang="ts">
import { computed } from 'vue';
import { CheckCircle2, AlertTriangle, Loader2, FileSearch } from 'lucide-vue-next';

export interface PipelineStep {
  id: string;
  label: string;
  status: 'pending' | 'running' | 'done' | 'error';
  detail?: string | null;
  progress?: { current: number; total: number; percent: number } | null;
}

export interface PipelineStepProgress {
  percent: number;
  label?: string;
}

const props = defineProps<{
  fileName: string;
  steps: PipelineStep[];
  // Live per-step progress (e.g. backend steps_progress map, keyed by step id).
  // Optional: when absent the component degrades to the legacy chip timeline.
  progress?: Record<string, PipelineStepProgress>;
}>();

const clampPct = (value: number) => Math.max(0, Math.min(100, value));

// Numeric percent for a step: inline step.progress wins, then the shared map.
const percentFor = (step: PipelineStep): number | null => {
  const inline = step.progress && typeof step.progress.percent === 'number' ? step.progress.percent : null;
  const mapped = props.progress?.[step.id] && typeof props.progress[step.id].percent === 'number' ? props.progress[step.id].percent : null;
  const raw = inline ?? mapped;
  return raw == null ? null : clampPct(raw);
};

const labelFor = (step: PipelineStep): string | null => {
  const mapped = props.progress?.[step.id];
  if (mapped && mapped.label) return mapped.label;
  return step.detail ?? null;
};

// True when at least one step carries a real number we can average (a completed
// step or a running step with a reported percent). Otherwise only a shimmer is
// honest — an unknown running step has no measurable progress yet.
const hasDeterminate = computed(() =>
  props.steps.some((s) => s.status === 'done' || (s.status === 'running' && percentFor(s) != null)),
);

// Equal-weight overall mean: done = 1.0, running = its reported fraction (an
// unreported running step is a neutral 0.5), pending = 0.
const overallPercent = computed<number | null>(() => {
  const steps = props.steps;
  if (!steps.length || !hasDeterminate.value) return null;
  let acc = 0;
  for (const step of steps) {
    if (step.status === 'done') acc += 1;
    else if (step.status === 'running') {
      const p = percentFor(step);
      acc += p == null ? 0.5 : p / 100;
    }
    // pending contributes 0
  }
  return Math.round((acc / steps.length) * 100);
});

const overallLabel = computed(() => {
  const runningStep = props.steps.find((s) => s.status === 'running');
  const label = runningStep ? labelFor(runningStep) || runningStep.label : '';
  return label || 'Working…';
});

// The overall run-progress header is only meaningful when the caller actually
// supplies live per-step progress. Legacy consumers (e.g. RAGChat) pass steps
// only and keep the plain timeline — render nothing extra for them.
const hasOverallProgress = computed(() => {
  const map = props.progress;
  if (!map) return false;
  return Object.values(map).some((entry) => Number.isFinite(entry?.percent));
});

// --- template helpers (kept deterministic) ----------------------------------

const showBar = (step: PipelineStep) => step.status === 'running' || step.status === 'done';
// Only called when the step is determinate: done → 100, or a running step that
// reported a percent (isIndeterminate guards the template), so no fallback is
// needed for a missing percent here.
const barPercent = (step: PipelineStep): number => (step.status === 'done' ? 100 : percentFor(step)!);
const isIndeterminate = (step: PipelineStep) => step.status === 'running' && percentFor(step) == null;
</script>

<template>
  <div class="rounded-2xl px-5 py-4 max-w-3xl bg-slate-900/60 border border-slate-800 rounded-tl-none">
    <!-- Header -->
    <div class="flex items-center gap-2.5 mb-3">
      <div class="relative shrink-0 w-7 h-7 flex items-center justify-center">
        <span class="absolute inset-0 rounded-full bg-indigo-500/20 animate-ping"></span>
        <span class="relative w-7 h-7 rounded-full bg-indigo-600/20 border border-indigo-500/30 flex items-center justify-center">
          <FileSearch class="w-3.5 h-3.5 text-indigo-400" />
        </span>
      </div>
      <span class="text-xs font-semibold text-slate-200 font-mono truncate">Processing {{ fileName }}</span>
    </div>

    <!-- Overall run progress (equal-weight over steps) — only when the caller
         supplies live per-step progress; otherwise omit to keep the legacy
         per-step timeline behavior for step-only consumers. -->
    <div v-if="hasOverallProgress" class="mb-4">
      <div class="flex items-center justify-between gap-2 mb-1">
        <span class="text-[10px] font-mono text-slate-500 truncate">{{ overallLabel }}</span>
        <span v-if="overallPercent != null" class="text-[10px] font-mono text-slate-400 tabular-nums shrink-0">{{ overallPercent }}%</span>
        <span v-else class="text-[10px] font-mono text-slate-600 shrink-0">working…</span>
      </div>
      <div class="h-1.5 rounded-full bg-slate-800/80 overflow-hidden">
        <div
          v-if="overallPercent != null"
          class="h-full rounded-full bg-gradient-to-r from-indigo-500/80 to-violet-500/80 transition-all duration-500"
          :style="{ width: `${overallPercent}%` }"
        ></div>
        <div v-else class="h-full w-1/3 rounded-full progress-indeterminate"></div>
      </div>
    </div>

    <!-- Timeline steps -->
    <div class="relative space-y-1.5">
      <div
        v-for="(step, i) in steps"
        :key="step.id"
        class="relative flex items-start gap-3 py-1.5"
      >
        <!-- Connector line -->
        <div
          v-if="i < steps.length - 1"
          class="absolute left-[9px] top-7 bottom-[-6px] w-px"
          :class="step.status === 'done' ? 'bg-emerald-500/40' : 'bg-slate-700/50'"
        ></div>

        <!-- Status icon -->
        <div class="relative shrink-0 w-[18px] h-[18px] flex items-center justify-center z-10 mt-px">
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

        <!-- Label + detail + step bar -->
        <div class="flex-1 min-w-0">
          <div class="flex items-center justify-between gap-2">
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
              v-if="step.status === 'running' && percentFor(step) != null"
              class="text-[10px] font-mono text-indigo-300/90 tabular-nums shrink-0"
            >{{ percentFor(step) }}%</span>
            <span
              v-else-if="step.detail"
              class="text-[10px] text-slate-500 font-mono shrink-0"
            >{{ step.detail }}</span>
          </div>

          <!-- Thin per-step progress track -->
          <div
            v-if="showBar(step)"
            class="relative mt-1.5 h-1 rounded-full bg-slate-800/70 overflow-hidden"
          >
            <div
              v-if="!isIndeterminate(step)"
              class="absolute inset-y-0 left-0 rounded-full transition-all duration-500"
              :class="step.status === 'done' ? 'bg-emerald-500/60' : 'bg-gradient-to-r from-indigo-500/70 to-violet-500/70'"
              :style="{ width: `${barPercent(step)}%` }"
            ></div>
            <div v-else class="absolute inset-y-0 w-1/3 rounded-full progress-indeterminate"></div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* Indeterminate shimmer used when a running step has no numeric progress yet. */
.progress-indeterminate {
  background: linear-gradient(90deg, transparent, rgba(129, 140, 248, 0.55), transparent);
  animation: progress-slide 1.2s ease-in-out infinite;
}

@keyframes progress-slide {
  0% {
    transform: translateX(-100%);
  }
  100% {
    transform: translateX(300%);
  }
}
</style>
