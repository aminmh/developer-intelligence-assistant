<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { AlertTriangle, BookOpen, ChevronDown, ChevronRight, Copy, Download, Focus, Link2, Maximize2, Minus, Network, Plus, RefreshCw, Search, Send, Sparkles, Tag, X } from 'lucide-vue-next';
import { VueFlow, useVueFlow, type Edge, type Node } from '@vue-flow/core';
import '@vue-flow/core/dist/style.css';
import '@vue-flow/core/dist/theme-default.css';
import { makeRequest } from '../ui/makeRequest';
import type {
  AnalysisSession,
  DomainConcept,
  DomainConceptsResponse,
  DomainContextResponse,
  DomainDictionaryResponse,
  DomainEntity,
  DomainEntityBehavior,
  DomainEntityRelationship,
  DomainBehaviorUsagesResponse,
  DomainModelResponse,
  DomainRelationship,
} from '../types';
import EntityModelCanvas from './EntityModelCanvas.vue';

const emit = defineEmits<{
  (event: 'askAboutConcept', concept: DomainConcept, analysis: AnalysisSession): void;
}>();

const analyses = ref<AnalysisSession[]>([]);
const selectedAnalysisId = ref('');
const concepts = ref<DomainConcept[]>([]);
const dictionary = ref<Array<{ term: string; count: number }>>([]);
const context = ref<DomainContextResponse | null>(null);
const selectedConceptId = ref('');
const search = ref('');
const conceptType = ref('');
const loadingAnalyses = ref(false);
const loadingConcepts = ref(false);
const loadingContext = ref(false);
const error = ref('');
const dictionaryExpanded = ref(false);
const mode = ref<'concepts' | 'entity-model'>('concepts');
const entityModel = ref<DomainModelResponse | null>(null);
const selectedEntityId = ref('');
const selectedRelationshipId = ref('');
const selectedBehavior = ref<DomainEntityBehavior | null>(null);
const behaviorUsages = ref<DomainBehaviorUsagesResponse | null>(null);
const loadingBehaviorUsages = ref(false);
const loadingMoreBehaviorUsages = ref(false);
const loadingEntityModel = ref(false);
const enrichingEntityModel = ref(false);
const entityCanvas = ref<InstanceType<typeof EntityModelCanvas> | null>(null);

const { fitView } = useVueFlow('domain-knowledge');

const selectedAnalysis = computed(() => analyses.value.find((analysis) => analysis.id === selectedAnalysisId.value) || null);
const selectedFramework = computed(() => {
  const framework = selectedAnalysis.value?.profile?.framework;
  if (typeof framework === 'string') return framework;
  if (framework && typeof framework === 'object') {
    return String((framework as any).name || (framework as any).id || '');
  }
  return '';
});
const isLaravelAnalysis = computed(() => !selectedFramework.value || /laravel/i.test(selectedFramework.value));
const selectedConcept = computed(() => concepts.value.find((concept) => concept.identity_key === selectedConceptId.value)
  || context.value?.recognized.find((concept) => concept.identity_key === selectedConceptId.value)
  || null);
const selectedEntity = computed(() => entityModel.value?.entities.find((entity) => (entity.id || `entity:${entity.name}`) === selectedEntityId.value) || null);
const selectedBehaviorEntity = computed(() => selectedBehavior.value ? (entityModel.value?.entities.find((entity) => entity.name === selectedBehavior.value?.owner?.split('\\').pop()) || selectedEntity.value) : null);
const selectedEntityRelationships = computed(() => selectedEntity.value?.relationships || []);
const selectedEntityRelationship = computed<DomainEntityRelationship | null>(() => {
  if (!entityModel.value || !selectedRelationshipId.value) return null;
  for (const entity of entityModel.value.entities) {
    const found = (entity.relationships || []).find((relationship) => relationship.id === selectedRelationshipId.value);
    if (found) return found;
  }
  const edge = (entityModel.value.relationships || []).find((relationship: any) => relationship.id === selectedRelationshipId.value);
  if (!edge) return null;
  return {
    ...edge,
    source_entity: edge.source_entity || (edge as any).sourceEntity,
    target_entity: edge.target_entity || (edge as any).targetEntity,
    name: edge.name || edge.relation_name,
  };
});
const conceptTypes = computed(() => [...new Set(concepts.value.map((concept) => concept.concept_type).filter(Boolean))].sort());
const visibleConcepts = computed(() => {
  const query = search.value.trim().toLowerCase();
  return concepts.value.filter((concept) => {
    if (conceptType.value && concept.concept_type !== conceptType.value) return false;
    if (!query) return true;
    const haystack = [
      concept.canonical_name,
      concept.qualified_name,
      concept.identity_key,
      ...(concept.roles || []),
      ...(concept.aliases || []).map((alias) => alias.alias),
    ].filter(Boolean).join(' ').toLowerCase();
    return haystack.includes(query);
  });
});
const graphConcepts = computed(() => {
  const related = context.value?.recognized || [];
  const map = new Map<string, DomainConcept>();
  for (const concept of visibleConcepts.value) map.set(concept.identity_key, concept);
  for (const concept of related) map.set(concept.identity_key, concept);
  for (const relationship of context.value?.relationships || []) {
    for (const endpoint of [
      { identity: relationship.source_identity, details: relationship.source },
      { identity: relationship.target_identity, details: relationship.target },
    ]) {
      if (!endpoint.identity || map.has(endpoint.identity)) continue;
      const details = endpoint.details as { canonical_name?: string } | undefined;
      map.set(endpoint.identity, {
        identity_key: endpoint.identity,
        canonical_name: details?.canonical_name || endpoint.identity,
        qualified_name: endpoint.identity,
        concept_type: 'related concept',
        roles: [],
      });
    }
  }
  if (selectedConcept.value) map.set(selectedConcept.value.identity_key, selectedConcept.value);
  return [...map.values()].slice(0, 80);
});
const graphNodeId = (identity: string) => `domain:${identity}`;
const flowNodes = computed<Node[]>(() => graphConcepts.value.map((concept, index) => ({
  id: graphNodeId(concept.identity_key),
  type: 'domainConcept',
  position: { x: (index % 4) * 260, y: Math.floor(index / 4) * 130 },
  class: concept.identity_key === selectedConceptId.value ? 'domain-node-selected' : '',
  data: concept,
})));
const flowEdges = computed<Edge[]>(() => {
  const visible = new Set(graphConcepts.value.map((concept) => concept.identity_key));
  return (context.value?.relationships || []).filter((relationship) => (
    visible.has(relationship.source_identity) && visible.has(relationship.target_identity)
  )).map((relationship, index) => ({
    id: relationship.id || `domain-relation:${relationship.source_identity}:${relationship.target_identity}:${index}`,
    source: graphNodeId(relationship.source_identity),
    target: graphNodeId(relationship.target_identity),
    label: relationship.relation_type,
    data: relationship,
  }));
});

