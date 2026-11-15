<script setup lang="ts">
import { computed } from 'vue';
import { CheckCircle2, AlertTriangle, Loader2, Circle, Cpu } from 'lucide-vue-next';

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

// Card header: "step N of M" and a single overall bar for every consumer.
const doneCount = computed(() => props.steps.filter((s) => s.status === 'done').length);
const currentIndex = computed(() => {
  const running = props.steps.findIndex((s) => s.status === 'running');
  return running >= 0 ? running + 1 : Math.min(doneCount.value + 1, props.steps.length);
});
const barFraction = computed<number | null>(() => (overallPercent.value == null ? null : overallPercent.value / 100));
</script>

<template>
  <div class="chat-progress" role="status" aria-live="polite">
    <div class="chat-progress-top">
      <Cpu aria-hidden="true" />
      <span class="truncate">Working on {{ fileName }}</span>
      <span class="chat-progress-step">step {{ currentIndex }} of {{ steps.length }}</span>
    </div>
    <div class="chat-progress-label">{{ overallLabel }}</div>

    <div class="chat-track" :data-indeterminate="barFraction == null">
      <i v-if="barFraction != null" :style="{ transform: `scaleX(${barFraction})` }"></i>
    </div>

    <ol class="chat-progress-steps">
      <li v-for="step in steps" :key="step.id" :data-status="step.status">
        <CheckCircle2 v-if="step.status === 'done'" aria-hidden="true" />
        <Loader2 v-else-if="step.status === 'running'" class="chat-spin" aria-hidden="true" />
        <AlertTriangle v-else-if="step.status === 'error'" aria-hidden="true" />
        <Circle v-else aria-hidden="true" />
        <span class="truncate">{{ step.label }}</span>
        <span v-if="step.status === 'running' && percentFor(step) != null" class="chat-progress-pct">{{ percentFor(step) }}%</span>
        <span v-else-if="step.detail" class="chat-progress-pct">{{ step.detail }}</span>
      </li>
    </ol>
  </div>
</template>
