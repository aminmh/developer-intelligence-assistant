<script setup lang="ts">
import { computed } from 'vue';

const props = defineProps<{ modelValue: string; disabled: boolean }>();
const emit = defineEmits<{ 'update:modelValue': [value: string] }>();
type Item = Record<string, any>;
const schema = computed<Item | null>(() => {
  try {
    const parsed = JSON.parse(props.modelValue);
    return parsed && Array.isArray(parsed.tables) && Array.isArray(parsed.relationships) && parsed.tables.every((t: Item) => Array.isArray(t.columns) && Array.isArray(t.keys) && Array.isArray(t.indexes)) ? parsed : null;
  } catch { return null; }
});
const fields: Record<string, string[]> = {
  table: ['name', 'namespace'], column: ['name', 'type', 'nullable', 'auto_increment'],
  key: ['name', 'kind', 'column_ids'], index: ['name', 'column_ids', 'unique'],
  relationship: ['name', 'source_table_id', 'source_column_ids', 'target_table_id', 'target_column_ids', 'on_delete', 'on_update'],
};
function commit(change: (value: Item) => void) {
  if (!schema.value) return;
  const value = structuredClone(schema.value); change(value);
  emit('update:modelValue', JSON.stringify(value, null, 2));
}
function collection(value: Item, kind: string, tableIndex: number): Item[] {
  if (kind === 'table') return value.tables;
  if (kind === 'relationship') return value.relationships;
  return value.tables[tableIndex][kind === 'index' ? 'indexes' : `${kind}s`];
}
function update(kind: string, tableIndex: number, itemIndex: number, field: string, event: Event) {
  const input = event.target as HTMLInputElement;
  commit((value) => {
    const item = collection(value, kind, tableIndex)[itemIndex];
    item[field] = input.type === 'checkbox' ? input.checked : field.endsWith('_ids') ? input.value.split(',').map((v) => v.trim()) : ['namespace', 'name', 'on_delete', 'on_update'].includes(field) && !input.value && !(kind === 'table' && field === 'name') && !(kind === 'column' && field === 'name') && !(kind === 'index' && field === 'name') ? null : input.value;
  });
}
function add(kind: string, tableIndex = 0) {
  const id = crypto.randomUUID();
  const defaults: Record<string, Item> = {
    table: { id, name: 'new_table', namespace: null, columns: [], keys: [], indexes: [] },
    column: { id, name: 'new_column', type: 'string', nullable: true, default: null, auto_increment: false },
    key: { id, name: null, kind: 'unique', column_ids: [] },
    index: { id, name: 'new_index', column_ids: [], unique: false },
    relationship: { id, name: null, source_table_id: '', source_column_ids: [], target_table_id: '', target_column_ids: [], on_delete: null, on_update: null },
  };
  commit((value) => collection(value, kind, tableIndex).push(defaults[kind]));
}
function remove(kind: string, tableIndex: number, index: number) {
  if (window.confirm(`Remove this ${kind}? References are not removed automatically; review them before saving.`)) commit((value) => { collection(value, kind, tableIndex).splice(index, 1); });
}
</script>

<template>
  <div v-if="schema" class="space-y-3">
    <p>Structured controls use stable IDs. Enter referenced column IDs separated by commas; removing an item may leave references to repair.</p>
    <fieldset v-for="(table, ti) in schema.tables" :key="table.id" :disabled="disabled" class="space-y-2 rounded-lg border border-slate-700 p-3">
      <legend>Table {{ table.name }} · ID {{ table.id }}</legend>
      <label v-for="field in fields.table" :key="field" class="mr-2 inline-block">{{ field }} <input :value="table[field] ?? ''" @input="update('table', ti, ti, field, $event)" class="rounded border border-slate-700 bg-slate-950 p-1"></label>
      <button type="button" @click="remove('table', ti, ti)" class="underline">Remove table</button>
      <div v-for="kind in ['column', 'key', 'index']" :key="kind" class="space-y-2">
        <strong>{{ kind }}s</strong>
        <fieldset v-for="(item, ii) in collection(schema, kind, ti)" :key="item.id" class="rounded border border-slate-800 p-2">
          <legend>{{ kind }} · ID {{ item.id }}</legend>
          <label v-for="field in fields[kind]" :key="field" class="mr-2 inline-block">{{ field }}
            <input v-if="['nullable', 'auto_increment', 'unique'].includes(field)" type="checkbox" :checked="item[field]" @input="update(kind, ti, ii, field, $event)">
            <select v-else-if="field === 'kind'" :value="item[field]" @input="update(kind, ti, ii, field, $event)" class="rounded border border-slate-700 bg-slate-950 p-1"><option>primary</option><option>unique</option></select>
            <input v-else :value="Array.isArray(item[field]) ? item[field].join(',') : item[field] ?? ''" @input="update(kind, ti, ii, field, $event)" class="rounded border border-slate-700 bg-slate-950 p-1">
          </label>
          <button type="button" @click="remove(kind, ti, ii)" class="underline">Remove {{ kind }}</button>
        </fieldset>
        <button type="button" @click="add(kind, ti)" class="underline">Add {{ kind }}</button>
      </div>
    </fieldset>
    <button type="button" :disabled="disabled" @click="add('table')" class="underline">Add table</button>
    <fieldset :disabled="disabled" class="space-y-2 rounded-lg border border-slate-700 p-3">
      <legend>Relationships</legend>
      <fieldset v-for="(item, ii) in schema.relationships" :key="item.id" class="rounded border border-slate-800 p-2">
        <legend>Relationship · ID {{ item.id }}</legend>
        <label v-for="field in fields.relationship" :key="field" class="mr-2 inline-block">{{ field }} <input :value="Array.isArray(item[field]) ? item[field].join(',') : item[field] ?? ''" @input="update('relationship', 0, ii, field, $event)" class="rounded border border-slate-700 bg-slate-950 p-1"></label>
        <button type="button" @click="remove('relationship', 0, ii)" class="underline">Remove relationship</button>
      </fieldset>
      <button type="button" @click="add('relationship')" class="underline">Add relationship</button>
    </fieldset>
  </div>
  <p v-else role="status" class="text-amber-300">Structured controls are unavailable until the JSON has valid schema arrays. Your JSON remains below.</p>
</template>