const loadAnalyses = async () => {
  loadingAnalyses.value = true;
  error.value = '';
  try {
    const response = await makeRequest('/api/analysis?limit=100');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || data.error || 'Failed to load saved analyses.');
    analyses.value = data.analyses || [];
    if (!analyses.value.some((analysis: AnalysisSession) => analysis.id === selectedAnalysisId.value)) {
      selectedAnalysisId.value = analyses.value[0]?.id || '';
    }
  } catch (cause: any) {
    error.value = cause?.message || 'Failed to load saved analyses.';
  } finally {
    loadingAnalyses.value = false;
  }
};

const loadDictionary = async () => {
  try {
    const params = new URLSearchParams({ limit: '16' });
    if (search.value.trim()) params.set('search', search.value.trim());
    const response = await makeRequest(`/api/domain/dictionary?${params.toString()}`);
    const data = await response.json() as DomainDictionaryResponse & { detail?: string };
    if (!response.ok) throw new Error(data.detail || 'Failed to load domain vocabulary.');
    const rawItems = (data as any).items;
    const normalizedItems = Array.isArray(rawItems)
      ? rawItems
      : rawItems && typeof rawItems === 'object'
        ? Object.entries(rawItems).map(([term, count]) => ({ term, count }))
        : [];
    dictionary.value = normalizedItems
      .map((item: any) => ({
        term: typeof item === 'string' ? item : String(item?.term ?? item?.name ?? ''),
        count: Number(item?.count ?? item?.frequency ?? 0) || 0,
      }))
      .filter((item) => item.term);
  } catch {
    dictionary.value = [];
  }
};

const loadConcepts = async () => {
  if (!selectedAnalysisId.value) {
    concepts.value = [];
    context.value = null;
    return;
  }
  loadingConcepts.value = true;
  error.value = '';
  selectedConceptId.value = '';
  context.value = null;
  try {
    const params = new URLSearchParams({ limit: '200' });
    if (search.value.trim()) params.set('search', search.value.trim());
    if (conceptType.value) params.set('concept_type', conceptType.value);
    const response = await makeRequest(`/api/analysis/${encodeURIComponent(selectedAnalysisId.value)}/domain-concepts?${params.toString()}`);
    const data = await response.json() as DomainConceptsResponse & { detail?: string };
    if (!response.ok) throw new Error(data.detail || 'Failed to load domain concepts.');
    concepts.value = data.items || [];
    if (concepts.value.length) selectedConceptId.value = concepts.value[0].identity_key;
  } catch (cause: any) {
    concepts.value = [];
    error.value = cause?.message || 'Failed to load domain concepts.';
  } finally {
    loadingConcepts.value = false;
    await nextTick();
    fitView({ padding: 0.2 });
  }
};

