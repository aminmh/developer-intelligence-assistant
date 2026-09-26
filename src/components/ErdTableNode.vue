<script setup lang="ts">
import { computed, inject } from 'vue';
import type { Node } from '@antv/x6';
import type { ErdTable } from '../types';

const props = defineProps<{ node?: Node }>();
const injectedNode = inject<() => Node>('getNode', () => props.node as Node);
const node = computed(() => props.node || injectedNode());
const table = computed<ErdTable | null>(() => {
  const data = node.value?.getData() as {
    table?: ErdTable;
    id?: string;
    name?: string;
    columns?: Array<{
      id?: string;
      name: string;
      type: string;
      nullable?: boolean;
      isPk?: boolean;
      isFk?: boolean;
      primaryKey?: boolean;
      foreignKey?: boolean;
    }>;
  } | ErdTable | undefined;
  if (!data) return null;
  if ('table' in data && data.table) return data.table;
  if ('fields' in data && Array.isArray(data.fields)) return data;
  if ('columns' in data && Array.isArray(data.columns)) {
    return {
      id: data.id || data.name || 'table:unknown',
      name: data.name || data.id || 'table',
      fields: data.columns.map((column) => ({
        id: column.id || `${data.id || data.name || 'table'}:${column.name}`,
        name: column.name,
        type: column.type,
        nullable: column.nullable !== false,
        primaryKey: Boolean(column.primaryKey ?? column.isPk),
        foreignKey: Boolean(column.foreignKey ?? column.isFk),
      })),
    };
  }
  return null;
});
const selected = computed(() => Boolean(node.value?.getData()?.selected));
const runtimeOperations = computed(() => Number(node.value?.getData()?.runtimeOperations || 0));
const busy = computed(() => runtimeOperations.value >= 100);
</script>

<template>
  <article v-if="table" class="erd-table-node" :class="{ 'erd-table-node--selected': selected, 'erd-table-node--busy': busy }">
    <header class="erd-table-node__header">
      <span class="erd-table-node__heading">
        <span class="erd-table-node__icon" aria-hidden="true">▦</span>
        <span class="erd-table-node__title" :title="table.name">{{ table.name }}</span>
      </span>
      <span class="erd-table-node__count">{{ busy ? `BUSY · ${runtimeOperations.toLocaleString()}` : `${table.fields.length} cols` }}</span>
    </header>
    <div class="erd-table-node__body">
      <div v-for="field in table.fields" :key="field.id" class="erd-table-node__row">
        <span class="erd-table-node__marker" :class="{ 'is-pk': field.primaryKey, 'is-fk': !field.primaryKey && field.foreignKey }">{{ field.primaryKey ? '◆' : field.foreignKey ? '↳' : '·' }}</span>
        <span class="erd-table-node__field" :title="`${field.name}: ${field.type}`">{{ field.name }}: {{ field.type }}<span v-if="field.nullable" class="erd-table-node__nullable">null</span></span>
      </div>
    </div>
  </article>
</template>

<style scoped>
.erd-table-node {
  width: 100%;
  height: 100%;
  overflow: hidden;
  box-sizing: border-box;
  border: 1px solid #334155;
  border-radius: 8px;
  background: #11182d;
  color: #e2e8f0;
  box-shadow: 0 12px 24px rgba(0, 0, 0, 0.2);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.erd-table-node--selected {
  border-color: #a5b4fc;
  border-width: 2px;
}

.erd-table-node--busy {
  border-color: #f97316;
  box-shadow: 0 0 0 2px rgba(249, 115, 22, 0.24), 0 12px 24px rgba(0, 0, 0, 0.2);
}

.erd-table-node--busy .erd-table-node__header {
  background: #431407;
}

.erd-table-node__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  box-sizing: border-box;
  height: 38px;
  padding: 0 14px;
  overflow: hidden;
  border-bottom: 1px solid #334155;
  border-radius: 7px 7px 0 0;
  background: #1e293b;
  font-family: ui-sans-serif, system-ui, sans-serif;
}

.erd-table-node__title,
.erd-table-node__field {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.erd-table-node__heading {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 6px;
}

.erd-table-node__icon {
  flex: 0 0 auto;
  color: #818cf8;
  font-size: 12px;
}

.erd-table-node__title {
  font-size: 13px;
  font-weight: 600;
}

.erd-table-node__count {
  flex: 0 0 auto;
  margin-left: 8px;
  color: #94a3b8;
  font-size: 9px;
}

.erd-table-node__body {
  height: calc(100% - 38px);
  overflow: hidden;
}

.erd-table-node__row {
  display: flex;
  align-items: center;
  box-sizing: border-box;
  height: 28px;
  min-width: 0;
  padding: 0 14px;
  border-bottom: 1px solid #1e293b;
  font-size: 11px;
}

.erd-table-node__row:nth-child(even) {
  background: #0f172a;
}

.erd-table-node__marker {
  flex: 0 0 14px;
  margin-right: 4px;
  color: #64748b;
  text-align: center;
}

.erd-table-node__marker.is-pk {
  color: #fcd34d;
}

.erd-table-node__marker.is-fk {
  color: #818cf8;
}

.erd-table-node__field {
  flex: 1 1 auto;
  color: #e2e8f0;
}

.erd-table-node__nullable {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 4px;
  border: 1px solid #475569;
  border-radius: 3px;
  color: #94a3b8;
  font-size: 9px;
}
</style>
