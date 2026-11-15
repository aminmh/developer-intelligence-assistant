<script setup lang="ts">
import { computed, ref } from 'vue';

const props = defineProps<{ modelValue: string; disabled: boolean }>();
const emit = defineEmits<{ 'update:modelValue': [value: string] }>();
type Item = Record<string, any>;
const schema = computed<any>(() => {
  try {
    const parsed = JSON.parse(props.modelValue);
    return parsed && Array.isArray(parsed.tables) && Array.isArray(parsed.relationships) && parsed.tables.every((t: Item) => Array.isArray(t.columns) && Array.isArray(t.keys) && Array.isArray(t.indexes)) ? parsed : null;
  } catch { return null; }
});
const fields: Record<string, string[]> = {
  table: ['name', 'namespace'], column: ['name', 'type', 'nullable', 'auto_increment', 'default_expression', 'identity'],
  key: ['name', 'kind', 'column_ids'], index: ['name', 'column_ids', 'unique'],
  relationship: ['name', 'source_table_id', 'source_column_ids', 'target_table_id', 'target_column_ids', 'on_delete', 'on_update'],
};
const showIds = ref(false);
const actions = ['no action', 'restrict', 'cascade', 'set null', 'set default'];
const columnsOf = (tableId: string): Item[] => schema.value?.tables.find((t: Item) => t.id === tableId)?.columns ?? [];
const tableName = (tableId: string) => schema.value?.tables.find((t: Item) => t.id === tableId)?.name ?? '(choose a table)';
const missing = (ids: string[], tableId: string) => ids.filter(id => !columnsOf(tableId).some(c => c.id === id));
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
    item[field] = field === 'auto_increment' ? (input.value === 'unknown' ? null : input.value === 'true') : input.type === 'checkbox' ? input.checked : field.endsWith('_ids') ? input.value.split(',').map((v) => v.trim()) : ['namespace', 'name', 'on_delete', 'on_update', 'default_expression', 'identity'].includes(field) && !input.value && !(kind === 'table' && field === 'name') && !(kind === 'column' && field === 'name') && !(kind === 'index' && field === 'name') ? null : input.value;
  });
}
// Checking keeps the order in which columns were picked; composite keys depend on it.
function toggleColumn(kind: string, tableIndex: number, itemIndex: number, field: string, columnId: string, event: Event) {
  const checked = (event.target as HTMLInputElement).checked;
  commit((value) => {
    const item = collection(value, kind, tableIndex)[itemIndex];
    item[field] = checked ? [...item[field].filter((id: string) => id !== columnId), columnId] : item[field].filter((id: string) => id !== columnId);
  });
}
function setRelationshipTable(itemIndex: number, side: string, event: Event) {
  const tableId = (event.target as HTMLSelectElement).value;
  commit((value) => { const item = value.relationships[itemIndex]; item[`${side}_table_id`] = tableId; item[`${side}_column_ids`] = []; });
}
function add(kind: string, tableIndex = 0) {
  const id = crypto.randomUUID();
  const defaults: Record<string, Item> = {
    table: { id, name: 'new_table', namespace: null, columns: [], keys: [], indexes: [], inspection_metadata: [] },
    column: { id, name: 'new_column', type: 'string', nullable: true, default: null, auto_increment: false, default_expression: null, identity: null },
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
    <div class="flex flex-wrap items-center gap-3">
      <p class="m-0">Pick columns by name; stable IDs are used behind the scenes. Removing an item may leave references to repair.</p>
      <label class="inline-flex items-center gap-1"><input v-model="showIds" type="checkbox">Show IDs</label>
    </div>
    <details v-for="(table, ti) in (schema.tables as Item[])" :key="table.id" class="rounded-lg border border-slate-700">
      <summary class="cursor-pointer p-3">
        <strong>{{ table.namespace ? `${table.namespace}.` : '' }}{{ table.name }}</strong>
        <span class="ml-2 text-xs opacity-70">{{ table.columns.length }} columns · {{ table.keys.length }} keys · {{ table.indexes.length }} indexes<template v-if="table.inspection_metadata?.length"> · {{ table.inspection_metadata.length }} notes</template></span>
      </summary>
      <fieldset :disabled="disabled" class="space-y-2 p-3 pt-0">
        <p v-if="showIds" class="m-0 break-all text-xs opacity-70">Table ID {{ table.id }}</p>
        <details v-if="table.inspection_metadata?.length"><summary>Catalog features and inspection notes ({{ table.inspection_metadata.length }})</summary><ul><li v-for="(note, index) in table.inspection_metadata" :key="index" class="break-all text-xs">{{ note }}</li></ul></details>
        <label v-for="field in fields.table" :key="field" class="mr-2 inline-block">{{ field }} <input :value="table[field] ?? ''" @input="update('table', ti, ti, field, $event)" class="rounded border border-slate-700 bg-slate-950 p-1"></label>
        <button type="button" @click="remove('table', ti, ti)" class="underline">Remove table</button>
        <div v-for="kind in ['column', 'key', 'index']" :key="kind" class="space-y-2">
          <strong>{{ kind }}s</strong>
          <fieldset v-for="(item, ii) in collection(schema, kind, ti)" :key="item.id" class="rounded border border-slate-800 p-2">
            <legend class="text-xs">{{ kind }} · {{ item.name ?? '(unnamed)' }}<template v-if="showIds"> · ID {{ item.id }}</template></legend>
            <template v-for="field in fields[kind]" :key="field">
              <label v-if="field !== 'column_ids'" class="mr-2 inline-block">{{ field }}
                <select v-if="field === 'auto_increment'" :value="item[field] == null ? 'unknown' : String(item[field])" @input="update(kind, ti, ii, field, $event)" class="rounded border border-slate-700 bg-slate-950 p-1"><option value="unknown">Unknown</option><option value="true">Yes</option><option value="false">No</option></select>
                <input v-else-if="['nullable', 'unique'].includes(field)" type="checkbox" :checked="item[field]" @input="update(kind, ti, ii, field, $event)">
                <select v-else-if="field === 'kind'" :value="item[field]" @input="update(kind, ti, ii, field, $event)" class="rounded border border-slate-700 bg-slate-950 p-1"><option>primary</option><option>unique</option></select>
                <input v-else :value="item[field] ?? ''" @input="update(kind, ti, ii, field, $event)" class="rounded border border-slate-700 bg-slate-950 p-1">
              </label>
              <div v-else class="mr-2 inline-flex flex-wrap items-center gap-2">columns
                <label v-for="column in table.columns" :key="column.id" class="inline-flex items-center gap-1"><input type="checkbox" :checked="item.column_ids.includes(column.id)" @change="toggleColumn(kind, ti, ii, 'column_ids', column.id, $event)">{{ column.name }}</label>
                <span v-for="id in missing(item.column_ids, table.id)" :key="id" class="text-amber-300">missing column ({{ id }})</span>
              </div>
            </template>
            <button type="button" @click="remove(kind, ti, ii)" class="underline">Remove {{ kind }}</button>
          </fieldset>
          <button type="button" @click="add(kind, ti)" class="underline">Add {{ kind }}</button>
        </div>
      </fieldset>
    </details>
    <button type="button" :disabled="disabled" @click="add('table')" class="underline">Add table</button>
    <fieldset :disabled="disabled" class="space-y-2 rounded-lg border border-slate-700 p-3">
      <legend>Relationships</legend>
      <fieldset v-for="(item, ii) in (schema.relationships as Item[])" :key="item.id" class="rounded border border-slate-800 p-2">
        <legend class="text-xs">{{ tableName(item.source_table_id) }} → {{ tableName(item.target_table_id) }}<template v-if="showIds"> · ID {{ item.id }}</template></legend>
        <label class="mr-2 inline-block">name <input :value="item.name ?? ''" @input="update('relationship', 0, ii, 'name', $event)" class="rounded border border-slate-700 bg-slate-950 p-1"></label>
        <template v-for="side in ['source', 'target']" :key="side">
          <label class="mr-2 inline-block">{{ side }} table
            <select :value="item[`${side}_table_id`]" @change="setRelationshipTable(ii, side, $event)" class="rounded border border-slate-700 bg-slate-950 p-1"><option value="" disabled>Choose a table</option><option v-for="table in schema.tables" :key="table.id" :value="table.id">{{ table.name }}</option></select>
          </label>
          <div class="mr-2 inline-flex flex-wrap items-center gap-2">{{ side }} columns
            <label v-for="column in columnsOf(item[`${side}_table_id`])" :key="column.id" class="inline-flex items-center gap-1"><input type="checkbox" :checked="item[`${side}_column_ids`].includes(column.id)" @change="toggleColumn('relationship', 0, ii, `${side}_column_ids`, column.id, $event)">{{ column.name }}</label>
            <span v-for="id in missing(item[`${side}_column_ids`], item[`${side}_table_id`])" :key="id" class="text-amber-300">missing column ({{ id }})</span>
          </div>
        </template>
        <label v-for="field in ['on_delete', 'on_update']" :key="field" class="mr-2 inline-block">{{ field }}
          <select :value="item[field] ?? ''" @change="update('relationship', 0, ii, field, $event)" class="rounded border border-slate-700 bg-slate-950 p-1"><option value="">(none)</option><option v-for="action in actions" :key="action">{{ action }}</option></select>
        </label>
        <button type="button" @click="remove('relationship', 0, ii)" class="underline">Remove relationship</button>
      </fieldset>
      <button type="button" @click="add('relationship')" class="underline">Add relationship</button>
    </fieldset>
  </div>
  <p v-else role="status" class="text-amber-300">Structured controls are unavailable until the JSON has valid schema arrays. Your JSON remains below.</p>
</template>
