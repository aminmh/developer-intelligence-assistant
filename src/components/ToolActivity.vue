<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { Wrench, Loader2, ChevronDown, AlertTriangle } from 'lucide-vue-next';
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
  <div v-if="calls.length || notice" class="chat-steps-wrap">
    <div v-if="notice" class="chat-tag chat-steps-notice" data-tone="warn" role="status">
      <AlertTriangle aria-hidden="true" />
      <span>Code tools unavailable: {{ notice }}</span>
    </div>

    <div v-if="calls.length" class="chat-steps">
      <button type="button" class="chat-steps-summary" :aria-expanded="open" @click="open = !open">
        <Loader2 v-if="running" class="chat-spin" aria-hidden="true" />
        <Wrench v-else aria-hidden="true" />
        <span v-if="running">Checking the code… {{ calls.length }} {{ calls.length === 1 ? 'step' : 'steps' }}</span>
        <span v-else>Checked the code in {{ calls.length }} {{ calls.length === 1 ? 'step' : 'steps' }}</span>
        <span v-if="failed" class="chat-steps-failed">· {{ failed }} failed</span>
        <ChevronDown class="chat-steps-chev" :data-open="open" aria-hidden="true" />
      </button>
      <ol v-if="open" class="chat-steps-list">
        <li
          v-for="call in calls"
          :key="call.id"
          :data-status="call.status"
          :title="call.error ? `${call.name}: ${call.error}` : call.name"
        >
          <span class="truncate">{{ describeToolCall(call) }}</span>
          <span v-if="call.truncated" class="chat-steps-flag">· partial</span>
          <span v-if="call.error" class="chat-steps-failed">· {{ describeToolError(call.error) }}</span>
          <em>{{ duration(call.durationMs) }}</em>
        </li>
      </ol>
    </div>
  </div>
</template>
