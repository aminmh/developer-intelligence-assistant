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
  <div class="chat-approve" role="group" aria-label="Tool call approval" :data-state="approval.state">
    <h4>
      <ShieldAlert aria-hidden="true" />
      <span v-if="approval.state === 'approved'">Approved, continuing</span>
      <span v-else-if="approval.state === 'denied'">Denied. The model will answer without it.</span>
      <span v-else-if="expired">This request expired. Ask the question again.</span>
      <span v-else>The model wants to run {{ approval.calls.length === 1 ? 'a tool that can change things' : `${approval.calls.length} tools that can change things` }}</span>
      <em v-if="approval.state === 'pending' && !expired && minutesLeft !== null">expires in {{ minutesLeft }} min</em>
    </h4>
    <ul class="chat-approve-calls">
      <li v-for="call in approval.calls" :key="call.id">
        <div class="chat-approve-name">{{ call.name }}</div>
        <pre>{{ argsText(call.arguments) }}</pre>
        <div v-if="call.invalid" class="chat-steps-failed">Won't run: {{ call.invalid }}</div>
      </li>
    </ul>
    <p v-if="approval.state === 'error'" class="chat-steps-failed">{{ approval.error }}</p>
    <div v-if="(approval.state === 'pending' || approval.state === 'deciding') && !expired" class="chat-gate-actions">
      <button
        type="button"
        class="chat-btn"
        data-variant="approve"
        :disabled="approval.state === 'deciding' || !anyValid"
        @click="emit('decide', 'approve')"
      >
        <Loader2 v-if="approval.state === 'deciding'" class="chat-spin" aria-hidden="true" />
        <Check v-else aria-hidden="true" />
        Approve
      </button>
      <button type="button" class="chat-btn" :disabled="approval.state === 'deciding'" @click="emit('decide', 'deny')">
        <X aria-hidden="true" />
        Deny
      </button>
    </div>
  </div>
</template>
