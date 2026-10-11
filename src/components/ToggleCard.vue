<script setup lang="ts">
withDefaults(defineProps<{
  modelValue: boolean;
  label: string;
  description?: string;
  density?: 'compact' | 'normal';
}>(), {
  description: '',
  density: 'normal',
});

const emit = defineEmits<{
  (event: 'update:modelValue', value: boolean): void;
}>();
</script>

<template>
  <button
    type="button"
    class="flex items-center justify-between gap-3 rounded-xl border text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950"
    :class="[
      density === 'compact' ? 'h-10 px-3' : 'px-3 py-3',
      modelValue ? 'border-emerald-400/40 bg-emerald-500/5' : 'border-slate-700 bg-slate-900/30',
    ]"
    :aria-pressed="modelValue"
    @click="emit('update:modelValue', !modelValue)"
  >
    <span class="flex min-w-0 items-center gap-2">
      <slot name="icon" />
      <span class="min-w-0">
        <span class="block font-semibold text-slate-200" :class="density === 'compact' ? 'text-[10px]' : 'text-xs'">{{ label }}</span>
        <span v-if="description && density !== 'compact'" class="mt-0.5 block text-[10px] text-slate-500">{{ description }}</span>
      </span>
    </span>
    <span class="shrink-0 font-mono text-[10px]" :class="modelValue ? 'text-emerald-300' : 'text-slate-500'">
      {{ modelValue ? 'On' : 'Off' }}
    </span>
  </button>
</template>
