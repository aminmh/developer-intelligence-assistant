<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import StructuredSchemaEditor from './StructuredSchemaEditor.vue';
import { attachmentFilename } from '../utils/migrationPreview';
import type { NormalizedSchema } from '../types';
import { makeRequest } from '../ui/makeRequest';
import { importDraftSchema } from '../utils/schemaDraft';
import { cacheRejectedLayout, cachedRejectedLayout, editorUrl, mayReplaceRejectedLayout, readEditorEvent, rejectedLayoutStored, storeRejectedLayout, validLayout } from '../utils/drawioBridge';
import { createDrawioDiagram, initializeDrawioMetadata, rebuildDrawioDiagram, schemaFromDrawio } from '../utils/drawioDiagram';
import { readMigrationRequest, type DrawioMigrationRequest } from '../utils/drawioMigrationBridge';
import { trackSchemaIdentities } from '../utils/schemaIdentity';

const props = defineProps<{ schema: NormalizedSchema | null; sourceType: string; analysisId: string; runtimeCounts: Record<string, number> }>();
type Draft = { id: string; analysis_id: string | null; source_kind: string; revision: number; schema: unknown; layout: string };
type DraftRow = Pick<Draft, 'id' | 'analysis_id' | 'source_kind' | 'revision'>;
const rows = ref<DraftRow[]>([]);
const selected = ref('');
const current = ref<Draft | null>(null);
const text = ref('');
const savedText = ref('');
const layout = ref('');
const savedLayout = ref('');
const rejectedLayout = ref('');
const editorActive = ref(false);
let popup: Window | null = null;
let editorSchemaBaseline: unknown = null;
let editorIdentities = new Map<string, string>();
let handshakeTimer: ReturnType<typeof setTimeout> | undefined;
let editorReady = false;
const migrationBusy = ref(false);
let migrationFinished: Promise<void> = Promise.resolve();
let editorSaveGeneration = 0;
let saveQueue: Promise<void> = Promise.resolve();
const queuedEditorSaves = ref(0);
let editorPreview: { xml: string; draftId: string; revision: number; request: MigrationRequest; preview: Preview } | null = null;
const busy = ref(false);
const message = ref('');
const error = ref('');
const draftRecoveryWarning = ref('');
const rejectedRecoveryWarning = ref('');
const dirty = computed(() => !!current.value && (text.value !== savedText.value || layout.value !== savedLayout.value));
const framework = ref('laravel');
const database = ref('mysql');
const timestamp = ref('');
const confirmation = ref(false);
type MigrationRequest = { framework: string; database: string; revision: number; migration_timestamp: string };
type Preview = { operations: unknown[]; files: { name: string; content: string }[]; warnings: string[]; generation_blocked: boolean; generation_error: string | null; requires_confirmation: boolean; preview_token: string };
const preview = ref<Preview | null>(null);
const previewRequest = ref<MigrationRequest | null>(null);
let previewGeneration = 0;
function clearPreview() { preview.value = null; previewRequest.value = null; confirmation.value = false; previewGeneration++; }
watch([framework, database, timestamp, text, layout, current], clearPreview, { flush: 'sync' });
async function previewMigration() {
  if (!current.value || dirty.value || editorActive.value) return;
  clearPreview(); error.value = ''; message.value = '';
  const draftId = current.value.id;
  const payload = { framework: framework.value, database: database.value, revision: current.value.revision, migration_timestamp: timestamp.value };
  const generation = previewGeneration;
  busy.value = true;
  try {
    const result = await request(`/api/schema-drafts/${encodeURIComponent(draftId)}/migration-preview`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    if (generation === previewGeneration) { preview.value = result; previewRequest.value = payload; }
  } catch (cause: any) { error.value = cause.message || 'Could not preview migration.'; }
  finally { busy.value = false; }
}
async function downloadMigration() {
  if (!current.value || !preview.value || !previewRequest.value || dirty.value || editorActive.value || preview.value.generation_blocked || (preview.value.requires_confirmation && !confirmation.value)) return;
  const { preview_token, ...reviewed_preview } = preview.value;
  const payload = { ...previewRequest.value, confirmed: confirmation.value, preview_token, reviewed_preview };
  busy.value = true; error.value = '';
  try {
    const response = await makeRequest(`/api/schema-drafts/${encodeURIComponent(current.value.id)}/migration-download`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(data.detail || `Download failed (${response.status}).`); }
    const filename = attachmentFilename(response.headers.get('content-disposition'));
    if (!filename) throw new Error('The download response has an invalid filename.');
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a'); link.href = url; link.download = filename; link.click(); URL.revokeObjectURL(url);
  } catch (cause: any) { error.value = cause.message || 'Could not download migration.'; }
  finally { busy.value = false; }
}
const recoveryKey = (id: string) => `schema-draft-recovery:${id}`;
function clearRecovery(id: string) {
  try { sessionStorage.removeItem(recoveryKey(id)); } catch { /* Storage may be unavailable. */ }
}
function rememberRejectedLayout(xml: string) {
  rejectedLayout.value = xml;
  if (!current.value) return;
  const id = current.value.id;
  let stored = false;
  try {
    stored = storeRejectedLayout(sessionStorage, `${recoveryKey(id)}:rejected-layout`, xml);
  } catch { stored = false; }
  cacheRejectedLayout(id, xml, stored);
  rejectedRecoveryWarning.value = stored ? '' : 'Local recovery is unavailable. Download the rejected diagram before leaving this page.';
}
function downloadRejectedLayout() {
  if (!rejectedLayout.value) return;
  const url = URL.createObjectURL(new Blob([rejectedLayout.value], { type: 'application/xml' }));
  const link = document.createElement('a'); link.href = url; link.download = 'rejected-schema-diagram.drawio'; link.click(); URL.revokeObjectURL(url);
}
function rememberEdits() {
  if (!current.value) return;
  try {
    if (dirty.value) sessionStorage.setItem(recoveryKey(current.value.id), JSON.stringify({ text: text.value, layout: layout.value, revision: current.value.revision }));
    else sessionStorage.removeItem(recoveryKey(current.value.id));
    draftRecoveryWarning.value = '';
  } catch { draftRecoveryWarning.value = 'Local recovery is unavailable. Copy unsaved edits before leaving this page.'; }
}
function warnBeforeUnload(event: BeforeUnloadEvent) {
  if (dirty.value || editorActive.value || (rejectedLayout.value && rejectedRecoveryWarning.value)) { event.preventDefault(); event.returnValue = ''; }
}
function closeEditor() {
  if (handshakeTimer) clearTimeout(handshakeTimer);
  window.removeEventListener('message', onEditorMessage);
  popup = null; editorReady = false; editorActive.value = false; editorPreview = null; editorSchemaBaseline = null; editorIdentities = new Map();
}
function sendRuntimeCounts() {
  if (!popup || !editorReady) return;
  const origin = editorUrl(import.meta.env.VITE_DRAWIO_ORIGIN || '', window.location.origin).origin;
  popup.postMessage(JSON.stringify({ action: 'schema-runtime-counts', counts: props.runtimeCounts }), origin);
}
watch(() => props.runtimeCounts, sendRuntimeCounts);
async function handleEditorMigration(event: DrawioMigrationRequest, editor: Window, origin: string) {
  if (!current.value || migrationBusy.value || queuedEditorSaves.value) {
    editor.postMessage(JSON.stringify({ action: 'schema-migration-result', requestId: event.requestId, ok: false, error: migrationBusy.value || queuedEditorSaves.value ? 'A save or migration request is in progress. Try again shortly.' : 'The draft is unavailable.' }), origin);
    return;
  }
  migrationBusy.value = true;
  let finishMigration!: () => void;
  migrationFinished = new Promise(resolve => { finishMigration = resolve; });
  const saveGeneration = editorSaveGeneration;
  const startingDraftId = current.value.id;
  const reply = (payload: Record<string, unknown>) => {
    if (popup === editor && editorReady) editor.postMessage(JSON.stringify({ action: 'schema-migration-result', requestId: event.requestId, ...payload }), origin);
  };
  try {
    if (event.event === 'schema-migration-preview') {
      editorPreview = null;
      if (!validLayout(event.xml)) throw new Error('The diagram is invalid or too large.');
      const schema = schemaFromDrawio(event.xml, editorSchemaBaseline, true);
      trackSchemaIdentities(schema, editorIdentities);
      const draftId = current.value.id;
      const previousText = text.value;
      const previousLayout = layout.value;
      const draft = await request(`/api/schema-drafts/${encodeURIComponent(draftId)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ schema, layout: event.xml, revision: current.value.revision }) });
      if (!current.value || current.value.id !== draftId) return;
      if (editorSaveGeneration !== saveGeneration || text.value !== previousText || layout.value !== previousLayout) {
        current.value = draft;
        savedText.value = JSON.stringify(draft.schema, null, 2);
        savedLayout.value = draft.layout;
        rememberEdits();
        throw new Error('The diagram was saved again during preview. Your newer changes are preserved; generate a new preview.');
      }
      clearRecovery(draftId); adopt(draft); rememberRejectedLayout('');
      if (popup !== editor || !editorReady) return;
      const migrationRequest = { framework: event.framework, database: event.database, revision: draft.revision, migration_timestamp: event.timestamp };
      const result = await request(`/api/schema-drafts/${encodeURIComponent(draftId)}/migration-preview`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(migrationRequest) });
      if (popup !== editor || !current.value || current.value.id !== draftId || current.value.revision !== draft.revision) return;
      editorPreview = { xml: event.xml, draftId, revision: draft.revision, request: migrationRequest, preview: result };
      reply({ ok: true, preview: result });
    } else {
      const saved = editorPreview;
      if (!saved || saved.xml !== event.xml || saved.draftId !== current.value.id || saved.revision !== current.value.revision) throw new Error('The diagram or draft changed. Generate a new preview.');
      if (saved.preview.generation_blocked || (saved.preview.requires_confirmation && !event.confirmed)) throw new Error('Review and confirm the changes before downloading.');
      const { preview_token, ...reviewed_preview } = saved.preview;
      const response = await makeRequest(`/api/schema-drafts/${encodeURIComponent(saved.draftId)}/migration-download`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...saved.request, confirmed: event.confirmed, preview_token, reviewed_preview }) });
      if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(data.detail || `Download failed (${response.status}).`); }
      const filename = attachmentFilename(response.headers.get('content-disposition'));
      if (!filename) throw new Error('The download response has an invalid filename.');
      const content = await response.text();
      if (new TextEncoder().encode(content).length > 1024 * 1024) throw new Error('The migration file is too large.');
      reply({ ok: true, filename, content });
    }
  } catch (cause: any) {
    if (event.event === 'schema-migration-preview' && editorSaveGeneration === saveGeneration && popup === editor && current.value?.id === startingDraftId) rememberRejectedLayout(event.xml);
    reply({ ok: false, error: cause.message || 'Migration request failed.' });
  } finally { migrationBusy.value = false; finishMigration(); }
}
function disconnectEditor() {
  if (window.confirm('Disconnect Draw.io? Changes not yet saved in Draw.io will not reach this draft.')) closeEditor();
}
function queueEditorSave(draftId: string, schema: unknown, xml: string, editor: Window, origin: string) {
  queuedEditorSaves.value++;
  saveQueue = saveQueue.then(async () => {
    await migrationFinished;
    if (!current.value || current.value.id !== draftId) throw new Error('The draft changed before the editor save completed.');
    const draft = await request(`/api/schema-drafts/${encodeURIComponent(draftId)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ schema, layout: xml, revision: current.value.revision }) });
    if (!current.value || current.value.id !== draftId) return;
    current.value = draft;
    savedText.value = JSON.stringify(draft.schema, null, 2);
    savedLayout.value = draft.layout;
    rememberEdits();
    message.value = dirty.value ? 'Draft saved; newer edits remain unsaved.' : 'Draw.io changes saved to the draft.';
  }).catch((cause: any) => {
    error.value = `${cause.message || 'Could not save the Draw.io draft.'} Your edits remain available for recovery.`;
    if (popup === editor && editorReady) editor.postMessage(JSON.stringify({ action: 'schema-draft-save-result', ok: false, error: error.value }), origin);
    rememberEdits();
  }).finally(() => { queuedEditorSaves.value--; });
}
function onEditorMessage(event: MessageEvent) {
  if (!popup) return;
  const origin = editorUrl(import.meta.env.VITE_DRAWIO_ORIGIN || '', window.location.origin).origin;
  const migrationRequest = readMigrationRequest(event, origin, popup);
  if (migrationRequest && editorReady) { void handleEditorMigration(migrationRequest, popup, origin); return; }
  const messageData = readEditorEvent(event, origin, popup);
  if (!messageData) return;
  if (editorReady && (messageData.event === 'save' || messageData.event === 'invalid' || messageData.event === 'oversize')) editorSaveGeneration++;
  if (messageData.event === 'init' && !editorReady) {
    editorReady = true;
    if (handshakeTimer) clearTimeout(handshakeTimer);
    popup.postMessage(JSON.stringify({ action: 'load', xml: layout.value, autosave: 0 }), origin);
    popup.postMessage(JSON.stringify({ action: 'schema-migration-host' }), origin);
    sendRuntimeCounts();
    message.value = 'Draw.io is open. Save there to update the draft.';
  } else if (messageData.event === 'oversize' && editorReady) {
    error.value = 'The editor message exceeds the size limit and was not retained. Reduce the diagram size in Draw.io and save again; the last valid draft is preserved.';
    popup.postMessage(JSON.stringify({ action: 'schema-draft-save-result', ok: false, error: error.value }), origin);
  } else if (messageData.event === 'invalid' && editorReady) {
    rememberRejectedLayout(messageData.xml);
    error.value = 'The diagram XML is invalid or contains a forbidden document type. The last valid draft is preserved; correct the diagram or download it for recovery.';
    popup.postMessage(JSON.stringify({ action: 'schema-draft-save-result', ok: false, error: error.value }), origin);
  } else if (messageData.event === 'save' && editorReady) {
    try {
      const schema = schemaFromDrawio(messageData.xml, editorSchemaBaseline, true);
      trackSchemaIdentities(schema, editorIdentities);
      text.value = JSON.stringify(schema, null, 2);
      layout.value = messageData.xml;
      rememberEdits(); error.value = ''; rememberRejectedLayout('');
      if (current.value) queueEditorSave(current.value.id, schema, messageData.xml, popup, origin);
      if (messageData.exit) { popup.close(); closeEditor(); }
      message.value = 'Saving the schema and diagram to the draft…';
    } catch (cause: any) {
      rememberRejectedLayout(messageData.xml);
      error.value = `${cause.message || 'Could not read the diagram.'} The last valid schema and layout are preserved. Correct the diagram and save again, or download the rejected diagram for recovery.`;
      popup.postMessage(JSON.stringify({ action: 'schema-draft-save-result', ok: false, error: error.value }), origin);
    }
  } else if (messageData.event === 'exit' && editorReady) {
    closeEditor();
    message.value = queuedEditorSaves.value ? 'Draw.io closed. Saving the draft…' : 'Draw.io closed.';
  }
}
function openEditor() {
  if (!current.value || editorActive.value || queuedEditorSaves.value || migrationBusy.value) return;
  error.value = ''; message.value = '';
  try {
    if (text.value !== savedText.value) throw new Error('Save structured schema edits before opening Draw.io.');
    const url = editorUrl(import.meta.env.VITE_DRAWIO_ORIGIN || '', window.location.origin);
    const savedSchema = JSON.parse(savedText.value);
    const identities = new Map<string, string>();
    trackSchemaIdentities(savedSchema, identities);
    const initialLayout = layout.value || createDrawioDiagram(savedSchema);
    if (!validLayout(initialLayout)) throw new Error('The stored diagram is invalid or too large. The draft was not changed.');
    const diagramSchema = schemaFromDrawio(initialLayout, savedSchema);
    if (JSON.stringify(diagramSchema) !== JSON.stringify(savedSchema)) throw new Error('The stored diagram does not match the saved schema. Rebuild the diagram before editing.');
    const initializedLayout = initializeDrawioMetadata(initialLayout, savedSchema);
    if (!validLayout(initializedLayout)) throw new Error('The diagram with schema metadata is invalid or too large.');
    popup = window.open(url.toString(), '_blank');
    if (!popup) throw new Error('The browser blocked Draw.io. Allow popups and retry.');
    editorSchemaBaseline = savedSchema;
    editorIdentities = identities;
    layout.value = initializedLayout;
    rememberEdits();
    editorActive.value = true; editorReady = false;
    window.addEventListener('message', onEditorMessage);
    handshakeTimer = setTimeout(() => {
      if (!editorReady) { closeEditor(); error.value = 'Draw.io did not connect. Check the editor service and browser isolation, then retry. The draft is preserved.'; }
    }, 15000);
  } catch (cause: any) { error.value = cause.message || 'Could not open Draw.io.'; }
}
function rebuildLayout() {
  if (!current.value || editorActive.value || queuedEditorSaves.value || migrationBusy.value || text.value !== savedText.value) return;
  error.value = ''; message.value = '';
  try {
    const rebuilt = rebuildDrawioDiagram(layout.value, current.value.schema, () => window.confirm('Rebuild the diagram from the saved schema? Existing positions and annotations will be lost.'));
    if (rebuilt === layout.value) return;
    if (!validLayout(rebuilt)) throw new Error('The rebuilt diagram is invalid or too large. The existing layout was preserved.');
    layout.value = rebuilt; rememberEdits();
    message.value = 'Diagram rebuilt from the saved schema. Save the draft to keep it.';
  } catch (cause: any) { error.value = cause.message || 'Could not rebuild the diagram. The existing layout was preserved.'; }
}

