<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { Wrench, Loader2, Check, X, ChevronDown, ChevronUp, AlertTriangle } from 'lucide-vue-next';
import type { ToolCallView, ToolAvailability } from '../types';
import { describeToolCall, describeToolError } from '../utils/agentTools';

const props = defineProps<{
  calls?: ToolCallView[];
  tools?: ToolAvailability | null;
  streaming?: boolean;
}>();

// Reasons worth showing: the user can act on them. Unrelated routes and
// local-model chats simply do not use tools.
const NOTICE_CODES = new Set(['untested', 'unsupported', 'disabled', 'error']);

const calls = computed(() => props.calls ?? []);
const running = computed(() => calls.value.filter((c) => c.status === 'running').length);
const failed = computed(() => calls.value.filter((c) => c.status === 'error').length);
const notice = computed(() =>
  props.tools && !props.tools.available && NOTICE_CODES.has(props.tools.code) ? props.tools.reason : null,
);

// Open while the answer streams; a long finished list starts collapsed.
const open = ref(true);
watch(
  () => props.streaming,
  (now, before) => {
    if (before && !now && calls.value.length > 4) open.value = false;
  },
);

const duration = (ms?: number | null) => (typeof ms === 'number' ? (ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`) : '');
</script>

<template>
  <div v-if="calls.length || notice" class="mb-3 text-left">
    <div
      v-if="notice"
      class="flex items-start gap-1.5 text-[10px] font-mono text-amber-300/90 bg-amber-400/5 border border-amber-400/20 rounded-lg px-2.5 py-1.5"
      role="status"
    >
      <AlertTriangle class="w-3 h-3 mt-px shrink-0" />
      <span>Code tools unavailable — {{ notice }}</span>
    </div>

    <div v-if="calls.length" class="rounded-lg border border-slate-800 bg-slate-950/40">
      <button
        type="button"
        class="w-full flex items-center gap-1.5 px-2.5 py-1.5 text-[10px] font-mono text-slate-400 hover:text-slate-200 transition-colors"
        :aria-expanded="open"
        @click="open = !open"
      >
        <Wrench class="w-3 h-3 text-cyan-400" />
        <span v-if="running">Checking the code… {{ calls.length }} {{ calls.length === 1 ? 'step' : 'steps' }}</span>
        <span v-else>Checked the code in {{ calls.length }} {{ calls.length === 1 ? 'step' : 'steps' }}</span>
        <span v-if="failed" class="text-rose-300/80">· {{ failed }} failed</span>
        <ChevronUp v-if="open" class="w-3 h-3 ml-auto" />
        <ChevronDown v-else class="w-3 h-3 ml-auto" />
      </button>
      <ol v-if="open" class="px-2.5 pb-2 space-y-1">
        <li
          v-for="call in calls"
          :key="call.id"
          class="flex items-center gap-1.5 text-[11px] font-mono min-w-0"
          :title="call.error ? `${call.name}: ${call.error}` : call.name"
        >
          <Loader2 v-if="call.status === 'running'" class="w-3 h-3 shrink-0 text-indigo-300 animate-spin" />
          <Check v-else-if="call.status === 'success'" class="w-3 h-3 shrink-0 text-emerald-400" />
          <X v-else class="w-3 h-3 shrink-0 text-rose-400" />
          <span class="truncate" :class="call.status === 'error' ? 'text-slate-500' : 'text-slate-300'">{{ describeToolCall(call) }}</span>
          <span v-if="call.truncated" class="shrink-0 text-amber-300/70">· partial</span>
          <span v-if="call.error" class="shrink-0 text-rose-300/70">· {{ describeToolError(call.error) }}</span>
          <span class="shrink-0 ml-auto pl-2 text-slate-600">{{ duration(call.durationMs) }}</span>
        </li>
      </ol>
    </div>
  </div>
</template>
