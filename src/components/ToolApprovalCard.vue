<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue';
import { ShieldAlert, Check, X, Loader2 } from 'lucide-vue-next';
import type { ApprovalView } from '../types';

const props = defineProps<{ approval: ApprovalView }>();
const emit = defineEmits<{ (e: 'decide', decision: 'approve' | 'deny'): void }>();

// Re-evaluate expiry every 15 s so an expired request stops offering Approve.
const now = ref(Date.now());
const timer = setInterval(() => { now.value = Date.now(); }, 15_000);
onBeforeUnmount(() => clearInterval(timer));

const expired = computed(() => props.approval.state === 'pending' && !!props.approval.expiresAt
  && Date.parse(props.approval.expiresAt) <= now.value);
const minutesLeft = computed(() => {
  if (!props.approval.expiresAt) return null;
  return Math.max(0, Math.ceil((Date.parse(props.approval.expiresAt) - now.value) / 60_000));
});
const anyValid = computed(() => props.approval.calls.some((c) => !c.invalid));
const argsText = (args: Record<string, unknown>) => JSON.stringify(args, null, 2);
</script>

<template>
  <div class="mb-3 rounded-lg border border-amber-400/30 bg-amber-400/5 p-3 text-left" role="group" aria-label="Tool call approval">
    <div class="flex items-center gap-1.5 text-[11px] font-mono text-amber-200">
      <ShieldAlert class="w-3.5 h-3.5" />
      <span v-if="approval.state === 'approved'">Approved — continuing</span>
      <span v-else-if="approval.state === 'denied'">Denied — the model will answer without it</span>
      <span v-else-if="expired">This request expired. Ask the question again.</span>
      <span v-else>The model wants to run {{ approval.calls.length === 1 ? 'a tool that can change things' : `${approval.calls.length} tools that can change things` }}</span>
      <span v-if="approval.state === 'pending' && !expired && minutesLeft !== null" class="ml-auto text-slate-500">expires in {{ minutesLeft }} min</span>
    </div>
    <ul class="mt-2 space-y-2">
      <li v-for="call in approval.calls" :key="call.id" class="rounded border border-slate-800 bg-slate-950/50 p-2">
        <div class="text-[11px] font-mono text-slate-200">{{ call.name }}</div>
        <pre class="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all text-[10px] leading-4 text-slate-400">{{ argsText(call.arguments) }}</pre>
        <div v-if="call.invalid" class="mt-1 text-[10px] font-mono text-rose-300">Will not run: {{ call.invalid }}</div>
      </li>
    </ul>
    <div v-if="approval.state === 'error'" class="mt-2 text-[10px] font-mono text-rose-300">{{ approval.error }}</div>
    <div v-if="(approval.state === 'pending' || approval.state === 'deciding') && !expired" class="mt-2 flex gap-2">
      <button
        type="button"
        class="flex items-center gap-1 rounded-md bg-emerald-600 px-2.5 py-1 text-[11px] text-white hover:bg-emerald-500 disabled:opacity-50"
        :disabled="approval.state === 'deciding' || !anyValid"
        @click="emit('decide', 'approve')"
      >
        <Loader2 v-if="approval.state === 'deciding'" class="w-3 h-3 animate-spin" />
        <Check v-else class="w-3 h-3" />
        Approve
      </button>
      <button
        type="button"
        class="flex items-center gap-1 rounded-md border border-slate-700 px-2.5 py-1 text-[11px] text-slate-300 hover:text-white disabled:opacity-50"
        :disabled="approval.state === 'deciding'"
        @click="emit('decide', 'deny')"
      >
        <X class="w-3 h-3" />
        Deny
      </button>
    </div>
  </div>
</template>
