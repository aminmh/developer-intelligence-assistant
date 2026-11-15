<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import {
  Activity, AlertTriangle, Archive, Braces, ChevronDown, ChevronsUpDown, Download, Eraser, FileCode, FileJson, GitMerge, KeyRound,
  Link2, Minus, PanelRightClose, PanelRightOpen, Play, RefreshCw, Search, Sparkles, Table, Table2, Trash2, X,
} from 'lucide-vue-next';
import { makeRequest } from '../ui/makeRequest';
import { useProjectScope } from '../composables/useProjectScope';
import { subscribeRuntimeUpdates } from '../ui/runtimeEvents';
import type { AnalysisSession, ErdRef, ErdTable, NormalizedSchema, RuntimeProjectRecord, RuntimeSnapshotData, SchemaVisualizationResponse } from '../types';
import { normalizeSavedSchema, parseSchemaInput, schemaToDbml, schemaToJson, type SchemaInputFormat } from '../utils/erdTransformer';
import ConfirmModal from './ConfirmModal.vue';
import SchemaDraftPanel from './SchemaDraftPanel.vue';
import RuntimeSchemaLoader from './runtime/RuntimeSchemaLoader.vue';
import OpenDrawioButton from './runtime/OpenDrawioButton.vue';
import type { SavedSchemaDraft } from '../types';
import RuleLinksPanel from './code-quality/RuleLinksPanel.vue';
import type { SchemaSelection } from '../utils/drawioBridge';

const draftPanel = ref<InstanceType<typeof SchemaDraftPanel> | null>(null);
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
const error = ref('');
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
const tableOps = (table: ErdTable) => busyTableCounts.value[table.name.toLowerCase()] || 0;
const maxTableOps = computed(() => Math.max(1, ...Object.values(busyTableCounts.value).map(Number)));
const formatOps = (value: number) => (value >= 1000 ? `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}k` : String(value));
const tableRuntimeTotal = computed(() => tableRuntime.value.reduce((sum, row) => sum + Number(row.operations || 0), 0));
const tableRuntimeMax = computed(() => Math.max(1, ...tableRuntime.value.map((row) => Number(row.operations || 0))));
</script>

<template>
  <section class="sc-page" aria-label="Database schema">
    <div class="sc-wrap">
      <!-- Toolbar -->
      <div class="sc-bar">
        <OpenDrawioButton />
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

      <SchemaDraftPanel ref="draftPanel" :schema="schema" :source-type="sourceType" :analysis-id="selectedAnalysisId" :runtime-counts="busyTableCounts" :selection="selectedEntity" @select-entity="selectEditorEntity" />

      <div v-if="runtimeError" class="sc-banner" data-tone="warn"><AlertTriangle aria-hidden="true" /><span>{{ runtimeError }}</span></div>
      <div v-if="error" class="sc-banner" data-tone="bad" role="alert"><AlertTriangle aria-hidden="true" /><span>{{ error }}</span></div>
      <div v-else-if="loading" class="sc-card sc-loading" role="status"><RefreshCw class="sc-spin" aria-hidden="true" />Loading schema…</div>
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
            <h2 id="sc-schema-title" class="sc-sec-title"><Table2 aria-hidden="true" />Original schema</h2>
            <span class="sc-tag">{{ schema.tables.length }} tables</span>
            <span class="sc-tag">{{ schema.refs.length }} relationships</span>
            <span v-if="schema.warnings.length" class="sc-tag" data-tone="warn"><AlertTriangle aria-hidden="true" />{{ schema.warnings.length }} {{ schema.warnings.length === 1 ? 'warning' : 'warnings' }}</span>
            <div class="sc-push sc-row">
              <button type="button" class="sc-btn sc-btn-sm" @click="exportDbml"><Download aria-hidden="true" />DBML</button>
              <button type="button" class="sc-btn sc-btn-sm" @click="exportJson"><Download aria-hidden="true" />JSON</button>
            </div>
          </div>
          <p class="sc-hint">This list shows the original source. Open a draft in Draw.io to see and edit the diagram.</p>
          <label class="sc-search"><Search aria-hidden="true" /><span class="sr-only">Search tables and columns</span><input v-model="search" placeholder="Search tables and columns…" /></label>
          <p class="sc-hint">{{ visibleTables.length }} of {{ schema.tables.length }} tables shown</p>

          <div class="sc-tables">
            <button
              v-for="table in visibleTables"
              :key="table.id"
              type="button"
              class="sc-table"
              :aria-pressed="selectedTableId === table.id"
              @click="selectTable(table)"
            >
              <span class="sc-table-head">
                <Table aria-hidden="true" />
                <b>{{ table.schema ? table.schema + '.' : '' }}{{ table.name }}</b>
                <span v-if="tableOps(table)" class="sc-heat" :style="{ opacity: 0.55 + 0.45 * (tableOps(table) / maxTableOps) }" :title="`${tableOps(table).toLocaleString()} observed operations`">{{ formatOps(tableOps(table)) }}</span>
              </span>
              <span class="sc-cols">
                <span v-for="field in table.fields.slice(0, PREVIEW_COLUMNS)" :key="field.id" class="sc-col" :data-key="field.primaryKey ? 'pk' : field.foreignKey ? 'fk' : undefined">
                  <KeyRound v-if="field.primaryKey" aria-label="Primary key" />
                  <Link2 v-else-if="field.foreignKey" aria-label="Foreign key" />
                  <Minus v-else aria-hidden="true" />
                  <span class="sc-col-name">{{ field.name }}</span>
                  <span class="sc-col-type">{{ field.type }}</span>
                </span>
                <span v-if="table.fields.length > PREVIEW_COLUMNS" class="sc-col-more">+ {{ table.fields.length - PREVIEW_COLUMNS }} more {{ table.fields.length - PREVIEW_COLUMNS === 1 ? 'column' : 'columns' }}</span>
              </span>
            </button>
          </div>
          <p v-if="!visibleTables.length" class="sc-hint">No matching tables.</p>

          <template v-if="schema.refs.length">
            <div class="sc-sec-head sc-mt"><h3 class="sc-sec-title sc-sec-title-sm"><GitMerge aria-hidden="true" />Relationships</h3><span class="sc-tag">{{ schema.refs.length }}</span></div>
            <div class="sc-rels">
              <button v-for="relation in schema.refs" :key="relation.id" type="button" class="sc-rel" :aria-pressed="selectedRefId === relation.id" @click="selectRef(relation)">
                <Link2 aria-hidden="true" />
                <span class="sc-rel-text"><b>{{ tableLabel(relation.sourceTable) }}</b>.{{ relation.sourceColumn }} → <b>{{ tableLabel(relation.targetTable) }}</b>.{{ relation.targetColumn }}</span>
                <span v-if="relation.cardinality" class="sc-rel-card">{{ relation.cardinality }}</span>
              </button>
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
                    <span>{{ field.name }}</span>
                    <span class="sc-push sc-field-type">{{ field.type }}</span>
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
              <p class="sc-ref-text">{{ selectedRef.description || `${tableLabel(selectedRef.sourceTable)}.${selectedRef.sourceColumn} → ${tableLabel(selectedRef.targetTable)}.${selectedRef.targetColumn}` }}</p>
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