async function request(path: string, init?: RequestInit) {
  const response = await makeRequest(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.error || `Request failed (${response.status}).`);
  return data;
}
async function refresh() {
  try { rows.value = (await request('/api/schema-drafts')).drafts; }
  catch (cause: any) { error.value = cause.message || 'Could not load drafts.'; }
}
function adopt(draft: Draft, recover = false) {
  const sameDraft = current.value?.id === draft.id;
  if (!sameDraft && current.value && rejectedRecoveryWarning.value) cacheRejectedLayout(current.value.id, '');
  current.value = draft; selected.value = draft.id;
  text.value = JSON.stringify(draft.schema, null, 2); savedText.value = text.value;
  layout.value = draft.layout; savedLayout.value = draft.layout;
  if (!sameDraft) {
    rejectedLayout.value = '';
    rejectedRecoveryWarning.value = '';
    rejectedLayout.value = cachedRejectedLayout(draft.id);
    if (rejectedLayout.value && !rejectedLayoutStored(draft.id)) rejectedRecoveryWarning.value = 'Local recovery is unavailable. Download the rejected diagram before leaving this page.';
    else {
      try {
        if (!rejectedLayout.value) {
          rejectedLayout.value = sessionStorage.getItem(`${recoveryKey(draft.id)}:rejected-layout`) || '';
          cacheRejectedLayout(draft.id, rejectedLayout.value, true);
        }
      }
      catch { /* Recovery storage may be unavailable. */ }
    }
  }
  if (recover) {
    try {
      const pending = JSON.parse(sessionStorage.getItem(recoveryKey(draft.id)) || 'null');
      if (pending && typeof pending.text === 'string' && Number.isInteger(pending.revision)) {
        text.value = pending.text;
        if (typeof pending.layout === 'string') layout.value = pending.layout;
        current.value = { ...draft, revision: pending.revision };
        message.value = 'Recovered unsaved edits from this browser tab. Save them or copy them before discarding.';
      }
    } catch { error.value = 'Could not restore local draft recovery.'; }
  }
}
function canReplace() {
  if (queuedEditorSaves.value || migrationBusy.value) { error.value = 'Wait for the current save or migration request to finish.'; return false; }
  if (editorActive.value) { error.value = 'Close the Draw.io session before switching drafts.'; return false; }
  if (!mayReplaceRejectedLayout(rejectedLayout.value, !rejectedRecoveryWarning.value, () => window.confirm('Local recovery is unavailable. Discard the rejected diagram and switch drafts? Download it first if you want to keep it.'))) return false;
  if (!dirty.value) return true;
  if (!window.confirm('Discard unsaved draft changes?')) return false;
  return true;
}
async function openDraft(id = selected.value) {
  if (!id || !canReplace()) return;
  busy.value = true; error.value = ''; message.value = '';
  try {
    const draft = await request(`/api/schema-drafts/${encodeURIComponent(id)}`);
    if (current.value && dirty.value) clearRecovery(current.value.id);
    adopt(draft, true); if (!message.value) message.value = 'Draft opened.';
  }
  catch (cause: any) { error.value = cause.message || 'Could not open draft.'; }
  finally { busy.value = false; }
}
async function createDraft() {
  if (!props.schema || !canReplace()) return;
  busy.value = true; error.value = ''; message.value = '';
  try {
    const saved = props.sourceType === 'saved';
    if (saved && !props.analysisId) throw new Error('Select an analysis first.');
    const path = saved ? `/api/schema-drafts/analysis/${encodeURIComponent(props.analysisId)}` : '/api/schema-drafts/import';
    const body = saved ? undefined : JSON.stringify({ schema: importDraftSchema(props.schema) });
    const draft = await request(path, { method: 'POST', ...(body ? { headers: { 'Content-Type': 'application/json' }, body } : {}) });
    if (current.value && dirty.value) clearRecovery(current.value.id);
    adopt(draft); message.value = 'Draft created. Its baseline remains unchanged.'; await refresh();
  } catch (cause: any) { error.value = cause.message || 'Could not create draft.'; }
  finally { busy.value = false; }
}
async function saveDraft() {
  if (!current.value || editorActive.value || queuedEditorSaves.value || migrationBusy.value) return;
  busy.value = true; error.value = ''; message.value = '';
  try {
    if (layout.value && !validLayout(layout.value)) throw new Error('The diagram layout is invalid or too large.');
    let schema: unknown;
    try { schema = JSON.parse(text.value); } catch { throw new Error('The draft is not valid JSON. Your edits are still here.'); }
    const draft = await request(`/api/schema-drafts/${encodeURIComponent(current.value.id)}`, { method: 'PUT',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ schema, layout: layout.value, revision: current.value.revision }) });
    clearRecovery(draft.id); adopt(draft); message.value = 'Draft saved.'; await refresh();
  } catch (cause: any) { error.value = `${cause.message || 'Could not save draft.'} Your edits remain in the editor; copy them before reopening a newer revision.`; }
  finally { busy.value = false; }
}
onMounted(() => { window.addEventListener('beforeunload', warnBeforeUnload); void refresh(); });
onUnmounted(() => { closeEditor(); window.removeEventListener('beforeunload', warnBeforeUnload); });
</script>