const loadEntityModel = async () => {
  if (!selectedAnalysisId.value) {
    entityModel.value = null;
    selectedEntityId.value = '';
    selectedRelationshipId.value = '';
    return;
  }
  loadingEntityModel.value = true;
  error.value = '';
  selectedEntityId.value = '';
  selectedRelationshipId.value = '';
  selectedBehavior.value = null;
  behaviorUsages.value = null;
  try {
    const response = await makeRequest(`/api/analysis/${encodeURIComponent(selectedAnalysisId.value)}/domain-model`);
    const data = await response.json() as DomainModelResponse & { detail?: string };
    if (!response.ok) throw new Error(data.detail || 'Failed to load entity model.');
    entityModel.value = { ...data, entities: Array.isArray(data.entities) ? data.entities : [], plantuml: data.plantuml || '' };
    const first = entityModel.value.entities[0];
    if (first) selectedEntityId.value = first.id || `entity:${first.name}`;
  } catch (cause: any) {
    entityModel.value = null;
    error.value = cause?.message || 'Failed to load entity model.';
  } finally {
    loadingEntityModel.value = false;
  }
};

const enrichEntityModel = async () => {
  if (!selectedAnalysisId.value || enrichingEntityModel.value) return;
  enrichingEntityModel.value = true;
  error.value = '';
  try {
    const response = await makeRequest(`/api/analysis/${encodeURIComponent(selectedAnalysisId.value)}/domain-model/enrich`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
    const data = await response.json() as DomainModelResponse & { model?: DomainModelResponse; detail?: string };
    if (!response.ok) throw new Error(data.detail || 'Entity model enrichment failed.');
    const nextModel = data.model || data;
    entityModel.value = { ...nextModel, entities: Array.isArray(nextModel.entities) ? nextModel.entities : entityModel.value?.entities || [], plantuml: nextModel.plantuml || entityModel.value?.plantuml || '', enriched: true };
  } catch (cause: any) {
    error.value = cause?.message || 'Entity model enrichment failed; deterministic data is still available.';
  } finally {
    enrichingEntityModel.value = false;
  }
};

const selectEntity = (entity: DomainEntity) => { selectedEntityId.value = entity.id || `entity:${entity.name}`; selectedRelationshipId.value = ''; selectedBehavior.value = null; behaviorUsages.value = null; };
const selectEntityRelationship = (relationship: DomainEntityRelationship) => { selectedRelationshipId.value = relationship.id || ''; selectedEntityId.value = ''; selectedBehavior.value = null; behaviorUsages.value = null; };
const loadBehaviorUsages = async (entity: DomainEntity, behavior: DomainEntityBehavior, append = false) => {
  if (!selectedAnalysisId.value || !behavior) return;
  if (append) loadingMoreBehaviorUsages.value = true; else loadingBehaviorUsages.value = true;
  error.value = '';
  try {
    const offset = append ? (behaviorUsages.value?.next_offset || 0) : 0;
    const params = new URLSearchParams({ entity: entity.name, behavior: behavior.behavior_key || behavior.name, offset: String(offset), limit: '25' });
    const response = await makeRequest(`/api/analysis/${encodeURIComponent(selectedAnalysisId.value)}/domain-model/usages?${params.toString()}`);
    const data = await response.json() as DomainBehaviorUsagesResponse & { detail?: string };
    if (!response.ok) throw new Error(data.detail || 'Failed to load behavior usages.');
    behaviorUsages.value = append && behaviorUsages.value
      ? { ...data, usages: [...behaviorUsages.value.usages, ...(data.usages || [])], offset: 0 }
      : data;
  } catch (cause: any) {
    error.value = cause?.message || 'Failed to load behavior usages.';
    if (!append) behaviorUsages.value = null;
  } finally {
    loadingBehaviorUsages.value = false;
    loadingMoreBehaviorUsages.value = false;
  }
};
const selectEntityBehavior = (entity: DomainEntity, behavior: DomainEntityBehavior) => {
  selectedEntityId.value = '';
  selectedRelationshipId.value = '';
  selectedBehavior.value = behavior;
  behaviorUsages.value = null;
  loadBehaviorUsages(entity, behavior);
};
const downloadPlantuml = () => {
  if (!entityModel.value?.plantuml) return;
  const url = URL.createObjectURL(new Blob([entityModel.value.plantuml], { type: 'text/plain' }));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = `domain-model-${selectedAnalysisId.value || 'analysis'}.puml`; anchor.click(); URL.revokeObjectURL(url);
};
const copyPlantuml = async () => {
  if (!entityModel.value?.plantuml) return;
  try { await navigator.clipboard.writeText(entityModel.value.plantuml); } catch { error.value = 'Could not copy PlantUML source to the clipboard.'; }
};

const loadContext = async (concept?: DomainConcept) => {
  if (!selectedAnalysisId.value || !concept) return;
  loadingContext.value = true;
  error.value = '';
  try {
    const params = new URLSearchParams({ concept_id: concept.identity_key });
    const response = await makeRequest(`/api/analysis/${encodeURIComponent(selectedAnalysisId.value)}/domain-context?${params.toString()}`);
    const data = await response.json() as DomainContextResponse & { detail?: string };
    if (!response.ok) throw new Error(data.detail || 'Failed to load domain context.');
    context.value = data;
    await nextTick();
    fitView({ padding: 0.2, duration: 250 });
  } catch (cause: any) {
    error.value = cause?.message || 'Failed to load domain context.';
  } finally {
    loadingContext.value = false;
  }
};

const selectConcept = (concept: DomainConcept) => {
  selectedConceptId.value = concept.identity_key;
  loadContext(concept);
};
const askAboutConcept = () => {
  if (selectedConcept.value && selectedAnalysis.value) emit('askAboutConcept', selectedConcept.value, selectedAnalysis.value);
};
const relationshipLabel = (relationship: DomainRelationship) => relationship.relation_type.replaceAll('_', ' ').toLowerCase();

watch(selectedAnalysisId, () => { loadConcepts(); if (mode.value === 'entity-model') loadEntityModel(); });
watch(mode, (value) => { if (value === 'entity-model') loadEntityModel(); });
watch([search, conceptType], () => { if (mode.value === 'concepts') loadConcepts(); loadDictionary(); });
onMounted(() => { loadAnalyses(); loadDictionary(); });
</script>

<template>
  <section class="flex-1 min-h-0 overflow-hidden p-5 flex flex-col gap-4">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h3 class="text-lg font-bold text-white flex items-center gap-2"><Network class="w-5 h-5 text-cyan-400" /> Domain Knowledge</h3>
        <p class="text-xs text-slate-500 mt-1">Explore verified concepts, relationships, and bounded evidence from a saved project.</p>
      </div>
      <button @click="loadAnalyses" class="px-3 py-2 rounded-xl border border-slate-700 text-xs text-slate-300 hover:bg-slate-800"><RefreshCw :class="loadingAnalyses ? 'animate-spin' : ''" class="w-3.5 h-3.5 inline mr-1" />Refresh</button>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-[260px_minmax(0,1fr)_auto] gap-2 rounded-2xl border border-slate-800 bg-slate-900/30 p-3">
      <select v-model="selectedAnalysisId" class="bg-[#11162d] border border-slate-800 rounded-xl px-3 py-2 text-xs text-slate-300"><option value="">Select saved analysis</option><option v-for="analysis in analyses" :key="analysis.id" :value="analysis.id">{{ analysis.label || analysis.filename }} · {{ analysis.created_at }}</option></select>
      <div class="flex flex-wrap items-center gap-2">
        <div class="flex rounded-xl border border-slate-800 bg-[#11162d] p-0.5">
          <button type="button" @click="mode = 'concepts'" class="rounded-lg px-3 py-1.5 text-[11px]" :class="mode === 'concepts' ? 'bg-cyan-500/20 text-cyan-200' : 'text-slate-500 hover:text-slate-300'">Concepts</button>
          <button type="button" @click="mode = 'entity-model'" class="rounded-lg px-3 py-1.5 text-[11px]" :class="mode === 'entity-model' ? 'bg-cyan-500/20 text-cyan-200' : 'text-slate-500 hover:text-slate-300'">Entity Model</button>
        </div>
        <div class="relative min-w-[180px] flex-1"><Search class="absolute left-3 top-2.5 w-3.5 h-3.5 text-slate-500" /><input v-model="search" :placeholder="mode === 'concepts' ? 'Search concept, alias, role…' : 'Search entity, table, property…'" class="w-full bg-[#11162d] border border-slate-800 rounded-xl pl-9 pr-3 py-2 text-xs text-slate-200" /></div>
      </div>
      <select v-if="mode === 'concepts'" v-model="conceptType" class="bg-[#11162d] border border-slate-800 rounded-xl px-3 py-2 text-xs text-slate-300"><option value="">All concept types</option><option v-for="type in conceptTypes" :key="type" :value="type">{{ type }}</option></select>
      <div v-else class="flex items-center gap-1"><button type="button" @click="enrichEntityModel" :disabled="enrichingEntityModel || !entityModel" class="rounded-xl border border-cyan-400/30 bg-cyan-500/10 px-3 py-2 text-xs text-cyan-200 hover:bg-cyan-500/20 disabled:opacity-50">{{ enrichingEntityModel ? 'Enriching…' : 'Enrich model' }}</button></div>
    </div>

    <div v-if="error && (!entityModel || mode === 'concepts')" class="rounded-xl border border-rose-500/30 bg-rose-950/20 p-4 text-xs text-rose-300">{{ error }}</div>
    <div v-else-if="!selectedAnalysisId" class="flex-1 rounded-2xl border border-dashed border-slate-700 p-10 text-center text-sm text-slate-500 flex items-center justify-center">Choose a saved analysis to browse its domain knowledge.</div>
    <template v-else-if="mode === 'concepts'">
    <div v-if="loadingConcepts" class="flex-1 rounded-2xl border border-slate-800 bg-slate-900/30 flex items-center justify-center text-sm text-slate-500">Loading domain concepts…</div>
    <div v-else-if="!concepts.length" class="flex-1 rounded-2xl border border-dashed border-slate-700 p-10 text-center text-sm text-slate-500 flex items-center justify-center">{{ isLaravelAnalysis ? 'No canonical domain concepts were indexed for this analysis.' : 'Domain Knowledge currently supports Laravel analyses only.' }}</div>
    <div v-else class="flex-1 min-h-0 grid grid-cols-1 xl:grid-cols-[260px_minmax(0,1fr)_340px] gap-3">
      <div class="min-h-0 overflow-y-auto rounded-2xl border border-slate-800 bg-slate-900/30">
        <div class="px-3 py-2 border-b border-slate-800 text-[10px] uppercase tracking-wide text-slate-500">{{ visibleConcepts.length }} concept(s)</div>
        <button v-for="concept in visibleConcepts" :key="concept.identity_key" @click="selectConcept(concept)" class="w-full text-left px-3 py-3 border-b border-slate-800/70 hover:bg-slate-800/40" :class="selectedConceptId === concept.identity_key ? 'bg-cyan-500/10 border-l-2 border-l-cyan-400' : ''">
          <div class="flex items-center gap-2"><Tag class="w-3.5 h-3.5 text-cyan-300 shrink-0" /><span class="text-xs font-semibold text-slate-200 truncate">{{ concept.canonical_name }}</span></div>
          <div class="mt-1 text-[10px] text-slate-500 truncate">{{ concept.concept_type }} · {{ concept.relationship_count || 0 }} relation(s)</div>
        </button>
      </div>

      <div class="min-h-0 rounded-2xl border border-slate-800 bg-[#0b1020] overflow-hidden relative">
        <div class="absolute top-3 left-3 z-10 text-[10px] text-slate-400 bg-slate-950/80 border border-slate-800 rounded-lg px-2 py-1">{{ graphConcepts.length }} graph node(s) · {{ flowEdges.length }} relationship(s)</div>
        <button @click="fitView({ padding: 0.2 })" title="Fit graph" class="absolute top-3 right-3 z-10 p-2 rounded-lg border border-slate-700 text-slate-300 bg-slate-950/80 hover:bg-slate-800"><Maximize2 class="w-3.5 h-3.5" /></button>
        <VueFlow id="domain-knowledge" :nodes="flowNodes" :edges="flowEdges" :fit-view-on-init="true" :nodes-draggable="true" :nodes-connectable="false" @node-click="({ node }) => selectConcept(node.data as DomainConcept)">
          <template #node-domainConcept="{ data }">
            <article class="w-[220px] rounded-xl border border-slate-700 bg-[#11182d] shadow-lg shadow-black/20 px-3 py-2">
              <div class="flex items-center gap-2"><Tag class="w-3.5 h-3.5 text-cyan-300" /><span class="text-xs font-semibold text-white truncate">{{ data.canonical_name }}</span></div>
              <p class="mt-1 text-[10px] text-slate-500 truncate">{{ data.concept_type }}{{ data.roles?.length ? ' · ' + data.roles.join(', ') : '' }}</p>
            </article>
          </template>
        </VueFlow>
      </div>

      <aside class="min-h-0 overflow-y-auto rounded-2xl border border-slate-800 bg-slate-900/30 p-3 space-y-3">
        <div v-if="selectedConcept" class="rounded-2xl border border-cyan-500/20 bg-cyan-950/10 p-3">
          <div class="flex items-start justify-between gap-2"><div><h4 class="text-sm font-semibold text-white">{{ selectedConcept.canonical_name }}</h4><p class="mt-1 text-[10px] text-slate-500 break-all">{{ selectedConcept.qualified_name || selectedConcept.identity_key }}</p></div><button @click="selectedConceptId = ''" class="text-slate-500 hover:text-slate-300"><X class="w-3.5 h-3.5" /></button></div>
          <p v-if="selectedConcept.description" class="mt-3 text-xs text-slate-300">{{ selectedConcept.description }}</p>
          <div class="mt-3 flex flex-wrap gap-1"><span class="rounded-md bg-cyan-500/10 px-2 py-1 text-[10px] text-cyan-200">{{ selectedConcept.concept_type }}</span><span v-for="role in selectedConcept.roles || []" :key="role" class="rounded-md bg-slate-800 px-2 py-1 text-[10px] text-slate-300">{{ role }}</span></div>
          <div v-if="selectedConcept.aliases?.length" class="mt-3 text-[11px] text-slate-400"><span class="text-slate-500">Aliases:</span> {{ selectedConcept.aliases.map((alias) => alias.alias).join(', ') }}</div>
          <button @click="askAboutConcept" class="mt-4 w-full rounded-lg bg-cyan-600/90 px-3 py-2 text-xs font-semibold text-white hover:bg-cyan-500"><Send class="mr-1 inline h-3.5 w-3.5" />Explain with domain context</button>
        </div>

        <div v-if="context?.warnings?.length || context?.degraded || context?.truncated" class="rounded-2xl border border-amber-500/30 bg-amber-950/20 p-3"><h4 class="text-xs font-semibold text-amber-200 flex items-center gap-2"><AlertTriangle class="w-3.5 h-3.5" /> Context notes</h4><p v-if="context.degraded" class="mt-2 text-[11px] text-amber-100/80">Semantic or project evidence was degraded; only verified partial context is shown.</p><p v-if="context.truncated" class="mt-2 text-[11px] text-amber-100/80">Context was capped to keep the LLM prompt bounded.</p><p v-for="warning in context?.warnings || []" :key="warning" class="mt-2 text-[11px] text-amber-100/80">{{ warning }}</p></div>

        <div v-if="context?.evidence?.length" class="rounded-2xl border border-slate-800 bg-slate-900/40 p-3"><h4 class="text-xs font-semibold text-slate-300 flex items-center gap-2"><BookOpen class="w-3.5 h-3.5 text-indigo-300" /> Evidence</h4><div v-for="evidence in context.evidence" :key="`${evidence.kind}-${evidence.file}-${evidence.start_line}`" class="mt-3 rounded-lg border border-slate-800 bg-slate-950/50 p-2"><div class="text-[10px] text-slate-500">{{ evidence.kind }} · {{ evidence.file || 'indexed context' }}{{ evidence.start_line ? ':' + evidence.start_line : '' }}</div><pre v-if="evidence.excerpt" class="mt-2 max-h-40 overflow-auto whitespace-pre-wrap text-[10px] leading-relaxed text-slate-300">{{ evidence.excerpt }}</pre><p v-else-if="evidence.name" class="mt-2 text-[11px] text-slate-300">{{ evidence.name }}</p></div></div>

        <div v-if="context?.analysis_context" class="rounded-2xl border border-indigo-500/20 bg-indigo-950/10 p-3"><h4 class="text-xs font-semibold text-indigo-200 flex items-center gap-2"><BookOpen class="w-3.5 h-3.5" /> Saved analysis facts</h4><pre class="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-[10px] leading-relaxed text-slate-300">{{ context.analysis_context }}</pre></div>

        <div v-if="context?.relationships?.length" class="rounded-2xl border border-slate-800 bg-slate-900/40 p-3"><h4 class="text-xs font-semibold text-slate-300 flex items-center gap-2"><Link2 class="w-3.5 h-3.5 text-indigo-300" /> Related concepts</h4><div v-for="relationship in context.relationships" :key="relationship.id || relationship.source_identity + relationship.target_identity" class="mt-2 text-[11px] text-slate-400">{{ relationship.source_identity }} <span class="text-cyan-300">{{ relationshipLabel(relationship) }}</span> {{ relationship.target_identity }}</div></div>

        <div class="rounded-2xl border border-slate-800 bg-slate-900/40 p-3"><button @click="dictionaryExpanded = !dictionaryExpanded" class="flex w-full items-center justify-between text-left"><span class="text-xs font-semibold text-slate-300 flex items-center gap-2"><BookOpen class="w-3.5 h-3.5 text-amber-300" /> Vocabulary index</span><ChevronDown v-if="dictionaryExpanded" class="h-3.5 w-3.5 text-slate-400" /><ChevronRight v-else class="h-3.5 w-3.5 text-slate-400" /></button><p v-if="dictionaryExpanded" class="mt-2 text-[10px] text-slate-500">Global typo-correction vocabulary from domain_dictionary.txt; frequency is not business meaning.</p><div v-if="dictionaryExpanded" class="mt-2 space-y-1"><div v-for="term in dictionary" :key="term.term" class="flex justify-between text-[11px] text-slate-400"><span>{{ term.term }}</span><span class="font-mono text-slate-500">{{ term.count }}</span></div></div></div>
      </aside>
    </div>
    </template>
    <template v-else>
      <div v-if="loadingEntityModel" class="flex-1 rounded-2xl border border-slate-800 bg-slate-900/30 flex items-center justify-center text-sm text-slate-500">Loading entity model…</div>
      <div v-else-if="!entityModel?.entities?.length" class="flex-1 rounded-2xl border border-dashed border-slate-700 p-10 text-center text-sm text-slate-500 flex items-center justify-center">No deterministic entities were found for this saved analysis.</div>
      <div v-else class="flex-1 min-h-0 grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_340px] gap-3">
        <div class="min-h-0 rounded-2xl border border-slate-800 bg-[#0b1020] overflow-hidden relative">
          <div class="absolute top-3 left-3 z-10 text-[10px] text-slate-400 bg-slate-950/80 border border-slate-800 rounded-lg px-2 py-1">{{ entityModel.entities.length }} entity(s) · {{ entityModel.antv_x6?.edges?.length || 0 }} relationship(s)</div>
          <div class="absolute top-3 right-3 z-10 flex items-center gap-1 rounded-lg border border-slate-800 bg-slate-950/80 px-2 py-1"><button type="button" @click="entityCanvas?.fit()" title="Fit to view" class="p-1 text-slate-300"><Maximize2 class="h-3.5 w-3.5" /></button><button type="button" @click="entityCanvas?.zoomIn()" title="Zoom in" class="p-1 text-slate-300"><Plus class="h-3.5 w-3.5" /></button><button type="button" @click="entityCanvas?.zoomOut()" title="Zoom out" class="p-1 text-slate-300"><Minus class="h-3.5 w-3.5" /></button><button type="button" @click="entityCanvas?.reset()" title="Reset layout" class="p-1 text-slate-300"><Focus class="h-3.5 w-3.5" /></button><button type="button" @click="entityCanvas?.prettify()" title="Prettify layout" class="p-1 text-slate-300"><Sparkles class="h-3.5 w-3.5" /></button></div>
          <EntityModelCanvas ref="entityCanvas" :model="entityModel" :search="search" :selected-entity-id="selectedEntityId" :selected-relationship-id="selectedRelationshipId" @select-entity="selectEntity" @select-relationship="selectEntityRelationship" @select-behavior="selectEntityBehavior" />
        </div>
        <aside class="min-h-0 overflow-y-auto rounded-2xl border border-slate-800 bg-slate-900/30 p-3 space-y-3">
          <div v-if="error && entityModel" class="rounded-xl border border-amber-500/30 bg-amber-950/20 p-3 text-[11px] text-amber-100/80">{{ error }}</div>
          <div class="rounded-2xl border border-slate-800 bg-slate-900/40 p-3"><div class="flex items-center justify-between gap-2"><h4 class="text-xs font-semibold text-slate-300">Entity model</h4><span class="rounded-md px-2 py-1 text-[9px]" :class="entityModel.enriched ? 'bg-indigo-500/20 text-indigo-200' : 'bg-emerald-500/15 text-emerald-200'">{{ entityModel.enriched ? 'enriched' : 'deterministic' }}</span></div><p class="mt-2 text-[10px] text-slate-500">{{ entityModel.source?.framework || selectedFramework || 'framework-neutral' }} · {{ entityModel.entities.length }} entities</p></div>
          <div v-if="selectedBehavior" class="rounded-2xl border border-violet-500/20 bg-violet-950/10 p-3"><div class="flex items-start justify-between gap-2"><div><h4 class="text-sm font-semibold text-white">{{ selectedBehaviorEntity?.name || selectedBehavior.owner || 'Behavior' }}</h4><p class="mt-1 text-[10px] text-violet-200 break-all">{{ selectedBehavior.signature || selectedBehavior.name }}</p></div><button type="button" @click="selectedBehavior = null; behaviorUsages = null" class="text-slate-500 hover:text-slate-300"><X class="h-3.5 w-3.5" /></button></div><div v-if="loadingBehaviorUsages" class="mt-4 text-[11px] text-slate-500">Loading verified usages…</div><template v-else-if="behaviorUsages"><div v-if="behaviorUsages.definition?.file || behaviorUsages.definition?.line" class="mt-3 text-[11px] text-slate-400"><span class="text-slate-500">Definition:</span> {{ behaviorUsages.definition.file || 'indexed source' }}{{ behaviorUsages.definition.line ? ':' + behaviorUsages.definition.line : '' }}</div><div v-if="behaviorUsages.warnings?.length" class="mt-3 space-y-1"><p v-for="warning in behaviorUsages.warnings" :key="typeof warning === 'string' ? warning : warning.code + warning.message" class="text-[11px] text-amber-200/80">{{ typeof warning === 'string' ? warning : warning.message || warning.code }}</p></div><div class="mt-3"><h5 class="text-[10px] uppercase tracking-wide text-slate-500">Verified usages · {{ behaviorUsages.total }}</h5><div v-if="!behaviorUsages.usages.length" class="mt-2 text-[11px] text-slate-500">No verified usage data available.</div><div v-for="usage in behaviorUsages.usages" :key="usage.location + usage.usage_kind + usage.caller_fqn" class="mt-2 rounded-lg border border-slate-800 bg-slate-950/40 p-2"><div class="font-mono text-[11px] text-slate-200">{{ usage.location }}</div><div class="mt-1 text-[10px] text-slate-500">{{ usage.usage_kind || 'reference' }}<span v-if="usage.caller_class || usage.caller_method"> · {{ [usage.caller_class, usage.caller_method].filter(Boolean).join('::') }}</span></div></div><button v-if="behaviorUsages.has_more" type="button" :disabled="loadingMoreBehaviorUsages" @click="loadBehaviorUsages(selectedBehaviorEntity || ({ name: behaviorUsages.entity } as DomainEntity), selectedBehavior, true)" class="mt-3 w-full rounded-lg border border-slate-700 px-3 py-2 text-[11px] text-slate-300 hover:bg-slate-800 disabled:opacity-50">{{ loadingMoreBehaviorUsages ? 'Loading…' : 'Load more' }}</button></div></template></div>
          <div v-else-if="selectedEntity" class="rounded-2xl border border-cyan-500/20 bg-cyan-950/10 p-3"><div class="flex items-start justify-between gap-2"><div><h4 class="text-sm font-semibold text-white">{{ selectedEntity.name }}</h4><p class="mt-1 text-[10px] text-slate-500 break-all">{{ selectedEntity.namespace || selectedEntity.table || 'entity' }}</p></div><button type="button" @click="selectedEntityId = ''" class="text-slate-500 hover:text-slate-300"><X class="h-3.5 w-3.5" /></button></div><p v-if="selectedEntity.description" class="mt-3 text-xs text-slate-300">{{ selectedEntity.description }}</p><div class="mt-3 space-y-2"><div v-for="property in selectedEntity.properties" :key="property.name" class="border-b border-slate-800 pb-2 text-[11px]"><div class="flex justify-between gap-2"><span class="font-mono text-slate-200">{{ property.name }}</span><span class="text-slate-500">{{ property.type }}</span></div><div class="mt-1 text-slate-500">{{ (property.is_primary ?? property.primaryKey) ? 'primary key · ' : '' }}{{ (property.is_foreign ?? property.foreignKey) ? 'foreign key · ' : '' }}{{ (property.is_nullable ?? property.nullable) ? 'nullable' : 'required' }}</div></div></div><div v-if="selectedEntityRelationships.length" class="mt-3"><h5 class="text-[10px] uppercase tracking-wide text-slate-500">Relationships</h5><button v-for="relationship in selectedEntityRelationships" :key="relationship.id || relationship.name" type="button" @click="selectEntityRelationship(relationship)" class="mt-2 block w-full text-left text-[11px] text-slate-400 hover:text-cyan-200">{{ relationship.name || relationship.relation_name || relationship.type || 'relationship' }} → {{ relationship.target_entity }} <span class="text-cyan-300">{{ relationship.cardinality || '' }}</span></button></div><div v-if="selectedEntity.scopes_or_behaviors?.length || selectedEntity.behaviors?.length" class="mt-3"><h5 class="text-[10px] uppercase tracking-wide text-slate-500">Behaviors</h5><button v-for="behavior in (selectedEntity.methods || selectedEntity.scopes_or_behaviors || selectedEntity.behaviors || [])" :key="typeof behavior === 'string' ? behavior : behavior.behavior_key || behavior.name" type="button" @click="selectEntityBehavior(selectedEntity, typeof behavior === 'string' ? { name: behavior, behavior_key: `${selectedEntity.name}::${behavior}()` } : behavior)" class="mt-1 block w-full text-left text-[11px] text-slate-400 hover:text-cyan-200">{{ typeof behavior === 'string' ? behavior : behavior.signature || behavior.name }}</button></div></div>
          <div v-else-if="selectedEntityRelationship" class="rounded-2xl border border-indigo-500/20 bg-indigo-950/10 p-3"><div class="flex items-center justify-between"><h4 class="text-sm font-semibold text-white">{{ selectedEntityRelationship.cardinality || 'Relationship' }}</h4><button type="button" @click="selectedRelationshipId = ''" class="text-slate-500 hover:text-slate-300"><X class="h-3.5 w-3.5" /></button></div><p class="mt-3 text-xs text-slate-300">{{ selectedEntityRelationship.name || selectedEntityRelationship.relation_name || selectedEntityRelationship.type }}</p><p class="mt-2 text-[11px] text-slate-500">{{ selectedEntityRelationship.source_entity }} → {{ selectedEntityRelationship.target_entity }}{{ selectedEntityRelationship.foreign_key ? ` · FK ${selectedEntityRelationship.foreign_key}` : '' }}</p></div>
          <div class="rounded-2xl border border-slate-800 bg-slate-900/40 p-3"><div class="flex items-center justify-between gap-2"><h4 class="text-xs font-semibold text-slate-300">PlantUML source</h4><div class="flex items-center gap-1"><button type="button" @click="copyPlantuml" title="Copy source" class="rounded-lg p-1.5 text-slate-400 hover:bg-slate-800 hover:text-white"><Copy class="h-3.5 w-3.5" /></button><button type="button" @click="downloadPlantuml" title="Download .puml" class="rounded-lg p-1.5 text-slate-400 hover:bg-slate-800 hover:text-white"><Download class="h-3.5 w-3.5" /></button></div></div><pre class="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-[10px] leading-relaxed text-slate-300">{{ entityModel.plantuml }}</pre></div>
          <div v-if="entityModel.warnings?.length" class="rounded-2xl border border-amber-500/30 bg-amber-950/20 p-3"><h4 class="text-xs font-semibold text-amber-200 flex items-center gap-2"><AlertTriangle class="h-3.5 w-3.5" /> Warnings</h4><p v-for="warning in entityModel.warnings" :key="typeof warning === 'string' ? warning : warning.code + warning.message" class="mt-2 text-[11px] text-amber-100/80">{{ typeof warning === 'string' ? warning : warning.message || warning.code }}</p></div>
        </aside>
      </div>
    </template>
  </section>
</template>

<style scoped>
:deep(.vue-flow__edge-text) { fill: #cbd5e1; font-size: 9px; }
:deep(.vue-flow__edge-textbg) { fill: #0b1020; opacity: .9; }
:deep(.domain-node-selected) { filter: drop-shadow(0 0 8px rgba(34, 211, 238, .45)); }
</style>
