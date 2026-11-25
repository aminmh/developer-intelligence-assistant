<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import {
  Activity, AlertTriangle, Archive, Braces, ChevronDown, ChevronsUpDown, Download, Eraser, FileCode, FileJson, GitMerge, KeyRound,
  Link2, Minus, PanelRightClose, PanelRightOpen, Play, RefreshCw, Search, Sparkles, Table, Table2, Trash2, X,
  EyeOff, GitCompare, Save, ExternalLink,
} from 'lucide-vue-next';
import { makeRequest } from '../ui/makeRequest';
import { useProjectScope } from '../composables/useProjectScope';
import { toastMessage } from '../composables/useToasts';
import { subscribeRuntimeUpdates } from '../ui/runtimeEvents';
import type { AnalysisSession, ErdRef, ErdTable, NormalizedSchema, RuntimeProjectRecord, RuntimeSnapshotData, SchemaVisualizationResponse } from '../types';
import { normalizeSavedSchema, parseSchemaInput, schemaToDbml, schemaToJson, type SchemaInputFormat } from '../utils/erdTransformer';
import ConfirmModal from './ConfirmModal.vue';
import SchemaDraftPanel, { type DraftStatus } from './SchemaDraftPanel.vue';
import RuntimeSchemaLoader from './runtime/RuntimeSchemaLoader.vue';
import OpenDrawioButton from './runtime/OpenDrawioButton.vue';
import type { SavedSchemaDraft } from '../types';
import RuleLinksPanel from './code-quality/RuleLinksPanel.vue';
import type { SchemaSelection } from '../utils/drawioBridge';
import { diffSchemas, relationshipKeys, tableChangeKey, type SchemaChanges, type TableChange } from '../utils/schemaChanges';

const draftPanel = ref<InstanceType<typeof SchemaDraftPanel> | null>(null);
const draftStatus = ref<DraftStatus>({ hasDraft: false, dirty: false, editorActive: false, busy: false, revision: 0 });
const acceptRuntimeDraft = (draft: SavedSchemaDraft, popup: Window | null) => draftPanel.value?.acceptRuntimeDraft(draft, popup) || false;
const canLoadRuntimeDraft = () => draftPanel.value?.canLoadRuntimeDraft() || false;
const analyses = ref<AnalysisSession[]>([]);
const selectedAnalysisId = ref('');
const sourceType = ref<'saved' | SchemaInputFormat>('saved');
const inputFormat = ref<SchemaInputFormat>('sql');
const inputText = ref('');
const schema = ref<NormalizedSchema | null>(null);
let schemaRequest = 0;
const search = ref('');
const loading = ref(false);
const loadingAnalyses = ref(false);
const parsing = ref(false);
const error = toastMessage('error');
const selectedTableId = ref('');
const selectedRefId = ref('');
const rightPanelOpen = ref(true);
const warningsExpanded = ref(true);
const deleteTarget = ref<AnalysisSession | null>(null);
const deletingAnalysis = ref(false);
const runtimeProjects = ref<RuntimeProjectRecord[]>([]);
const runtimeScope = ref('');
const runtimeData = ref<RuntimeSnapshotData | null>(null);
const runtimeError = ref('');
const runtimeOptions = computed(() => runtimeProjects.value.filter((item) => item.commit_sha && item.revision_id));
// Table usage follows the project chosen in the header switcher when it has runtime data.
const scope = useProjectScope();
const scopedRuntimeOption = () => {
  if (!scope.projectId.value) return null;
  const forProject = runtimeOptions.value.filter((item) => item.project_id === scope.projectId.value);
  const wanted = scope.revision.value === 'latest' ? scope.resolvedRevision.value?.revision_id : scope.revision.value;
  return forProject.find((item) => item.revision_id === wanted) || forProject[0] || null;
};
watch(() => [scope.projectId.value, scope.revision.value, scope.resolvedRevision.value?.revision_id] as const, () => {
  const option = scopedRuntimeOption();
  if (option) runtimeScope.value = `${option.project_id}:${option.revision_id}`;
});
const tableRuntime = computed(() => runtimeData.value?.telemetry.tables.filter((row) => row.table_name.toLowerCase() === selectedTable.value?.name.toLowerCase()) || []);
const tableSymbols = computed(() => runtimeData.value?.telemetry.symbol_tables.filter((row) => row.table_name.toLowerCase() === selectedTable.value?.name.toLowerCase()) || []);
// Business rules linked to the selected table or to the code that uses it.
const runtimeProject = computed(() => { const [project_id = '', revision_id = ''] = runtimeScope.value.split(':'); return { project_id, revision_id }; });
const tableRuleIds = computed(() => [...new Set([
  ...((runtimeData.value as any)?.telemetry.tables || []).filter((row: any) => row.table_name.toLowerCase() === selectedTable.value?.name.toLowerCase()).map((row: any) => row.table_id),
  ...tableSymbols.value.map((row) => row.symbol_id)])].filter(Boolean));
const tableRoutes = computed(() => {
  const ids = new Set(tableSymbols.value.map((row) => row.symbol_id));
  return runtimeData.value?.telemetry.symbol_routes.filter((row) => ids.has(row.symbol_id)) || [];
});
const busyTableCounts = computed(() => Object.fromEntries((runtimeData.value?.telemetry.tables || []).reduce((counts, row) => {
  const key = row.table_name.toLowerCase();
  counts.set(key, (counts.get(key) || 0) + Number(row.operations || 0));
  return counts;
}, new Map<string, number>())));

