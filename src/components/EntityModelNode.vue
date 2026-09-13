<script setup lang="ts">
import { computed, inject } from 'vue';
import type { Node } from '@antv/x6';
import type { DomainEntity, DomainEntityProperty } from '../types';

const props = defineProps<{ node?: Node }>();
const injectedNode = inject<() => Node>('getNode', () => props.node as Node);
const node = computed(() => props.node || injectedNode());
const data = computed(() => (node.value?.getData?.() || {}) as Record<string, unknown>);
const entity = computed<DomainEntity | null>(() => {
  const value = data.value.entity as DomainEntity | undefined;
  if (value) return value;
  if (typeof data.value.name !== 'string') return null;
  return {
    name: data.value.name,
    namespace: typeof data.value.namespace === 'string' ? data.value.namespace : null,
    table: typeof data.value.table === 'string' ? data.value.table : null,
    framework: typeof data.value.framework === 'string' ? data.value.framework : null,
    stereotype: typeof data.value.stereotype === 'string' ? data.value.stereotype : 'Entity',
    properties: Array.isArray(data.value.fields) ? data.value.fields as DomainEntityProperty[] : [],
    methods: Array.isArray(data.value.methods) ? data.value.methods as any[] : [],
  };
});
const selected = computed(() => Boolean(data.value.selected));
const properties = computed(() => (entity.value?.properties || []).slice(0, 14));
const methods = computed(() => ((entity.value as any)?.methods || (entity.value as any)?.scopes_or_behaviors || []).slice(0, 10));
const propertyType = (property: DomainEntityProperty) => String(property.type || 'unknown');
const visibilityMarker = (visibility?: unknown) => ({ public: '+', protected: '#', private: '-', internal: '~' }[String(visibility || 'public')] || '+');
const methodName = (method: any) => typeof method === 'string' ? method : String(method?.name || 'method');
const methodVisibility = (method: any) => typeof method === 'string' ? '+' : visibilityMarker(method?.visibility);
const methodText = (method: any) => {
  if (typeof method === 'string') return method;
  const params = String(method?.parameters || '');
  const returnType = String(method?.return_type || 'void');
  return `${methodName(method)}(${params}): ${returnType}`;
};
const propertyFlags = (property: DomainEntityProperty) => [
  property.is_primary ?? property.primaryKey ? 'PK' : '',
  property.is_foreign ?? property.foreignKey ? 'FK' : '',
  property.is_nullable ?? property.nullable ? 'null' : '',
].filter(Boolean).join(' ');
</script>

<template>
  <article v-if="entity" class="entity-model-node" :class="{ 'entity-model-node--selected': selected }">
    <header class="entity-model-node__header">
      <div class="entity-model-node__heading">
        <span class="entity-model-node__icon" aria-hidden="true">◇</span>
        <span class="entity-model-node__title" :title="entity.name">{{ entity.name }}</span>
      </div>
      <span class="entity-model-node__stereotype">&lt;&lt;{{ (entity as any).stereotype || 'Entity' }}&gt;&gt;</span>
    </header>
    <div class="entity-model-node__meta" :title="entity.namespace || entity.table || ''">
      {{ entity.namespace || 'class' }}<span v-if="entity.table" class="entity-model-node__table"> · {{ entity.table }}</span>
    </div>
    <div class="entity-model-node__body">
      <div v-for="property in properties" :key="property.name" class="entity-model-node__row">
        <span class="entity-model-node__marker">{{ visibilityMarker(property.visibility) }}</span>
        <span class="entity-model-node__field" :title="`${propertyType(property)}: ${property.name}`">{{ propertyType(property) }}: {{ property.name }}</span>
        <span v-if="property.cast_or_enum" class="entity-model-node__flags">{{ property.cast_or_enum }}</span>
      </div>
      <div v-if="entity.properties.length > properties.length" class="entity-model-node__more">+ {{ entity.properties.length - properties.length }} more</div>
      <div v-if="!entity.properties.length" class="entity-model-node__empty">No attributes recorded</div>
    </div>
    <div class="entity-model-node__methods">
      <div v-for="method in methods" :key="methodName(method)" class="entity-model-node__method">{{ methodVisibility(method) }} {{ methodText(method) }}</div>
      <div v-if="!methods.length" class="entity-model-node__empty">No behaviors recorded</div>
    </div>
  </article>
</template>

<style scoped>
.entity-model-node { width: 100%; height: 100%; overflow: hidden; box-sizing: border-box; border: 1px solid #334155; border-radius: 8px; background: #11182d; color: #e2e8f0; box-shadow: 0 12px 24px rgba(0, 0, 0, .2); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.entity-model-node--selected { border-color: #22d3ee; border-width: 2px; box-shadow: 0 0 16px rgba(34, 211, 238, .26); }
.entity-model-node__header { display: flex; align-items: center; justify-content: space-between; box-sizing: border-box; height: 36px; padding: 0 12px; overflow: hidden; border-bottom: 1px solid #334155; background: #1e293b; font-family: ui-sans-serif, system-ui, sans-serif; }
.entity-model-node__heading { display: flex; align-items: center; min-width: 0; gap: 6px; }
.entity-model-node__icon { flex: 0 0 auto; color: #22d3ee; font-size: 13px; }
.entity-model-node__title, .entity-model-node__field, .entity-model-node__meta { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.entity-model-node__title { font-size: 13px; font-weight: 600; }
.entity-model-node__stereotype { flex: 0 0 auto; margin-left: 6px; color: #a5b4fc; font-size: 9px; font-style: italic; }
.entity-model-node__meta { height: 22px; box-sizing: border-box; padding: 5px 12px 2px; color: #64748b; font-size: 9px; }
.entity-model-node__body { max-height: 260px; overflow: hidden; border-bottom: 1px solid #334155; }
.entity-model-node__row { display: flex; align-items: center; box-sizing: border-box; height: 25px; min-width: 0; padding: 0 10px; border-bottom: 1px solid #1e293b; font-size: 10px; }
.entity-model-node__row:nth-child(even) { background: #0f172a; }
.entity-model-node__marker { flex: 0 0 12px; margin-right: 3px; color: #64748b; text-align: center; }
.entity-model-node__marker.is-pk { color: #fcd34d; }
.entity-model-node__marker.is-fk { color: #818cf8; }
.entity-model-node__field { flex: 1 1 auto; color: #e2e8f0; }
.entity-model-node__flags { flex: 0 0 auto; margin-left: 4px; color: #94a3b8; font-size: 8px; }
.entity-model-node__methods { max-height: 180px; overflow: hidden; }
.entity-model-node__method { box-sizing: border-box; height: 25px; padding: 5px 12px; border-bottom: 1px solid #1e293b; color: #c4b5fd; font-size: 10px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.entity-model-node__more, .entity-model-node__empty { padding: 6px 12px; color: #64748b; font-size: 9px; }
</style>
