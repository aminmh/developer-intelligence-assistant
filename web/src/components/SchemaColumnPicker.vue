<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

type Column = Record<string, any>;
const props = defineProps<{ columns: Column[]; modelValue: string[]; disabled?: boolean; label: string }>();
const emit = defineEmits<{ 'update:modelValue': [ids: string[]] }>();
const root = ref<HTMLDetailsElement | null>(null);
const names = computed(() => props.modelValue.map(id => props.columns.find(column => column.id === id)?.name));
const missing = computed(() => props.modelValue.filter(id => !props.columns.some(column => column.id === id)));

// Picking order is kept: composite keys and indexes depend on it.
function toggle(id: string, checked: boolean) {
  const rest = props.modelValue.filter(value => value !== id);
  emit('update:modelValue', checked ? [...rest, id] : rest);
}
function closeOutside(event: MouseEvent) {
  if (root.value?.open && !root.value.contains(event.target as Node)) root.value.open = false;
}
onMounted(() => document.addEventListener('click', closeOutside));
onBeforeUnmount(() => document.removeEventListener('click', closeOutside));
</script>

<template>
  <details ref="root" class="picker" :class="{ 'picker-disabled': disabled }">
    <summary class="sc-input" :aria-label="label">
      <span v-if="modelValue.length" class="picker-value">{{ names.map(name => name ?? '?').join(', ') }}</span>
      <span v-else class="picker-empty">Choose columns</span>
    </summary>
    <div class="picker-menu" role="group" :aria-label="label">
      <label v-for="column in columns" :key="column.id"><input type="checkbox" :disabled="disabled" :checked="modelValue.includes(column.id)" @change="toggle(column.id, ($event.target as HTMLInputElement).checked)">{{ column.name }}</label>
      <p v-if="!columns.length" class="picker-empty">No columns available.</p>
    </div>
    <p v-if="missing.length" class="picker-warn">Missing column: {{ missing.join(', ') }}</p>
  </details>
</template>

<style scoped>
.picker { position: relative; min-width: 9rem; }
.picker > summary { display: flex; align-items: center; cursor: pointer; list-style: none; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.picker > summary::-webkit-details-marker { display: none; }
.picker-value { overflow: hidden; text-overflow: ellipsis; }
.picker-empty { color: var(--shell-text-3, var(--shell-text-2)); font-size: 0.75rem; }
.picker-disabled > summary { pointer-events: none; opacity: 0.6; }
.picker-menu { position: absolute; z-index: 20; top: calc(100% + 0.25rem); left: 0; display: grid; gap: 0.125rem; min-width: 100%; max-height: 14rem; overflow: auto; padding: 0.375rem; border: 1px solid var(--shell-line-strong); border-radius: 0.625rem; background: var(--shell-field, #0b1220); box-shadow: 0 10px 30px rgb(0 0 0 / 0.4); }
.picker-menu label { display: flex; align-items: center; gap: 0.5rem; padding: 0.25rem 0.375rem; border-radius: 0.375rem; font-size: 0.75rem; white-space: nowrap; cursor: pointer; }
.picker-menu label:hover { background: rgb(255 255 255 / 0.06); }
.picker-warn { margin: 0.125rem 0 0; font-size: 0.6875rem; color: #fcd34d; }
</style>