<template>
  <details class="rounded-2xl border border-slate-800 bg-slate-900/30 p-3 text-xs text-slate-300">
    <summary class="cursor-pointer font-semibold text-slate-200">Schema drafts</summary>
    <p class="mt-2 text-slate-400">Draft access requires a global administrator. The diagram below shows the original source, not draft edits. Schema coverage is unverified; imported indexes and complex constraints may be incomplete.</p>
    <div class="mt-3 flex flex-wrap items-center gap-2">
      <button type="button" :disabled="!schema || busy" @click="createDraft" class="rounded-lg border border-indigo-400/30 px-3 py-2 disabled:opacity-50">Create draft from current source</button>
      <label for="schema-draft-select">Saved draft</label>
      <select id="schema-draft-select" v-model="selected" :disabled="busy" class="max-w-64 rounded-lg border border-slate-700 bg-slate-950 px-2 py-2"><option value="">Choose a draft</option><option v-for="row in rows" :key="row.id" :value="row.id">{{ row.source_kind }} · {{ row.analysis_id || row.id }} · revision {{ row.revision }}</option></select>
      <button type="button" :disabled="!selected || busy" @click="openDraft()" class="rounded-lg border border-slate-700 px-3 py-2 disabled:opacity-50">Open</button>
      <button type="button" :disabled="busy" @click="refresh" class="rounded-lg border border-slate-700 px-3 py-2 disabled:opacity-50">Refresh drafts</button>
    </div>
    <div v-if="current" class="mt-3 space-y-2">
      <p>Structured schema · revision {{ current.revision }} <span v-if="dirty" class="text-amber-300">· unsaved changes</span></p>
      <StructuredSchemaEditor v-model="text" :disabled="busy || editorActive || queuedEditorSaves > 0 || migrationBusy" @update:model-value="rememberEdits" />
      <details><summary class="cursor-pointer">Edit schema JSON</summary>
      <label for="schema-draft-json" class="block">Schema JSON</label>
      <textarea id="schema-draft-json" v-model="text" @input="rememberEdits" :disabled="busy || editorActive || queuedEditorSaves > 0 || migrationBusy" spellcheck="false" class="h-48 w-full rounded-lg border border-slate-700 bg-slate-950 p-3 font-mono text-xs text-slate-200" />
      </details>
      <button type="button" :disabled="busy || editorActive || queuedEditorSaves > 0 || migrationBusy || !dirty" @click="saveDraft" class="rounded-lg border border-indigo-400/30 px-3 py-2 disabled:opacity-50">Save draft</button>
      <button type="button" :disabled="busy || editorActive || queuedEditorSaves > 0 || migrationBusy" @click="openEditor" class="ml-2 rounded-lg border border-indigo-400/30 px-3 py-2 disabled:opacity-50">Open Draw.io editor</button>
      <button v-if="editorActive" type="button" @click="disconnectEditor" class="ml-2 rounded-lg border border-slate-700 px-3 py-2">Disconnect editor</button>
      <button v-if="rejectedLayout" type="button" @click="downloadRejectedLayout" class="ml-2 rounded-lg border border-amber-400/50 px-3 py-2">Download rejected diagram for recovery</button>
      <div v-if="layout" class="rounded-lg border border-amber-500/60 bg-amber-950/30 p-3 text-amber-200">
        <p>The existing Draw.io layout is not automatically synchronized with the schema and may be outdated. It may omit new entities or retain removed ones.</p>
        <p>Rebuilding uses the saved schema and replaces existing positions and annotations. Save schema edits first.</p>
        <button type="button" :disabled="busy || editorActive || queuedEditorSaves > 0 || migrationBusy || text !== savedText" @click="rebuildLayout" class="mt-2 rounded-lg border border-amber-400/50 px-3 py-2 disabled:opacity-50">Rebuild layout from saved schema</button>
      </div>
      <p class="text-slate-400">In Draw.io, use Extras to edit column properties, table keys and indexes, and relationships, or to preview a migration. Edit table and column labels directly. Save in Draw.io to update the draft. The property editors use JSON and stable IDs. Leaving this view disconnects the editor.</p>
      <fieldset :disabled="busy || editorActive || queuedEditorSaves > 0 || migrationBusy" class="space-y-2 rounded-lg border border-slate-700 p-3">
        <legend>Migration preview</legend>
        <p class="text-amber-300">The baseline is unverified. Compare it with the target database and review the model-generated code before use. Validation accepts only a narrow set of supported migration operations.</p>
        <label class="mr-3">Framework <select v-model="framework" class="rounded border border-slate-700 bg-slate-950 p-1"><option value="laravel">Laravel</option><option value="symfony">Symfony / Doctrine</option></select></label>
        <label class="mr-3">Database <select v-model="database" class="rounded border border-slate-700 bg-slate-950 p-1"><option value="mysql">MySQL</option><option value="postgresql">PostgreSQL</option></select></label>
        <label>Migration timestamp (YYYY_MM_DD_HHMMSS) <input v-model="timestamp" placeholder="2026_11_02_120000" class="rounded border border-slate-700 bg-slate-950 p-1"></label>
        <p v-if="dirty" class="text-amber-300">Save the draft before generating a preview.</p>
        <button type="button" :disabled="dirty || !timestamp" @click="previewMigration" class="block rounded-lg border border-indigo-400/30 px-3 py-2 disabled:opacity-50">Preview saved revision</button>
        <div v-if="preview" class="space-y-2">
          <p v-if="preview.generation_blocked" role="alert" class="text-rose-300">Generation blocked: {{ preview.generation_error || 'No migration can be generated.' }}</p>
          <ul class="list-disc pl-5 text-amber-300"><li v-for="warning in preview.warnings" :key="warning">{{ warning }}</li></ul>
          <h4 class="font-semibold">Operations (before and after)</h4>
          <p v-if="!preview.operations.length">No changes detected.</p>
          <pre v-for="(operation, index) in preview.operations" :key="index" class="overflow-auto whitespace-pre-wrap rounded bg-slate-950 p-2">{{ JSON.stringify(operation, null, 2) }}</pre>
          <div v-for="file in preview.files" :key="file.name"><h4 class="font-semibold">{{ file.name }}</h4><pre class="max-h-80 overflow-auto rounded bg-slate-950 p-2">{{ file.content }}</pre></div>
          <label v-if="preview.requires_confirmation" class="block text-amber-300"><input v-model="confirmation" type="checkbox"> I reviewed the destructive or ambiguous changes in this preview.</label>
          <button type="button" :disabled="preview.generation_blocked || (preview.requires_confirmation && !confirmation)" @click="downloadMigration" class="rounded-lg border border-indigo-400/30 px-3 py-2 disabled:opacity-50">Download previewed migration</button>
        </div>
      </fieldset>
    </div>
    <p v-if="message" role="status" class="mt-2 text-emerald-300">{{ message }}</p>
    <p v-if="error" role="alert" class="mt-2 text-rose-300">{{ error }}</p>
    <p v-if="draftRecoveryWarning" role="alert" class="mt-2 text-amber-300">{{ draftRecoveryWarning }}</p>
    <p v-if="rejectedRecoveryWarning" role="alert" class="mt-2 text-amber-300">{{ rejectedRecoveryWarning }}</p>
  </details>
</template>
