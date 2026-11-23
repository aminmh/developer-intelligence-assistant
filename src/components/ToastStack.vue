<script setup lang="ts">
import { AlertTriangle, CheckCircle, Info, X } from 'lucide-vue-next';
import { TOAST_DURATION_MS, useToasts, type ToastKind } from '../composables/useToasts';

const { toasts, dismiss, pause, resume } = useToasts();
const icons: Record<ToastKind, unknown> = { success: CheckCircle, error: AlertTriangle, info: Info };

// Focus leaving to another element inside the same toast is not a real leave.
const onFocusOut = (id: number, event: FocusEvent) => {
  const card = event.currentTarget as HTMLElement;
  if (!card.contains(event.relatedTarget as Node | null)) resume(id);
};
</script>

<template>
  <div class="app-toasts" aria-label="Notifications">
    <!-- Errors interrupt screen readers; confirmations wait their turn. -->
    <div class="sr-only" role="alert" aria-atomic="true">
      <template v-for="item in toasts" :key="`a-${item.id}-${item.count}`"><span v-if="item.kind === 'error'">{{ item.title }}: {{ item.message }}</span></template>
    </div>
    <div class="sr-only" role="status" aria-live="polite" aria-atomic="true">
      <template v-for="item in toasts" :key="`s-${item.id}-${item.count}`"><span v-if="item.kind !== 'error'">{{ item.message }}</span></template>
    </div>

    <TransitionGroup name="app-toast" tag="ol" class="app-toast-list">
      <li
        v-for="item in toasts"
        :key="item.id"
        class="app-toast app-glass"
        :data-kind="item.kind"
        :data-paused="item.paused"
        @pointerenter="pause(item.id)"
        @pointerleave="resume(item.id)"
        @focusin="pause(item.id)"
        @focusout="onFocusOut(item.id, $event)"
      >
        <component :is="icons[item.kind]" class="app-toast-icon" aria-hidden="true" />
        <div class="app-toast-text">
          <p class="app-toast-title">{{ item.title }}<span v-if="item.count > 1" class="app-toast-count">×{{ item.count }}</span></p>
          <p class="app-toast-body">{{ item.message }}</p>
        </div>
        <button type="button" class="app-toast-close" aria-label="Dismiss notification" @click="dismiss(item.id)">
          <X aria-hidden="true" />
        </button>
        <span
          :key="`${item.remaining}-${item.count}-${item.paused}`"
          class="app-toast-timer"
          aria-hidden="true"
          :style="{ '--toast-from': String(item.remaining / TOAST_DURATION_MS), animationDuration: `${item.remaining}ms` }"
        ></span>
      </li>
    </TransitionGroup>
  </div>
</template>
