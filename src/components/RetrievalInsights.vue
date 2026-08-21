<script setup lang="ts">
import { ref } from 'vue';
import { ChevronDown, ChevronUp, CircleAlert, Database, GitBranch, Loader2, Sparkles } from 'lucide-vue-next';
import type { RetrievalInsights } from '../types';

const props = defineProps<{ queryId: string }>();

const expanded = ref(false);
const loading = ref(false);
const error = ref<string | null>(null);
const insights = ref<RetrievalInsights | null>(null);

const load = async () => {
  if (insights.value || loading.value) return;
  loading.value = true;
  error.value = null;
  try {
    const response = await fetch(`/api/query/${encodeURIComponent(props.queryId)}/insights`);
    const data = await response.json();
    if (!response.ok || !data.success) {
      throw new Error(data.error || 'Retrieval insights unavailable.');
    }
    insights.value = data.insights;
  } catch (cause: any) {
    error.value = cause?.message || 'Retrieval insights unavailable.';
  } finally {
    loading.value = false;
  }
};

const toggle = () => {
  expanded.value = !expanded.value;
  if (expanded.value) load();
};

const modeLabel = (mode: RetrievalInsights['mode']) => {
  if (mode === 'hybrid') return 'Graph-assisted';
  if (mode === 'vector_only') return 'Vector search';
  if (mode === 'explicit_context') return 'Tool context';
  if (mode === 'no_results') return 'No results';
  return 'Retrieval unavailable';
};

const modeClasses = (mode: RetrievalInsights['mode']) => {
  if (mode === 'hybrid') return 'text-emerald-300 bg-emerald-500/10 border-emerald-500/25';
  if (mode === 'vector_only') return 'text-indigo-300 bg-indigo-500/10 border-indigo-500/25';
  if (mode === 'explicit_context') return 'text-amber-300 bg-amber-500/10 border-amber-500/25';
  return 'text-slate-400 bg-slate-500/10 border-slate-500/25';
};
</script>

<template>
  <div class="mt-2 max-w-3xl w-full">
    <button
      @click="toggle"
      class="group flex items-center gap-2 text-[10px] text-slate-400 hover:text-indigo-300 font-mono transition-colors"
    >
      <Sparkles class="w-3 h-3 text-indigo-400" />
      <span>Retrieval insights</span>
      <Loader2 v-if="loading" class="w-3 h-3 animate-spin text-indigo-400" />
      <ChevronUp v-else-if="expanded" class="w-3 h-3" />
      <ChevronDown v-else class="w-3 h-3" />
    </button>

    <div v-if="expanded" class="mt-2 rounded-2xl border border-slate-800 bg-[#0c101f]/90 p-4 text-left shadow-lg">
      <div v-if="loading" class="flex items-center gap-2 py-3 text-[10px] text-indigo-300 font-mono">
        <Loader2 class="w-3.5 h-3.5 animate-spin" />
        Inspecting retrieval path…
      </div>

      <div v-else-if="error" class="flex items-center gap-2 py-2 text-[10px] text-slate-400 font-mono">
        <CircleAlert class="w-3.5 h-3.5 text-amber-400 shrink-0" />
        <span>{{ error }}</span>
      </div>

      <template v-else-if="insights">
        <div v-if="insights.available === false" class="flex items-start gap-2 py-2 text-[10px] text-slate-400 font-mono">
          <CircleAlert class="w-3.5 h-3.5 text-amber-400 shrink-0" />
          <span>Retrieval details are unavailable for this older response. New responses will include provenance here.</span>
        </div>

        <div class="flex items-center justify-between gap-3 mb-3">
          <div class="flex items-center gap-2">
            <div class="p-1.5 rounded-lg bg-indigo-500/10 border border-indigo-500/20">
              <GitBranch class="w-3.5 h-3.5 text-indigo-400" />
            </div>
            <div>
              <p class="text-[11px] font-semibold text-slate-200">How context was found</p>
              <p class="text-[9px] text-slate-500 font-mono">Evidence from the retrieval pipeline</p>
            </div>
          </div>
          <span
            class="text-[9px] font-mono px-2 py-1 rounded-full border"
            :class="modeClasses(insights.mode)"
          >{{ modeLabel(insights.mode) }}</span>
        </div>

        <div class="grid grid-cols-3 gap-2 mb-3">
          <div class="rounded-xl bg-slate-900/60 border border-slate-800 p-2.5">
            <p class="text-[9px] text-slate-500 font-mono">Vector hits</p>
            <p class="text-sm text-slate-200 font-semibold mt-1">{{ insights.vector_result_count }}</p>
          </div>
          <div class="rounded-xl bg-slate-900/60 border border-slate-800 p-2.5">
            <p class="text-[9px] text-slate-500 font-mono">Graph neighbors</p>
            <p class="text-sm text-slate-200 font-semibold mt-1">{{ insights.graph_neighbor_count }}</p>
          </div>
          <div class="rounded-xl bg-slate-900/60 border border-slate-800 p-2.5">
            <p class="text-[9px] text-slate-500 font-mono">Graph context</p>
            <p class="text-sm text-emerald-300 font-semibold mt-1">{{ insights.graph_context_count }}</p>
          </div>
        </div>

        <div v-if="insights.graph_fallback" class="mb-3 rounded-xl border border-amber-500/20 bg-amber-500/5 px-3 py-2 text-[9px] text-amber-300 font-mono">
          Graph retrieval was unavailable, so the answer used vector search only.
        </div>

        <div v-if="insights.sources.length" class="space-y-1.5">
          <p class="text-[9px] uppercase tracking-wider text-slate-500 font-semibold">Top sources</p>
          <div
            v-for="(source, index) in insights.sources"
            :key="`${source.file}-${source.name}-${index}`"
            class="flex items-center gap-2 rounded-lg bg-slate-900/40 border border-slate-800 px-2.5 py-2"
          >
            <Database class="w-3 h-3 text-slate-500 shrink-0" />
            <span class="min-w-0 flex-1 truncate text-[10px] text-slate-300 font-mono">
              {{ source.class_name ? `${source.class_name}::${source.name}` : source.name || source.file }}
            </span>
            <span
              v-if="source.via_graph"
              class="text-[8px] px-1.5 py-0.5 rounded-full border text-emerald-300 bg-emerald-500/10 border-emerald-500/20 font-mono"
            >GRAPH</span>
            <span class="text-[9px] text-slate-500 font-mono">{{ Math.round(source.score * 100) }}%</span>
          </div>
        </div>

        <p class="mt-3 pt-3 border-t border-slate-800 text-[9px] leading-relaxed text-slate-600">
          This panel shows which context entered the prompt. It does not claim the model consciously relied on every retrieved item.
        </p>
      </template>
    </div>
  </div>
</template>
