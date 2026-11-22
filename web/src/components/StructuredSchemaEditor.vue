<script setup lang="ts">
import { computed, ref } from 'vue';
import { Plus, Trash2 } from 'lucide-vue-next';
import SchemaColumnPicker from './SchemaColumnPicker.vue';

const props = defineProps<{ modelValue: string; disabled: boolean }>();
const emit = defineEmits<{ 'update:modelValue': [value: string] }>();
type Item = Record<string, any>;
const schema = computed<any>(() => {
  try {
    const parsed = JSON.parse(props.modelValue);
    return parsed && Array.isArray(parsed.tables) && Array.isArray(parsed.relationships) && parsed.tables.every((t: Item) => Array.isArray(t.columns) && Array.isArray(t.keys) && Array.isArray(t.indexes)) ? parsed : null;
  } catch { return null; }
});
const showIds = ref(false);
const filter = ref('');
const picked = ref(0);
const actions = ['no action', 'restrict', 'cascade', 'set null', 'set default'];
const tables = computed<Item[]>(() => schema.value?.tables ?? []);
const ti = computed(() => Math.max(0, Math.min(picked.value, tables.value.length - 1)));
const table = computed<Item | undefined>(() => tables.value[ti.value]);
const visibleTables = computed(() => tables.value
  .map((item, index) => ({ item, index }))
  .filter(({ item }) => !filter.value.trim() || item.name.toLowerCase().includes(filter.value.trim().toLowerCase())));
const columnsOf = (tableId: string): Item[] => tables.value.find(t => t.id === tableId)?.columns ?? [];
const title = (item: Item) => (item.namespace ? `${item.namespace}.` : '') + item.name;

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
    item[field] = field === 'auto_increment' ? (input.value === 'unknown' ? null : input.value === 'true') : input.type === 'checkbox' ? input.checked : ['namespace', 'name', 'on_delete', 'on_update', 'default_expression', 'identity'].includes(field) && !input.value && !(kind === 'table' && field === 'name') && !(kind === 'column' && field === 'name') && !(kind === 'index' && field === 'name') ? null : input.value;
  });
}
function setIds(kind: string, tableIndex: number, itemIndex: number, field: string, ids: string[]) {
  commit((value) => { collection(value, kind, tableIndex)[itemIndex][field] = ids; });
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
  if (kind === 'table') picked.value = tables.value.length;
  commit((value) => collection(value, kind, tableIndex).push(defaults[kind]));
}
function remove(kind: string, tableIndex: number, index: number) {
  if (window.confirm(`Remove this ${kind}? References are not removed automatically; review them before saving.`)) commit((value) => { collection(value, kind, tableIndex).splice(index, 1); });
}
</script>

