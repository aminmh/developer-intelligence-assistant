<script setup lang="ts">
import { AlertTriangle, X } from 'lucide-vue-next';

defineProps<{
  show: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  busy?: boolean;
  error?: string;
}>();

const emit = defineEmits<{
  (e: 'confirm'): void;
  (e: 'cancel'): void;
}>();
</script>

<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="opacity-0"
      enter-to-class="opacity-100"
      leave-active-class="transition duration-150 ease-in"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0"
    >
      <div
        v-if="show"
        class="fixed inset-0 z-50 flex items-center justify-center p-4"
      >
        <!-- Backdrop -->
        <div
          class="absolute inset-0 bg-slate-950/70 backdrop-blur-sm"
          @click="!busy && emit('cancel')"
        />

        <!-- Modal -->
        <div
          class="relative w-full max-w-sm bg-slate-900 border border-slate-700/60 rounded-2xl shadow-2xl shadow-slate-950/50 p-6"
        >
          <div class="flex items-start gap-3 mb-4">
            <div class="w-9 h-9 rounded-xl bg-rose-500/10 border border-rose-500/20 flex items-center justify-center shrink-0">
              <AlertTriangle class="w-4 h-4 text-rose-400" />
            </div>
            <div class="flex-1 min-w-0">
              <h3 class="text-sm font-semibold text-white">{{ title }}</h3>
              <p class="text-xs text-slate-400 mt-1 leading-relaxed">{{ message }}</p>
              <p v-if="error" role="alert" class="text-xs text-rose-300 mt-3">{{ error }}</p>
            </div>
            <button
              @click="!busy && emit('cancel')"
              :disabled="busy"
              class="text-slate-500 hover:text-slate-300 transition-colors shrink-0 disabled:opacity-50"
            >
              <X class="w-4 h-4" />
            </button>
          </div>

          <div class="flex gap-2.5 justify-end">
            <button
              @click="emit('cancel')"
              :disabled="busy"
              class="px-4 py-2 text-xs font-medium text-slate-400 hover:text-white hover:bg-slate-800/60 rounded-xl transition-colors disabled:opacity-50"
            >
              {{ cancelLabel ?? 'Cancel' }}
            </button>
            <button
              @click="emit('confirm')"
              :disabled="busy"
              class="px-4 py-2 text-xs font-semibold text-white bg-rose-600 hover:bg-rose-500 rounded-xl transition-colors shadow-sm shadow-rose-600/20 disabled:opacity-50"
            >
              {{ busy ? 'Working…' : confirmLabel ?? 'Delete' }}
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>
