<script setup lang="ts">
import { ref, watch } from 'vue';

const props = withDefaults(defineProps<{
  modelValue: unknown;
  label?: string;
  rows?: number;
  allowEmpty?: boolean;
  suggestions?: string[];
}>(), { rows: 6, allowEmpty: false, suggestions: () => [] });

const emit = defineEmits<{
  'update:modelValue': [value: unknown];
  validity: [valid: boolean];
}>();

const stringify = (value: unknown) => JSON.stringify(value ?? {}, null, 2);
const initialText = props.allowEmpty && props.modelValue === undefined ? '' : stringify(props.modelValue);
const text = ref(initialText);
const error = ref('');

const setValidity = (valid: boolean, message = '') => {
  error.value = message;
  emit('validity', valid);
};

const parse = () => {
  if (!text.value.trim() && props.allowEmpty) {
    setValidity(true);
    emit('update:modelValue', undefined);
    return;
  }
  try {
    const value = JSON.parse(text.value);
    setValidity(true);
    emit('update:modelValue', value);
  } catch {
    setValidity(false, 'Invalid JSON.');
  }
};

const format = () => {
  try {
    const value = JSON.parse(text.value);
    text.value = stringify(value);
    setValidity(true);
    emit('update:modelValue', value);
  } catch {
    setValidity(false, 'Invalid JSON cannot be formatted.');
  }
};

const addSuggestion = (key: string) => {
  try {
    const current = JSON.parse(text.value || '{}');
    if (!current || Array.isArray(current) || typeof current !== 'object') {
      setValidity(false, 'Common headers require a JSON object.');
      return;
    }
    if (!(key in current)) current[key] = '';
    text.value = stringify(current);
    setValidity(true);
    emit('update:modelValue', current);
  } catch {
    setValidity(false, 'Fix invalid JSON before adding a header.');
  }
};

const onSuggestionChange = (event: Event) => {
  const select = event.target as HTMLSelectElement;
  addSuggestion(select.value);
  select.value = '';
};

watch(() => props.modelValue, (value) => {
  if (props.allowEmpty && value === undefined) {
    text.value = '';
    setValidity(true);
    return;
  }
  const next = stringify(value);
  try {
    if (JSON.stringify(JSON.parse(text.value)) === JSON.stringify(value)) return;
  } catch {
    return;
  }
  text.value = next;
  setValidity(true);
}, { deep: true });

setValidity(true);
</script>

<template>
  <div class="space-y-1">
    <div class="flex items-center justify-between gap-2">
      <label v-if="label" class="block text-[10px] uppercase tracking-wider text-slate-500">{{ label }}</label>
      <div class="flex items-center gap-1.5">
        <select
          v-if="suggestions.length"
          aria-label="Add common header"
          class="rounded-md border border-slate-700/80 bg-slate-800/70 px-2 py-1 text-[10px] text-cyan-300 outline-none focus:border-cyan-500/70"
          @change="onSuggestionChange"
        >
          <option value="">Add header…</option>
          <option v-for="suggestion in suggestions" :key="suggestion" :value="suggestion">{{ suggestion }}</option>
        </select>
        <button
          type="button"
          @click="format"
          class="rounded-md border border-slate-700/80 bg-slate-800/70 px-2 py-1 text-[10px] font-medium text-cyan-300 transition-colors hover:border-cyan-500/50 hover:bg-cyan-500/10"
        >
          Format JSON
        </button>
      </div>
    </div>
    <textarea
      v-model="text"
      :rows="rows"
      spellcheck="false"
      :aria-invalid="!!error"
      @input="parse"
      class="native-json-editor w-full resize-y rounded-lg border bg-slate-950/70 px-3 py-2 font-mono text-[11px] leading-5 text-slate-200 outline-none transition-colors placeholder:text-slate-600"
      :class="error ? 'border-rose-500/70 focus:border-rose-400' : 'border-slate-700/80 focus:border-cyan-500/70'"
    />
    <p v-if="error" class="text-[10px] text-rose-300">{{ error }}</p>
  </div>
</template>

<style scoped>
.native-json-editor {
  min-height: 140px;
  tab-size: 2;
  caret-color: rgb(103 232 249);
  box-shadow: inset 0 1px 0 rgb(148 163 184 / 0.04);
}

.native-json-editor::selection {
  background: rgb(8 145 178 / 0.35);
}
</style>