<template>
  <div v-if="schema" class="se">
    <div class="se-top">
      <p class="se-hint">Pick a table to edit its columns, keys and indexes. Relationships are listed below.</p>
      <label class="se-check"><input v-model="showIds" type="checkbox">Show IDs</label>
    </div>

    <div class="se-body">
      <nav class="se-tables" aria-label="Tables">
        <input v-model="filter" class="sc-input" type="search" placeholder="Filter tables" aria-label="Filter tables">
        <ul>
          <li v-for="{ item, index } in visibleTables" :key="item.id">
            <button type="button" class="se-table-btn" :aria-current="index === ti ? 'true' : undefined" @click="picked = index">
              <span class="se-table-name">{{ item.name }}</span><small>{{ item.columns.length }}</small>
            </button>
          </li>
          <li v-if="!visibleTables.length" class="se-hint">No matching tables.</li>
        </ul>
        <button type="button" class="sc-btn sc-btn-sm" :disabled="disabled" @click="add('table')"><Plus aria-hidden="true" />Add table</button>
      </nav>

      <section v-if="table" class="se-detail" :aria-label="`Table ${title(table)}`">
        <fieldset :disabled="disabled" class="se-fieldset">
          <div class="se-head">
            <label>Name<input :value="table.name" class="sc-input" @input="update('table', ti, ti, 'name', $event)"></label>
            <label>Namespace<input :value="table.namespace ?? ''" class="sc-input" @input="update('table', ti, ti, 'namespace', $event)"></label>
            <button type="button" class="sc-btn sc-btn-sm" @click="remove('table', ti, ti)"><Trash2 aria-hidden="true" />Remove table</button>
          </div>
          <p v-if="showIds" class="se-id">ID {{ table.id }}</p>
          <details v-if="table.inspection_metadata?.length" class="se-notes"><summary>Catalog notes ({{ table.inspection_metadata.length }})</summary><ul><li v-for="(note, index) in table.inspection_metadata" :key="index">{{ note }}</li></ul></details>

          <h4 class="se-title">Columns <small>{{ table.columns.length }}</small></h4>
          <div class="se-scroll">
            <table class="se-grid">
              <thead><tr><th>Name</th><th>Type</th><th class="se-narrow">Nullable</th><th>Auto-increment</th><th>Default expression</th><th>Identity</th><th v-if="showIds">ID</th><th class="se-narrow"><span class="sr-only">Actions</span></th></tr></thead>
              <tbody>
                <tr v-for="(column, ci) in (table.columns as Item[])" :key="column.id">
                  <td><input :value="column.name" class="sc-input" aria-label="Column name" @input="update('column', ti, ci, 'name', $event)"></td>
                  <td><input :value="column.type" class="sc-input" aria-label="Column type" @input="update('column', ti, ci, 'type', $event)"></td>
                  <td class="se-center"><input type="checkbox" :checked="column.nullable" aria-label="Nullable" @change="update('column', ti, ci, 'nullable', $event)"></td>
                  <td><select class="sc-input" :value="column.auto_increment == null ? 'unknown' : String(column.auto_increment)" aria-label="Auto-increment" @change="update('column', ti, ci, 'auto_increment', $event)"><option value="unknown">Unknown</option><option value="true">Yes</option><option value="false">No</option></select></td>
                  <td><input :value="column.default_expression ?? ''" class="sc-input" aria-label="Default expression" @input="update('column', ti, ci, 'default_expression', $event)"></td>
                  <td><input :value="column.identity ?? ''" class="sc-input" aria-label="Identity" @input="update('column', ti, ci, 'identity', $event)"></td>
                  <td v-if="showIds" class="se-id-cell">{{ column.id }}</td>
                  <td class="se-center"><button type="button" class="sc-btn sc-btn-sm sc-btn-icon" title="Remove column" aria-label="Remove column" @click="remove('column', ti, ci)"><Trash2 aria-hidden="true" /></button></td>
                </tr>
                <tr v-if="!table.columns.length"><td colspan="8" class="se-empty">No columns.</td></tr>
              </tbody>
            </table>
          </div>
          <button type="button" class="sc-btn sc-btn-sm" @click="add('column', ti)"><Plus aria-hidden="true" />Add column</button>

          <h4 class="se-title">Keys <small>{{ table.keys.length }}</small></h4>
          <div class="se-scroll">
            <table class="se-grid">
              <thead><tr><th>Name</th><th>Kind</th><th>Columns</th><th v-if="showIds">ID</th><th class="se-narrow"><span class="sr-only">Actions</span></th></tr></thead>
              <tbody>
                <tr v-for="(key, ki) in (table.keys as Item[])" :key="key.id">
                  <td><input :value="key.name ?? ''" class="sc-input" aria-label="Key name" @input="update('key', ti, ki, 'name', $event)"></td>
                  <td><select class="sc-input" :value="key.kind" aria-label="Key kind" @change="update('key', ti, ki, 'kind', $event)"><option>primary</option><option>unique</option></select></td>
                  <td><SchemaColumnPicker :columns="table.columns" :model-value="key.column_ids" label="Key columns" :disabled="disabled" @update:model-value="setIds('key', ti, ki, 'column_ids', $event)" /></td>
                  <td v-if="showIds" class="se-id-cell">{{ key.id }}</td>
                  <td class="se-center"><button type="button" class="sc-btn sc-btn-sm sc-btn-icon" title="Remove key" aria-label="Remove key" @click="remove('key', ti, ki)"><Trash2 aria-hidden="true" /></button></td>
                </tr>
                <tr v-if="!table.keys.length"><td colspan="5" class="se-empty">No keys.</td></tr>
              </tbody>
            </table>
          </div>
          <button type="button" class="sc-btn sc-btn-sm" @click="add('key', ti)"><Plus aria-hidden="true" />Add key</button>

          <h4 class="se-title">Indexes <small>{{ table.indexes.length }}</small></h4>
          <div class="se-scroll">
            <table class="se-grid">
              <thead><tr><th>Name</th><th>Columns</th><th class="se-narrow">Unique</th><th v-if="showIds">ID</th><th class="se-narrow"><span class="sr-only">Actions</span></th></tr></thead>
              <tbody>
                <tr v-for="(index, ii) in (table.indexes as Item[])" :key="index.id">
                  <td><input :value="index.name" class="sc-input" aria-label="Index name" @input="update('index', ti, ii, 'name', $event)"></td>
                  <td><SchemaColumnPicker :columns="table.columns" :model-value="index.column_ids" label="Index columns" :disabled="disabled" @update:model-value="setIds('index', ti, ii, 'column_ids', $event)" /></td>
                  <td class="se-center"><input type="checkbox" :checked="index.unique" aria-label="Unique" @change="update('index', ti, ii, 'unique', $event)"></td>
                  <td v-if="showIds" class="se-id-cell">{{ index.id }}</td>
                  <td class="se-center"><button type="button" class="sc-btn sc-btn-sm sc-btn-icon" title="Remove index" aria-label="Remove index" @click="remove('index', ti, ii)"><Trash2 aria-hidden="true" /></button></td>
                </tr>
                <tr v-if="!table.indexes.length"><td colspan="5" class="se-empty">No indexes.</td></tr>
              </tbody>
            </table>
          </div>
          <button type="button" class="sc-btn sc-btn-sm" @click="add('index', ti)"><Plus aria-hidden="true" />Add index</button>
        </fieldset>
      </section>
      <p v-else class="se-hint">This schema has no tables yet. Add one to start.</p>
    </div>

    <section aria-label="Relationships">
      <fieldset :disabled="disabled" class="se-fieldset">
        <h4 class="se-title">Relationships <small>{{ schema.relationships.length }}</small></h4>
        <div class="se-scroll">
          <table class="se-grid">
            <thead><tr><th>Name</th><th>From table</th><th>From columns</th><th>To table</th><th>To columns</th><th>On delete</th><th>On update</th><th v-if="showIds">ID</th><th class="se-narrow"><span class="sr-only">Actions</span></th></tr></thead>
            <tbody>
              <tr v-for="(rel, ri) in (schema.relationships as Item[])" :key="rel.id">
                <td><input :value="rel.name ?? ''" class="sc-input" aria-label="Relationship name" @input="update('relationship', 0, ri, 'name', $event)"></td>
                <td><select class="sc-input" :value="rel.source_table_id" aria-label="From table" @change="setRelationshipTable(ri, 'source', $event)"><option value="" disabled>Choose a table</option><option v-for="t in tables" :key="t.id" :value="t.id">{{ t.name }}</option></select></td>
                <td><SchemaColumnPicker :columns="columnsOf(rel.source_table_id)" :model-value="rel.source_column_ids" label="From columns" :disabled="disabled" @update:model-value="setIds('relationship', 0, ri, 'source_column_ids', $event)" /></td>
                <td><select class="sc-input" :value="rel.target_table_id" aria-label="To table" @change="setRelationshipTable(ri, 'target', $event)"><option value="" disabled>Choose a table</option><option v-for="t in tables" :key="t.id" :value="t.id">{{ t.name }}</option></select></td>
                <td><SchemaColumnPicker :columns="columnsOf(rel.target_table_id)" :model-value="rel.target_column_ids" label="To columns" :disabled="disabled" @update:model-value="setIds('relationship', 0, ri, 'target_column_ids', $event)" /></td>
                <td><select class="sc-input" :value="rel.on_delete ?? ''" aria-label="On delete" @change="update('relationship', 0, ri, 'on_delete', $event)"><option value="">(none)</option><option v-for="action in actions" :key="action">{{ action }}</option></select></td>
                <td><select class="sc-input" :value="rel.on_update ?? ''" aria-label="On update" @change="update('relationship', 0, ri, 'on_update', $event)"><option value="">(none)</option><option v-for="action in actions" :key="action">{{ action }}</option></select></td>
                <td v-if="showIds" class="se-id-cell">{{ rel.id }}</td>
                <td class="se-center"><button type="button" class="sc-btn sc-btn-sm sc-btn-icon" title="Remove relationship" aria-label="Remove relationship" @click="remove('relationship', 0, ri)"><Trash2 aria-hidden="true" /></button></td>
              </tr>
              <tr v-if="!schema.relationships.length"><td colspan="9" class="se-empty">No relationships.</td></tr>
            </tbody>
          </table>
        </div>
        <button type="button" class="sc-btn sc-btn-sm" @click="add('relationship')"><Plus aria-hidden="true" />Add relationship</button>
      </fieldset>
    </section>
  </div>
  <p v-else role="status" class="se-hint" data-tone="warn">Structured controls are unavailable until the JSON has valid schema arrays. Your JSON remains below.</p>