const loadRuntime = async () => {
  runtimeError.value = '';
  try {
    if (!runtimeProjects.value.length) {
      const response = await makeRequest('/api/runtime/projects');
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Runtime project list unavailable.');
      runtimeProjects.value = data.projects || [];
    }
    if (!runtimeScope.value && runtimeOptions.value.length) {
      const params = new URLSearchParams(window.location.search);
      const project = runtimeOptions.value.find((item) => item.project_id === params.get('project_id')
        && (params.get('revision_id')
          ? item.revision_id === params.get('revision_id')
          : item.commit_sha === params.get('commit_sha'))) || scopedRuntimeOption() || runtimeOptions.value[0];
      runtimeScope.value = `${project.project_id}:${project.revision_id}`;
    }
    const [projectId, revisionId] = runtimeScope.value.split(':');
    if (!projectId || !revisionId) { runtimeData.value = null; return; }
    const response = await makeRequest(`/api/runtime/projects/${encodeURIComponent(projectId)}/revisions/${encodeURIComponent(revisionId)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Runtime table usage unavailable.');
    runtimeData.value = data;
  } catch (cause: any) {
    runtimeError.value = cause?.message || 'Runtime table usage unavailable.';
    runtimeData.value = null;
  }
};
const selectedTable = computed(() => schema.value?.tables.find((table) => table.id === selectedTableId.value) || null);
const selectedRef = computed(() => schema.value?.refs.find((item) => item.id === selectedRefId.value) || null);
const selectedEntity = computed<SchemaSelection | null>(() => selectedTable.value ? { kind: 'table', id: selectedTable.value.id } : selectedRef.value ? { kind: 'relationship', id: selectedRef.value.id } : null);
function selectEditorEntity(selection: SchemaSelection) {
  if (selection.kind === 'table') {
    const table = schema.value?.tables.find(item => item.id === selection.id);
    if (table) selectTable(table);
  } else {
    const relation = schema.value?.refs.find(item => item.id === selection.id);
    if (relation) selectRef(relation);
  }
  rightPanelOpen.value = true;
}
// ---- Changes made in Draw.io, shown on request -------------------------------
// The draft panel streams what is in the editor. Nothing here is saved: the button only reveals the differences
// from the original schema, and a newer edit makes the button appear again.
const editorState = ref<{ baseline: unknown; schema: unknown } | null>(null);
const shownChanges = ref<SchemaChanges | null>(null);
const dismissedSignature = ref<string | null>(null);
// Compared with the saved draft, so only what changed since it was last saved counts as new, whatever source the explorer shows.
const pendingChanges = computed(() => (editorState.value ? diffSchemas(editorState.value.baseline as any, editorState.value.schema) : null));
const pendingSignature = computed(() => pendingChanges.value?.signature ?? '');
const hasNewChanges = computed(() => pendingSignature.value !== (shownChanges.value?.signature ?? '') && pendingSignature.value !== dismissedSignature.value);
const onEditorSchema = (next: { baseline: unknown; schema: unknown } | null) => {
  editorState.value = next;
  if (next === null) { shownChanges.value = null; dismissedSignature.value = null; }
};
// With no analysis or import loaded, the explorer shows the saved draft itself, so there is a Tables section to update.
const schemaFromDraft = ref(false);
let draftParseRequest = 0;
const onDraftSchema = async (next: unknown | null) => {
  const requestId = ++draftParseRequest;
  if (next === null) {
    if (schemaFromDraft.value) { schema.value = null; schemaFromDraft.value = false; }
    return;
  }
  if (schema.value && !schemaFromDraft.value) return;
  try {
    const parsed = await parseSchemaInput(next, { format: 'json', source: { kind: 'draft' } });
    if (requestId === draftParseRequest && (!schema.value || schemaFromDraft.value)) { schema.value = parsed; schemaFromDraft.value = true; }
  } catch { /* A draft that cannot be shown leaves the explorer as it was. */ }
};
// The explorer can be far below the fold, so showing the changes also brings the first one into view.
const showNewChanges = async () => {
  shownChanges.value = pendingChanges.value?.total ? pendingChanges.value : null; dismissedSignature.value = null;
  if (!shownChanges.value) return;
  const onlyRelationships = !shownChanges.value.tables.length;
  if (onlyRelationships) relationsOpen.value = true; else tablesOpen.value = true;
  await nextTick();
  const first = document.querySelector<HTMLElement>(onlyRelationships ? '.sc-rel[data-change]' : '.sc-table[data-change]');
  first?.scrollIntoView({ block: 'center', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
};
const hideChanges = () => { dismissedSignature.value = pendingSignature.value; shownChanges.value = null; };
watch(schema, () => { shownChanges.value = null; dismissedSignature.value = null; });
const changeSummary = computed(() => {
  const tables = shownChanges.value?.tables ?? [];
  const count = (status: TableChange['status']) => tables.filter((item) => item.status === status).length;
  const relations = shownChanges.value?.relationships ?? [];
  return [
    count('added') && `${count('added')} new ${count('added') === 1 ? 'table' : 'tables'}`,
    count('changed') && `${count('changed')} changed`,
    count('removed') && `${count('removed')} removed`,
    relations.length && `${relations.length} ${relations.length === 1 ? 'relationship' : 'relationships'}`,
  ].filter(Boolean).join(' · ');
});
const CHANGE_LABEL = { added: 'new', changed: 'changed', removed: 'removed' } as const;
const COLUMN_MARK = { added: '+', changed: '~', removed: '−' } as const;
const MAX_CHANGE_ROWS = 4;
type TableCard = { key: string; table: ErdTable | null; change: TableChange | null; name: string; fields: Array<{ id: string; name: string; type: string; primaryKey: boolean; foreignKey: boolean }> };
const tableCards = computed<TableCard[]>(() => {
  const byKey = new Map((shownChanges.value?.tables ?? []).filter((item) => item.status !== 'added').map((item) => [item.key, item]));
  const cards: TableCard[] = visibleTables.value.map((table) => ({
    key: table.id, table, change: byKey.get(tableChangeKey(table.schema, table.name)) ?? null, name: `${table.schema ? table.schema + '.' : ''}${table.name}`,
    fields: table.fields.map((field) => ({ id: field.id, name: field.name, type: field.type, primaryKey: !!field.primaryKey, foreignKey: !!field.foreignKey })),
  }));
  const query = search.value.trim().toLowerCase();
  const shown = new Set((schema.value?.tables ?? []).map((table) => tableChangeKey(table.schema, table.name)));
  for (const change of shownChanges.value?.tables ?? []) {
    if (change.status !== 'added' || shown.has(change.key)) continue;
    const fields = (change.fields ?? []).map((field, index) => ({ id: `${change.key}:${index}`, name: field.name, type: field.type, primaryKey: field.primaryKey, foreignKey: false }));
    if (query && !`${change.name} ${fields.map((field) => field.name).join(' ')}`.toLowerCase().includes(query)) continue;
    cards.push({ key: `added:${change.key}`, table: null, change, name: `${change.namespace ? change.namespace + '.' : ''}${change.name}`, fields });
  }
  return cards;
});
const removedRefIds = computed(() => {
  const removed = new Set((shownChanges.value?.relationships ?? []).filter((item) => item.status === 'removed').map((item) => item.key));
  const keys = schema.value ? relationshipKeys(schema.value) : new Map<string, string>();
  return new Set([...keys].filter(([, key]) => removed.has(key)).map(([id]) => id));
});
const addedRelations = computed(() => {
  const present = new Set(schema.value ? relationshipKeys(schema.value).values() : []);
  return (shownChanges.value?.relationships ?? []).filter((item) => item.status === 'added' && !present.has(item.key));
});
const tablesOpen = ref(true);
const relationsOpen = ref(false);
const visibleTables = computed(() => {
  const query = search.value.trim().toLowerCase();
  if (!schema.value || !query) return schema.value?.tables || [];
  return schema.value.tables.filter((table) => `${table.name} ${table.fields.map((field) => field.name).join(' ')}`.toLowerCase().includes(query));
});
const selectedAnalysis = computed(() => analyses.value.find((analysis) => analysis.id === selectedAnalysisId.value) || null);

const loadAnalyses = async () => {
  loadingAnalyses.value = true;
  error.value = '';
  try {
    const response = await makeRequest('/api/analysis?limit=100');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || data.error || 'Failed to load saved analyses.');
    analyses.value = (data.analyses || []).filter((item: AnalysisSession) => item.kind !== 'code_index');
    if (!analyses.value.some((analysis) => analysis.id === selectedAnalysisId.value)) selectedAnalysisId.value = analyses.value[0]?.id || '';
  } catch (cause: any) {
    error.value = cause?.message || 'Failed to load saved analyses.';
  } finally {
    loadingAnalyses.value = false;
  }
};

const loadSaved = async () => {
  schemaFromDraft.value = false;
  const requestId = ++schemaRequest;
  const analysisId = selectedAnalysisId.value;
  schema.value = null;
  if (!selectedAnalysisId.value) {
    loading.value = false;
    return;
  }
  loading.value = true;
  error.value = '';
  selectedTableId.value = '';
  selectedRefId.value = '';
  try {
    const response = await makeRequest(`/api/analysis/${encodeURIComponent(analysisId)}/schema-visualization`);
    const data = await response.json() as SchemaVisualizationResponse & { normalizedSchema?: NormalizedSchema; detail?: string };
    if (!response.ok) throw new Error(data.detail || 'Failed to load schema visualization.');
    const result = data.normalizedSchema || normalizeSavedSchema(data);
    if (requestId === schemaRequest) schema.value = result;
  } catch (cause: any) {
    if (requestId === schemaRequest) { schema.value = null; error.value = cause?.message || 'Failed to load schema visualization.'; }
  } finally {
    if (requestId === schemaRequest) loading.value = false;
  }
};

const parseImport = async () => {
  schemaFromDraft.value = false;
  if (!inputText.value.trim()) {
    error.value = 'Paste SQL, DBML, or normalized JSON before importing.';
    return;
  }
  const requestId = ++schemaRequest;
  schema.value = null;
  parsing.value = true;
  error.value = '';
  selectedTableId.value = '';
  selectedRefId.value = '';
  try {
    const parsed = await parseSchemaInput(inputText.value, { format: inputFormat.value, source: { kind: inputFormat.value } });
    const result = parsed;
    if (requestId === schemaRequest) schema.value = result;
  } catch (cause: any) {
    if (requestId === schemaRequest) { schema.value = null; error.value = cause?.message || 'Could not parse schema input.'; }
  } finally {
    if (requestId === schemaRequest) parsing.value = false;
  }
};

const loadSample = () => {
  inputFormat.value = 'sql';
  sourceType.value = 'sql';
  inputText.value = 'CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE);\nCREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT, FOREIGN KEY (user_id) REFERENCES users(id));';
  void parseImport();
};

const clearImport = () => {
  schemaRequest++;
  parsing.value = false;
  inputText.value = '';
  if (sourceType.value !== 'saved') schema.value = null;
  error.value = '';
};

const selectTable = (table: ErdTable) => {
  selectedTableId.value = table.id;
  selectedRefId.value = '';
};

const selectRef = (item: ErdRef) => {
  selectedRefId.value = item.id;
  selectedTableId.value = '';
};

const formatDefault = (value: unknown) => value === null || value === undefined || value === '' ? '—' : String(value);
const download = (content: string, name: string, type: string) => {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  URL.revokeObjectURL(url);
};
const exportDbml = () => schema.value && download(schemaToDbml(schema.value), 'schema.dbml', 'text/plain');
const exportJson = () => schema.value && download(schemaToJson(schema.value), 'schema.json', 'application/json');
const requestDeleteAnalysis = () => { if (selectedAnalysis.value) deleteTarget.value = selectedAnalysis.value; };

const deleteAnalysis = async () => {
  const target = deleteTarget.value;
  if (!target || deletingAnalysis.value) return;
  deletingAnalysis.value = true;
  error.value = '';
  try {
    const response = await makeRequest(`/api/analysis/${encodeURIComponent(target.id)}`, { method: 'DELETE' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.success) throw new Error(data.error || data.detail || 'Failed to delete saved analysis.');
    deleteTarget.value = null;
    selectedAnalysisId.value = '';
    await loadAnalyses();
  } catch (cause: any) {
    error.value = cause?.message || 'Failed to delete saved analysis.';
  } finally {
    deletingAnalysis.value = false;
  }
};

watch(selectedAnalysisId, () => { if (sourceType.value === 'saved') void loadSaved(); });
watch(sourceType, (value) => {
  schemaRequest++;
  schemaFromDraft.value = false;
  schema.value = null;
  loading.value = false;
  parsing.value = false;
  if (value === 'saved') void loadSaved();
}, { flush: 'sync' });
watch(runtimeScope, () => { void loadRuntime(); });
let stopRuntimeUpdates: (() => void) | undefined;
onMounted(async () => {
  await Promise.all([loadAnalyses(), loadRuntime()]);
  stopRuntimeUpdates = subscribeRuntimeUpdates(() => { void loadRuntime(); });
});
onUnmounted(() => stopRuntimeUpdates?.());

// ---- Presentation helpers ----------------------------------------------------
const sourceOptions = [
  { id: 'saved' as const, label: 'Saved analysis', icon: Archive },
  { id: 'sql' as const, label: 'SQL', icon: FileCode },
  { id: 'dbml' as const, label: 'DBML', icon: Braces },
  { id: 'json' as const, label: 'JSON', icon: FileJson },
];
// The source switch also picks the parse format, so the two can't disagree.
watch(sourceType, (value) => { if (value !== 'saved') inputFormat.value = value; }, { flush: 'sync' });
const importFileName = computed(() => (inputFormat.value === 'dbml' ? 'schema.dbml' : inputFormat.value === 'json' ? 'schema.json' : 'schema.sql'));
const PREVIEW_COLUMNS = 4;
// Relationship ends are table IDs (`table:posts`); show the readable name.
const tableLabel = (id: string) => id.replace(/^table:/, '');
// Relationship ends are table IDs (often opaque hashes). Show the table's name, schema-qualified like the Tables list.
const displayName = (table: ErdTable) => `${table.schema ? table.schema + '.' : ''}${table.name}`;
const tableByReference = (reference: string) => {
  const bare = reference.toLowerCase().replace(/^table:/, '');
  return schema.value?.tables.find((table) => table.id === reference)
    || schema.value?.tables.find((table) => table.name.toLowerCase() === bare || displayName(table).toLowerCase() === bare);
};
const tableName = (reference: string) => { const table = tableByReference(reference); return table ? displayName(table) : tableLabel(reference); };
type RelationRow = { key: string; ref: ErdRef | null; local: string; other: string; otherColumn: string; cardinality?: string; change?: 'added' | 'removed' };
type RelationGroup = { key: string; name: string; out: RelationRow[]; in: RelationRow[] };
// One card per table, listing every relationship it takes part in: the tables it references and the tables that reference it.
const relationGroups = computed<RelationGroup[]>(() => {
  const groups = new Map<string, RelationGroup>();
  const resolve = (reference: string) => { const table = tableByReference(reference); return table ? { label: displayName(table), short: table.name } : { label: tableLabel(reference), short: tableLabel(reference) }; };
  const groupFor = (label: string, short: string) => {
    const key = short.toLowerCase();
    let group = groups.get(key);
    if (!group) { group = { key, name: label, out: [], in: [] }; groups.set(key, group); }
    return group;
  };
  for (const ref of schema.value?.refs ?? []) {
    const source = resolve(ref.sourceTable), target = resolve(ref.targetTable);
    const change = removedRefIds.value.has(ref.id) ? 'removed' as const : undefined;
    groupFor(source.label, source.short).out.push({ key: `${ref.id}:out`, ref, local: ref.sourceColumn, other: target.label, otherColumn: ref.targetColumn, cardinality: ref.cardinality, change });
    groupFor(target.label, target.short).in.push({ key: `${ref.id}:in`, ref, local: ref.targetColumn, other: source.label, otherColumn: ref.sourceColumn, cardinality: ref.cardinality, change });
  }
  for (const relation of addedRelations.value) {
    const source = resolve(relation.sourceTable), target = resolve(relation.targetTable);
    groupFor(source.label, source.short).out.push({ key: `${relation.key}:out`, ref: null, local: relation.sourceColumn, other: target.label, otherColumn: relation.targetColumn, change: 'added' });
    groupFor(target.label, target.short).in.push({ key: `${relation.key}:in`, ref: null, local: relation.targetColumn, other: source.label, otherColumn: relation.sourceColumn, change: 'added' });
  }
  return [...groups.values()].sort((a, b) => a.name.localeCompare(b.name));
});
const tableOps = (table: ErdTable) => busyTableCounts.value[table.name.toLowerCase()] || 0;
const maxTableOps = computed(() => Math.max(1, ...Object.values(busyTableCounts.value).map(Number)));
const formatOps = (value: number) => (value >= 1000 ? `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}k` : String(value));
const tableRuntimeTotal = computed(() => tableRuntime.value.reduce((sum, row) => sum + Number(row.operations || 0), 0));
const tableRuntimeMax = computed(() => Math.max(1, ...tableRuntime.value.map((row) => Number(row.operations || 0))));
</script>

<template>
  <section class="sc-page" aria-label="Database schema">
    <div class="sc-wrap">
      <!-- Pinned to the top so it is seen on returning from Draw.io, however far down the explorer is. -->
      <div v-if="hasNewChanges" class="sc-newchanges-bar" role="status" aria-live="polite">
        <GitCompare aria-hidden="true" />
        <span><b>Draw.io has new changes</b><template v-if="pendingChanges?.total"> · {{ pendingChanges.total }} {{ pendingChanges.total === 1 ? 'difference' : 'differences' }} from the saved draft</template></span>
        <button type="button" class="sc-btn sc-btn-sm sc-push" data-variant="primary" @click="showNewChanges"><GitCompare aria-hidden="true" />Show new changes</button>
      </div>
      <!-- Toolbar -->
      <div class="sc-bar">
        <OpenDrawioButton label="Blank Draw.io" hint="Opens an empty Draw.io that is not connected to any draft. To edit a draft, open it below and choose Open in Draw.io." />
        <div class="sc-seg" role="group" aria-label="Schema source">
          <button v-for="option in sourceOptions" :key="option.id" type="button" :aria-pressed="sourceType === option.id" @click="sourceType = option.id">
            <component :is="option.icon" aria-hidden="true" />{{ option.label }}
          </button>
        </div>
        <template v-if="sourceType === 'saved'">
          <label class="sc-pick">
            <Archive aria-hidden="true" />
            <span class="sr-only">Saved analysis</span>
            <select v-model="selectedAnalysisId">
              <option value="">Select saved analysis</option>
              <option v-for="analysis in analyses" :key="analysis.id" :value="analysis.id">{{ analysis.label || analysis.filename }} · {{ analysis.created_at }}</option>
            </select>
            <ChevronsUpDown aria-hidden="true" />
          </label>
          <button v-if="selectedAnalysis" type="button" class="sc-btn sc-btn-icon" data-variant="danger" :disabled="deletingAnalysis" title="Remove selected analyzed project" aria-label="Remove selected analyzed project" @click="requestDeleteAnalysis"><Trash2 aria-hidden="true" /></button>
        </template>
        <label class="sc-pick">
          <Activity aria-hidden="true" />
          <span class="sr-only">Runtime project for table usage</span>
          <select v-model="runtimeScope">
            <option value="">Runtime project for table usage</option>
            <option v-for="item in runtimeOptions" :key="item.project_id + item.revision_id" :value="item.project_id + ':' + item.revision_id">{{ item.project_id }} · {{ item.revision_id ? `Revision ${item.revision_id.slice(4, 12)}` : 'Revision unavailable' }}</option>
          </select>
          <ChevronsUpDown aria-hidden="true" />
        </label>
        <button type="button" class="sc-btn sc-btn-icon sc-push" title="Refresh analyses" aria-label="Refresh analyses" @click="loadAnalyses"><RefreshCw :class="{ 'sc-spin': loadingAnalyses }" aria-hidden="true" /></button>
      </div>

      <!-- Import -->
      <section v-if="sourceType !== 'saved'" class="sc-card" aria-labelledby="sc-import-title">
        <div class="sc-sec-head"><h2 id="sc-import-title" class="sc-sec-title"><FileCode aria-hidden="true" />Import a schema</h2></div>
        <div class="sc-import">
          <div class="sc-editor">
            <div class="sc-editor-head"><FileCode aria-hidden="true" />{{ importFileName }}<span class="sc-push">{{ inputFormat === 'sql' ? 'MySQL / PostgreSQL / SQLite' : inputFormat === 'dbml' ? 'DBML' : 'Normalized JSON' }}</span></div>
            <label for="sc-import-text" class="sr-only">Schema source</label>
            <textarea id="sc-import-text" v-model="inputText" spellcheck="false" :placeholder="inputFormat === 'dbml' ? 'Table users { id int [pk] }' : inputFormat === 'json' ? '{ &quot;tables&quot;: [], &quot;refs&quot;: [] }' : 'CREATE TABLE users (...);'"></textarea>
          </div>
          <div class="sc-import-side">
            <button type="button" class="sc-btn sc-btn-lg" data-variant="primary" :disabled="parsing" @click="parseImport"><Play aria-hidden="true" />{{ parsing ? 'Parsing…' : 'Import' }}</button>
            <div class="sc-row">
              <button type="button" class="sc-btn" @click="loadSample"><Sparkles aria-hidden="true" />Sample</button>
              <button type="button" class="sc-btn" @click="clearImport"><Eraser aria-hidden="true" />Clear</button>
            </div>
            <p class="sc-hint">Imports stay in this session until you create a draft. Unsupported or ambiguous references appear as warnings.</p>
          </div>
        </div>
      </section>

      <RuntimeSchemaLoader :project-id="scope.projectId.value" :accept-draft="acceptRuntimeDraft" :can-load="canLoadRuntimeDraft" />

      <SchemaDraftPanel ref="draftPanel" :schema="schema" :schema-is-draft="schemaFromDraft" :source-type="sourceType" :analysis-id="selectedAnalysisId" :runtime-counts="busyTableCounts" :selection="selectedEntity" @select-entity="selectEditorEntity" @editor-schema="onEditorSchema" @draft-schema="onDraftSchema" @status="draftStatus = $event" />
      <!-- A draft is saved only here or in the panel above: Draw.io never saves it for you. -->
      <div v-if="draftStatus.hasDraft" class="sc-draft-bar" :data-state="draftStatus.dirty ? 'unsaved' : 'saved'" role="status">
        <Save aria-hidden="true" />
        <span><b>Draft revision {{ draftStatus.revision }}</b> · {{ draftStatus.dirty ? 'unsaved changes' : 'saved' }}<template v-if="draftStatus.editorActive"> · Draw.io is open and saves nothing by itself</template></span>
        <button v-if="!draftStatus.editorActive" type="button" class="sc-btn sc-btn-sm sc-push" data-variant="primary" :disabled="draftStatus.busy" title="Opens this draft in Draw.io in a new tab. Close any older Draw.io tab first: it does not update by itself." @click="draftPanel?.openEditor()"><ExternalLink aria-hidden="true" />Open in Draw.io</button>
        <button type="button" class="sc-btn sc-btn-sm" :class="{ 'sc-push': draftStatus.editorActive }" :data-variant="draftStatus.dirty ? 'primary' : undefined" :disabled="!draftStatus.dirty || draftStatus.busy" @click="draftPanel?.saveDraft()"><Save aria-hidden="true" />Save draft</button>
      </div>

      <div v-if="runtimeError" class="sc-banner" data-tone="warn"><AlertTriangle aria-hidden="true" /><span>{{ runtimeError }}</span></div>
      <div v-if="loading" class="sc-card sc-loading" role="status"><RefreshCw class="sc-spin" aria-hidden="true" />Loading schema…</div>
      <div v-else-if="!schema" class="sc-empty">
        <span class="sc-empty-icon"><Table2 aria-hidden="true" /></span>
        <h2>No schema loaded</h2>
        <p>Choose a saved analysis, or import SQL, DBML or normalized JSON.</p>
        <button type="button" class="sc-btn" data-variant="primary" @click="loadSample"><Sparkles aria-hidden="true" />Load sample schema</button>
      </div>

      <div v-else class="sc-layout" :data-rail="rightPanelOpen ? 'open' : 'closed'">
        <!-- Explorer -->
        <section class="sc-card" aria-labelledby="sc-schema-title">
          <div class="sc-sec-head">
            <h2 id="sc-schema-title" class="sc-sec-title"><Table2 aria-hidden="true" />{{ schemaFromDraft ? 'Draft schema' : 'Original schema' }}</h2>
            <span class="sc-tag">{{ schema.tables.length }} tables</span>
            <span class="sc-tag">{{ schema.refs.length }} relationships</span>
            <span v-if="schema.warnings.length" class="sc-tag" data-tone="warn"><AlertTriangle aria-hidden="true" />{{ schema.warnings.length }} {{ schema.warnings.length === 1 ? 'warning' : 'warnings' }}</span>
            <div class="sc-push sc-row">
              <button type="button" class="sc-btn sc-btn-sm" @click="exportDbml"><Download aria-hidden="true" />DBML</button>
              <button type="button" class="sc-btn sc-btn-sm" @click="exportJson"><Download aria-hidden="true" />JSON</button>
            </div>
          </div>
          <p class="sc-hint">{{ schemaFromDraft ? 'This list shows the saved draft.' : 'This list shows the original source.' }} Edit the draft in Draw.io: your edits can be shown here with Show new changes, and the draft is saved only when you choose Save draft.</p>
          <div v-if="shownChanges" class="sc-changes-bar" role="status">
            <GitCompare aria-hidden="true" /><span><b>Changes from Draw.io</b> · {{ changeSummary }}</span>
            <button type="button" class="sc-btn sc-btn-sm sc-push" @click="hideChanges"><EyeOff aria-hidden="true" />Hide changes</button>
          </div>
          <div class="sc-sec-head sc-mt">
            <button type="button" class="sc-collapse" :aria-expanded="tablesOpen" aria-controls="sc-tables-body" @click="tablesOpen = !tablesOpen"><ChevronDown class="sc-collapse-chev" aria-hidden="true" /><Table aria-hidden="true" /><span>Tables</span><span class="sc-tag">{{ schema.tables.length }}</span></button>
          </div>
          <div v-show="tablesOpen" id="sc-tables-body">
          <label class="sc-search"><Search aria-hidden="true" /><span class="sr-only">Search tables and columns</span><input v-model="search" placeholder="Search tables and columns…" /></label>
          <p class="sc-hint">{{ visibleTables.length }} of {{ schema.tables.length }} tables shown</p>

          <div class="sc-tables">
            <button
              v-for="card in tableCards"
              :key="card.key"
              type="button"
              class="sc-table"
              :data-change="card.change?.status"
              :aria-pressed="!!card.table && selectedTableId === card.table.id"
              :aria-disabled="!card.table || undefined"
              :title="card.change?.detail || (card.table ? undefined : 'New in Draw.io, not saved yet')"
              @click="card.table && selectTable(card.table)"
            >
              <span class="sc-table-head">
                <Table aria-hidden="true" />
                <b>{{ card.name }}</b>
                <span v-if="card.change" class="sc-chg" :data-status="card.change.status">{{ CHANGE_LABEL[card.change.status] }}</span>
                <span v-if="card.table && tableOps(card.table)" class="sc-heat" :style="{ opacity: 0.55 + 0.45 * (tableOps(card.table) / maxTableOps) }" :title="`${tableOps(card.table).toLocaleString()} observed operations`">{{ formatOps(tableOps(card.table)) }}</span>
              </span>
              <span class="sc-cols">
                <span v-for="field in card.fields.slice(0, PREVIEW_COLUMNS)" :key="field.id" class="sc-col" :data-key="field.primaryKey ? 'pk' : field.foreignKey ? 'fk' : undefined">
                  <KeyRound v-if="field.primaryKey" aria-label="Primary key" />
                  <Link2 v-else-if="field.foreignKey" aria-label="Foreign key" />
                  <Minus v-else aria-hidden="true" />
                  <span class="sc-col-name">{{ field.name }}</span>
                  <span class="sc-col-type" :title="field.type">{{ field.type }}</span>
                </span>
                <span v-if="card.fields.length > PREVIEW_COLUMNS" class="sc-col-more">+ {{ card.fields.length - PREVIEW_COLUMNS }} more {{ card.fields.length - PREVIEW_COLUMNS === 1 ? 'column' : 'columns' }}</span>
              </span>
              <span v-if="card.change?.detail || card.change?.columns.length" class="sc-chg-list">
                <span v-if="card.change.detail" class="sc-chg-item" data-status="changed"><span aria-hidden="true">~</span>{{ card.change.detail }}</span>
                <span v-for="column in card.change.columns.slice(0, MAX_CHANGE_ROWS)" :key="column.status + column.name" class="sc-chg-item" :data-status="column.status"><span aria-hidden="true">{{ COLUMN_MARK[column.status] }}</span><b>{{ column.name }}</b><em v-if="column.detail">{{ column.detail }}</em></span>
                <span v-if="card.change.columns.length > MAX_CHANGE_ROWS" class="sc-col-more">+ {{ card.change.columns.length - MAX_CHANGE_ROWS }} more {{ card.change.columns.length - MAX_CHANGE_ROWS === 1 ? 'change' : 'changes' }}</span>
              </span>
            </button>
          </div>
          <p v-if="!tableCards.length" class="sc-hint">No matching tables.</p>
          </div>

          <template v-if="schema.refs.length || addedRelations.length">
            <div class="sc-sec-head sc-mt">
              <button type="button" class="sc-collapse" :aria-expanded="relationsOpen" aria-controls="sc-relations-body" @click="relationsOpen = !relationsOpen"><ChevronDown class="sc-collapse-chev" aria-hidden="true" /><GitMerge aria-hidden="true" /><span>Relationships</span><span class="sc-tag">{{ schema.refs.length }}</span></button>
            </div>
            <div v-show="relationsOpen" id="sc-relations-body" class="sc-relcards">
              <div v-for="group in relationGroups" :key="group.key" class="sc-relcard">
                <div class="sc-relcard-head"><Table aria-hidden="true" /><b>{{ group.name }}</b><span class="sc-tag">{{ group.out.length + group.in.length }}</span></div>
                <template v-for="side in ([{ id: 'out', label: 'References' }, { id: 'in', label: 'Referenced by' }] as const)" :key="side.id">
                  <template v-if="group[side.id].length">
                    <p class="sc-relcard-label">{{ side.label }}</p>
                    <component
                      :is="row.ref ? 'button' : 'div'"
                      v-for="row in group[side.id]"
                      :key="row.key"
                      :type="row.ref ? 'button' : undefined"
                      class="sc-rel"
                      :data-change="row.change"
                      :aria-pressed="row.ref ? selectedRefId === row.ref.id : undefined"
                      :title="row.change === 'added' ? 'New in Draw.io, not saved yet' : undefined"
                      @click="row.ref && selectRef(row.ref)"
                    >
                      <Link2 aria-hidden="true" />
                      <span v-if="side.id === 'out'" class="sc-rel-text"><b>{{ row.local }}</b> → <b>{{ row.other }}</b>.{{ row.otherColumn }}</span>
                      <span v-else class="sc-rel-text"><b>{{ row.other }}</b>.{{ row.otherColumn }} → <b>{{ row.local }}</b></span>
                      <span v-if="row.change" class="sc-chg" :data-status="row.change">{{ row.change === 'added' ? 'new' : 'removed' }}</span>
                      <span v-else-if="row.cardinality" class="sc-rel-card">{{ row.cardinality }}</span>
                    </component>
                  </template>
                </template>
              </div>
            </div>
          </template>
        </section>

        <!-- Details rail -->
        <aside class="sc-rail" aria-label="Schema details">
          <button type="button" class="sc-btn sc-btn-sm sc-rail-toggle" :title="rightPanelOpen ? 'Collapse details panel' : 'Expand details panel'" :aria-label="rightPanelOpen ? 'Collapse details panel' : 'Expand details panel'" :aria-expanded="rightPanelOpen" @click.stop="rightPanelOpen = !rightPanelOpen">
            <PanelRightClose v-if="rightPanelOpen" aria-hidden="true" /><PanelRightOpen v-else aria-hidden="true" /><span v-if="rightPanelOpen">Hide details</span>
          </button>
          <template v-if="rightPanelOpen">
            <details v-if="schema.warnings.length" class="sc-warnings" :open="warningsExpanded" @toggle="warningsExpanded = ($event.target as HTMLDetailsElement).open">
              <summary><AlertTriangle aria-hidden="true" />{{ schema.warnings.length }} {{ schema.warnings.length === 1 ? 'warning' : 'warnings' }}<ChevronDown class="sc-chev" aria-hidden="true" /></summary>
              <p v-for="warning in schema.warnings" :key="warning.code + warning.message">{{ warning.message }}</p>
            </details>

            <div v-if="selectedTable" class="sc-card">
              <div class="sc-detail-head"><Table aria-hidden="true" /><b>{{ selectedTable.name }}</b><button type="button" class="sc-btn sc-btn-icon sc-btn-sm sc-push" aria-label="Close table details" @click="selectedTableId = ''"><X aria-hidden="true" /></button></div>
              <div v-if="runtimeData" class="sc-usage">
                <p class="sc-usage-top"><b>{{ tableRuntimeTotal.toLocaleString() }}</b>observed operations · {{ runtimeData.telemetry.retention_hours / 24 }} retained days</p>
                <template v-if="tableRuntime.length">
                  <div v-for="row in tableRuntime" :key="row.operation" class="sc-op">
                    <span>{{ row.operation || 'other' }}</span>
                    <span class="sc-op-bar" aria-hidden="true"><i :style="{ transform: `scaleX(${Number(row.operations || 0) / tableRuntimeMax})` }"></i></span>
                    <span>{{ Number(row.operations || 0).toLocaleString() }}</span>
                  </div>
                </template>
                <p v-else class="sc-hint">No operations observed for this table.</p>
                <div v-if="tableSymbols.length" class="sc-code-use">
                  <span class="sc-label">Code using this table</span>
                  <span v-for="row in tableSymbols" :key="row.symbol_id">{{ row.symbol_id }} · {{ row.calls }}</span>
                </div>
                <p v-if="tableRoutes.length" class="sc-hint">Routes: {{ [...new Set(tableRoutes.map((row) => row.route))].join(', ') }}</p>
              </div>
              <RuleLinksPanel v-if="runtimeProject.project_id && tableRuleIds.length" class="sc-mt" compact :project-id="runtimeProject.project_id" :revision-id="runtimeProject.revision_id" :symbol-ids="tableRuleIds" />
              <div class="sc-fields">
                <div v-for="field in selectedTable.fields" :key="field.id" class="sc-field">
                  <p class="sc-field-top">
                    <KeyRound v-if="field.primaryKey" class="sc-pk" aria-label="Primary key" />
                    <Link2 v-else-if="field.foreignKey" class="sc-fk" aria-label="Foreign key" />
                    <span class="sc-field-name">{{ field.name }}</span>
                    <span class="sc-field-type" :title="field.type">{{ field.type }}</span>
                  </p>
                  <p class="sc-field-tags">
                    <span v-if="field.primaryKey" class="sc-tag">primary key</span>
                    <span v-if="field.references" class="sc-tag">FK → {{ field.references.table }}.{{ field.references.column }}</span>
                    <span class="sc-tag">{{ field.nullable ? 'nullable' : 'required' }}</span>
                    <span v-if="formatDefault(field.default) !== '—'" class="sc-tag">default {{ formatDefault(field.default) }}</span>
                    <span v-if="field.unique" class="sc-tag">unique</span>
                  </p>
                </div>
              </div>
            </div>

            <div v-else-if="selectedRef" class="sc-card">
              <div class="sc-detail-head"><Link2 aria-hidden="true" /><b>{{ selectedRef.cardinality }} relationship</b><button type="button" class="sc-btn sc-btn-icon sc-btn-sm sc-push" aria-label="Close relationship details" @click="selectedRefId = ''"><X aria-hidden="true" /></button></div>
              <p class="sc-ref-text">{{ selectedRef.description || `${tableName(selectedRef.sourceTable)}.${selectedRef.sourceColumn} → ${tableName(selectedRef.targetTable)}.${selectedRef.targetColumn}` }}</p>
              <p class="sc-hint">Provenance: {{ selectedRef.provenance || 'static' }}</p>
            </div>
            <div v-else class="sc-card sc-hint sc-m0">Select a table or relationship for details.</div>

            <p class="sc-hint sc-source">Source: {{ schema.source.framework || schema.source.kind || 'unknown' }} · {{ schema.tables.length }} tables · {{ schema.refs.length }} refs</p>
          </template>
        </aside>
      </div>
    </div>
    <ConfirmModal :show="!!deleteTarget" title="Remove analyzed project?" :message="deleteTarget ? `This removes ${deleteTarget.label || deleteTarget.filename} and its saved analysis artifacts. This cannot be undone.` : ''" confirm-label="Remove project" :cancel-label="deletingAnalysis ? 'Please wait…' : 'Cancel'" @confirm="deleteAnalysis" @cancel="deleteTarget = null" />
  </section>
</template>
