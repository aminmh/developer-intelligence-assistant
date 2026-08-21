<script setup lang="ts">
import { ref, watch } from 'vue';
import { X, Loader2, RefreshCw, Gauge } from 'lucide-vue-next';

interface MetricResult {
  score: number | null;
  tier: string;
}

const props = defineProps<{
  show: boolean;
  queryId: string | null;
}>();

const emit = defineEmits<{ (e: 'close'): void }>();

const loading = ref(false);
const error = ref<string | null>(null);
const metrics = ref<Record<string, MetricResult> | null>(null);

const METRIC_LABELS: Record<string, string> = {
  faithfulness: 'Faithfulness',
  answer_relevancy: 'Answer Relevancy',
  context_precision: 'Context Precision',
};

const METRIC_DESCRIPTIONS: Record<string, string> = {
  faithfulness: 'How well the answer sticks to the retrieved source material.',
  answer_relevancy: 'How directly the answer addresses the question asked.',
  context_precision: 'How much of the retrieved material was actually useful.',
};

const TIER_COLORS: Record<string, string> = {
  Excellent: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30',
  Good: 'text-indigo-400 bg-indigo-500/10 border-indigo-500/30',
  'Needs review': 'text-amber-400 bg-amber-500/10 border-amber-500/30',
  Poor: 'text-rose-400 bg-rose-500/10 border-rose-500/30',
  Unavailable: 'text-slate-400 bg-slate-500/10 border-slate-500/30',
};

const load = async () => {
  if (!props.queryId) return;
  loading.value = true;
  error.value = null;
  metrics.value = null;
  try {
    const res = await fetch(`/api/query/${props.queryId}/evaluate`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      metrics.value = data.metrics;
    } else {
      error.value = data.error || 'Evaluation unavailable for this response';
    }
  } catch {
    error.value = 'Evaluation unavailable for this response';
  } finally {
    loading.value = false;
  }
};

watch(() => props.show, (v) => {
  if (v) load();
});
</script>

<template>
  <Teleport to="body">
    <div
      v-if="show"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      @click.self="emit('close')"
    >
      <div class="w-full max-w-md bg-[#0c101f] border border-slate-800 rounded-2xl shadow-xl">
        <div class="flex items-center justify-between p-4 border-b border-slate-800">
          <div class="flex items-center gap-2">
            <Gauge class="w-4 h-4 text-indigo-400" />
            <span class="text-sm font-semibold text-white">Response Evaluation</span>
          </div>
          <button @click="emit('close')" class="text-slate-500 hover:text-slate-300">
            <X class="w-4 h-4" />
          </button>
        </div>

        <div class="p-4 space-y-3 max-h-[70vh] overflow-y-auto">
          <!-- Loading -->
          <div v-if="loading" class="flex items-center gap-2 text-xs text-indigo-300 font-mono py-6 justify-center">
            <Loader2 class="w-4 h-4 animate-spin" />
            Evaluating…
          </div>

          <!-- Error -->
          <div v-else-if="error" class="text-center py-6 space-y-3">
            <p class="text-xs text-slate-400">{{ error }}</p>
            <button
              @click="load"
              class="inline-flex items-center gap-1.5 text-xs text-indigo-300 hover:text-indigo-200 font-mono"
            >
              <RefreshCw class="w-3.5 h-3.5" />
              Retry
            </button>
          </div>

          <!-- Metrics -->
          <div v-else-if="metrics" class="space-y-3">
            <div
              v-for="(label, key) in METRIC_LABELS"
              :key="key"
              class="bg-slate-900/40 border border-slate-800 rounded-xl p-4"
            >
              <div class="flex items-center justify-between mb-1.5">
                <span class="text-xs font-semibold text-slate-200">{{ label }}</span>
                <span
                  class="text-[10px] font-mono px-2 py-0.5 rounded-full border"
                  :class="TIER_COLORS[metrics[key]?.tier ?? 'Unavailable']"
                >
                  {{ metrics[key]?.tier ?? 'Unavailable' }}
                </span>
              </div>

              <div class="flex items-center gap-3">
                <div class="flex-1 h-1.5 bg-slate-800 rounded-full overflow-hidden">
                  <div
                    class="h-full bg-indigo-500 rounded-full transition-all duration-500"
                    :style="{ width: `${Math.round((metrics[key]?.score ?? 0) * 100)}%` }"
                  ></div>
                </div>
                <span class="text-xs font-mono text-slate-300 tabular-nums">
                  {{ metrics[key]?.score != null ? `${Math.round((metrics[key]!.score as number) * 100)}%` : '—' }}
                </span>
              </div>

              <p class="text-[10px] text-slate-500 mt-2 leading-relaxed">
                {{ METRIC_DESCRIPTIONS[key] }}
              </p>
            </div>

            <p class="text-[9px] text-slate-600 leading-relaxed pt-1">
              Scores come from a local judge model and are a sanity signal, not ground truth.
            </p>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>