</template>

<style scoped>
.se { display: grid; gap: 1rem; min-width: 0; }
.se-top { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 0.75rem; }
.se-hint { margin: 0; font-size: 0.78125rem; color: var(--shell-text-2); }
.se-hint[data-tone="warn"] { color: #fcd34d; }
.se-check { display: inline-flex; align-items: center; gap: 0.375rem; font-size: 0.78125rem; color: var(--shell-text-2); cursor: pointer; }
.se-body { display: grid; grid-template-columns: minmax(11rem, 14rem) minmax(0, 1fr); gap: 1rem; align-items: start; }
@media (max-width: 900px) { .se-body { grid-template-columns: minmax(0, 1fr); } }
.se-tables { display: grid; gap: 0.5rem; }
.se-tables ul { display: grid; gap: 0.125rem; margin: 0; padding: 0; list-style: none; max-height: 28rem; overflow: auto; }
.se-table-btn { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; width: 100%; padding: 0.4375rem 0.625rem; border: 1px solid transparent; border-radius: 0.5rem; background: transparent; color: var(--shell-text-2); font-size: 0.78125rem; text-align: left; cursor: pointer; }
.se-table-btn:hover { background: rgb(255 255 255 / 0.05); }
.se-table-btn[aria-current="true"] { border-color: rgb(var(--shell-grad-a-rgb) / 0.5); background: rgb(var(--shell-grad-a-rgb) / 0.12); color: var(--shell-text); }
.se-table-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--font-mono); }
.se-table-btn small { flex-shrink: 0; opacity: 0.6; }
.se-detail { min-width: 0; padding: 0.875rem; border: 1px solid var(--shell-line); border-radius: 0.75rem; }
.se-fieldset { display: grid; gap: 0.625rem; min-width: 0; margin: 0; padding: 0; border: 0; }
.se-head { display: flex; flex-wrap: wrap; align-items: end; gap: 0.75rem; }
.se-head label { display: grid; gap: 0.25rem; font-size: 0.6875rem; letter-spacing: 0.04em; text-transform: uppercase; color: var(--shell-text-2); }
.se-title { display: flex; align-items: baseline; gap: 0.5rem; margin: 0.5rem 0 0; font-size: 0.8125rem; font-weight: 600; color: var(--shell-text); }
.se-title small { font-weight: 400; color: var(--shell-text-2); }
.se-id, .se-id-cell { font-family: var(--font-mono); font-size: 0.6875rem; color: var(--shell-text-2); overflow-wrap: anywhere; }
.se-id { margin: 0; }
.se-notes summary { cursor: pointer; font-size: 0.78125rem; color: rgb(var(--shell-grad-a-rgb)); }
.se-notes ul { margin: 0.375rem 0 0; padding-left: 1.125rem; font-size: 0.6875rem; color: var(--shell-text-2); overflow-wrap: anywhere; }
.se-scroll { overflow-x: auto; border: 1px solid var(--shell-line); border-radius: 0.625rem; }
.se-grid { width: 100%; min-width: 38rem; border-collapse: collapse; }
.se-grid th { padding: 0.5rem; border-bottom: 1px solid var(--shell-line); font-size: 0.6875rem; font-weight: 600; letter-spacing: 0.04em; text-align: left; text-transform: uppercase; color: var(--shell-text-2); white-space: nowrap; }
.se-grid td { padding: 0.3125rem 0.375rem; border-bottom: 1px solid var(--shell-line); vertical-align: middle; }
.se-grid tbody tr:last-child td { border-bottom: 0; }
.se-grid .sc-input { width: 100%; height: 1.875rem; }
.se-narrow { width: 1%; white-space: nowrap; }
.se-center { text-align: center; }
.se-empty { padding: 0.75rem; text-align: center; font-size: 0.78125rem; color: var(--shell-text-2); }
</style>
