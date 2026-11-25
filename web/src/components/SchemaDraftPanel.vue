<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import StructuredSchemaEditor from './StructuredSchemaEditor.vue';
import { AlertTriangle, Check, ChevronDown, Download, ExternalLink, Eye, FileCode, GitCompare, PencilRuler, Plus, RefreshCw, Save, Trash2, Unplug } from 'lucide-vue-next';
import { attachmentFilename } from '../utils/migrationPreview';
import type { NormalizedSchema, StructuredSchemaDraft, SavedSchemaDraft } from '../types';
import { makeRequest } from '../ui/makeRequest';
import { toast, toastMessage } from '../composables/useToasts';
import { importDraftSchema } from '../utils/schemaDraft';
import { cacheRejectedLayout, cachedRejectedLayout, editorUrl, MAX_LAYOUT_BYTES, mayReplaceRejectedLayout, readEditorEvent, readEditorSelection, rejectedLayoutStored, storeRejectedLayout, validLayout, type SchemaSelection } from '../utils/drawioBridge';
import { createDrawioDiagram, initializeDrawioMetadata, rebuildDrawioDiagram, schemaFromDrawio } from '../utils/drawioDiagram';
import { readMigrationRequest, readSchemaExportRequest, readInspectionRequest, type DrawioMigrationRequest, type DrawioSchemaExportRequest } from '../utils/drawioMigrationBridge';
import { trackSchemaIdentities } from '../utils/schemaIdentity';
import { structuredSchemaToDbml } from '../utils/schemaDbml';
import { sameStructuredSchema } from '../utils/schemaChanges';

const props = defineProps<{ schema: NormalizedSchema | null; /** True when the explorer is showing the open draft itself rather than an analysis or import. */ schemaIsDraft?: boolean; sourceType: string; analysisId: string; runtimeCounts: Record<string, number>; selection: SchemaSelection | null }>();
// `editorSchema` carries what is in Draw.io right now next to the saved draft it started from (null when the editor
// closes). It is never saved: it only feeds "Show new changes", and a draft is saved only when the developer chooses
// Save draft. `draftSchema` is the saved draft itself, so the explorer can show it when no other source is loaded.
const emit = defineEmits<{ selectEntity: [selection: SchemaSelection]; editorSchema: [state: { baseline: unknown; schema: unknown } | null]; draftSchema: [schema: unknown | null]; status: [status: DraftStatus] }>();
// What the explorer shows next to the schema, so the manual-save state is visible even while this panel is closed.
export type DraftStatus = { hasDraft: boolean; dirty: boolean; editorActive: boolean; busy: boolean; revision: number };
type Draft = SavedSchemaDraft;
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
let closedWatcher: ReturnType<typeof setInterval> | undefined;
let editorReady = false;
const migrationBusy = ref(false);
let liveTimer: ReturnType<typeof setTimeout> | undefined;
let lastLiveSchema: unknown;
let editorSaveGeneration = 0;
let editorPreview: { xml: string; draftId: string; revision: number; request: MigrationRequest; preview: Preview } | null = null;
const busy = ref(false);
const message = toastMessage('success');
const error = toastMessage('error');
class UnsavedDraftError extends Error {}
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
const previewSection = ref<HTMLElement | null>(null);
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
    if (generation === previewGeneration) {
      preview.value = result; previewRequest.value = payload;
      // The preview (or the reason it was blocked) is rendered far below the Preview button: bring it into view.
      await nextTick();
      previewSection.value?.scrollIntoView({ block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
      previewSection.value?.focus({ preventScroll: true });
    }
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
  if (closedWatcher) clearInterval(closedWatcher);
  if (liveTimer) clearTimeout(liveTimer);
  closedWatcher = undefined; liveTimer = undefined; lastLiveSchema = undefined;
  window.removeEventListener('message', onEditorMessage);
  emit('editorSchema', null);
  popup = null; editorReady = false; editorActive.value = false; editorPreview = null; editorSchemaBaseline = null; editorIdentities = new Map();
}
function sendRuntimeCounts() {
  if (!popup || !editorReady) return;
  const origin = editorUrl(import.meta.env.VITE_DRAWIO_ORIGIN || '', window.location.origin).origin;
  popup.postMessage(JSON.stringify({ action: 'schema-runtime-counts', counts: props.runtimeCounts }), origin);
}
function revealSelection() {
  if (!popup || !editorReady || !props.selection) return;
  const origin = editorUrl(import.meta.env.VITE_DRAWIO_ORIGIN || '', window.location.origin).origin;
  popup.postMessage(JSON.stringify({ action: 'schema-reveal-selection', ...props.selection }), origin);
  popup.focus();
}
watch(() => props.runtimeCounts, sendRuntimeCounts);
// Draw.io reports every edit. Read it after a short pause and hand the schema to the explorer; nothing is saved.
function publishLiveSchema(xml: string) {
  if (liveTimer) clearTimeout(liveTimer);
  liveTimer = setTimeout(() => {
    if (!popup || !editorReady || !editorSchemaBaseline) return;
    try { announceLiveSchema(schemaFromDrawio(xml, editorSchemaBaseline, true)); }
    catch { /* A diagram that is mid-edit cannot be read yet; the last readable state stays. */ }
  }, 400);
}
function announceLiveSchema(schema: unknown) {
  lastLiveSchema = schema;
  try { emit('editorSchema', { baseline: JSON.parse(savedText.value), schema }); } catch { /* No saved draft to compare with. */ }
}
function closedMessage() {
  return dirty.value ? 'Draw.io closed. Your Draw.io changes are unsaved here: choose Save draft to keep them.' : 'Draw.io closed.';
}
function handleSchemaExport(event: DrawioSchemaExportRequest, editor: Window, origin: string) {
  try {
    if (!current.value) throw new Error('The draft is unavailable.');
    if (!validLayout(event.xml)) throw new Error('The diagram is invalid or too large.');
    const schema = schemaFromDrawio(event.xml, editorSchemaBaseline, true);
    trackSchemaIdentities(schema, editorIdentities);
    const content = event.event === 'schema-export-dbml' ? structuredSchemaToDbml(schema as StructuredSchemaDraft) : JSON.stringify(schema, null, 2);
    if (new TextEncoder().encode(content).length > MAX_LAYOUT_BYTES) throw new Error('The schema is too large to export.');
    editor.postMessage(JSON.stringify({ action: 'schema-export-result', requestId: event.requestId, ok: true, content }), origin);
  } catch (cause: any) {
    editor.postMessage(JSON.stringify({ action: 'schema-export-result', requestId: event.requestId, ok: false, error: cause.message || 'Schema export failed.' }), origin);
  }
}
async function handleEditorMigration(event: DrawioMigrationRequest, editor: Window, origin: string) {
  if (!current.value || migrationBusy.value) {
    editor.postMessage(JSON.stringify({ action: 'schema-migration-result', requestId: event.requestId, ok: false, error: migrationBusy.value ? 'A migration request is in progress. Try again shortly.' : 'The draft is unavailable.' }), origin);
    return;
  }
  migrationBusy.value = true;
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
      // The preview describes the saved draft. Draw.io changes are never saved for the developer, so an unsaved diagram
      // has to be saved from this page first.
      if (!sameStructuredSchema(schema, JSON.parse(savedText.value))) {
        throw new UnsavedDraftError('This diagram has changes that are not saved to the draft. Save in Draw.io, choose Save draft on the page, then preview the migration.');
      }
      if (popup !== editor || !editorReady) return;
      const draft = current.value;
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
    if (event.event === 'schema-migration-preview' && !(cause instanceof UnsavedDraftError) && editorSaveGeneration === saveGeneration && popup === editor && current.value?.id === startingDraftId) rememberRejectedLayout(event.xml);
    reply({ ok: false, error: cause.message || 'Migration request failed.' });
  } finally { migrationBusy.value = false; }
}
function disconnectEditor() {
  if (window.confirm('Disconnect Draw.io? Changes not yet saved in Draw.io will not reach this draft.')) closeEditor();
}
function onEditorMessage(event: MessageEvent) {
  if (!popup) return;
  const origin = editorUrl(import.meta.env.VITE_DRAWIO_ORIGIN || '', window.location.origin).origin;
  const selection = readEditorSelection(event, origin, popup);
  if (selection && editorReady) {
    const exists = selection.kind === 'table' ? props.schema?.tables.some(table => table.id === selection.id) : props.schema?.refs.some(ref => ref.id === selection.id);
    if (exists) { emit('selectEntity', selection); toast.info('Selection shown in the original schema details.'); }
    else toast.info('This selection is not present in the original schema details.');
    return;
  }
  const inspectionRequest = readInspectionRequest(event, origin, popup);
  if (inspectionRequest && editorReady && current.value) {
    const editor = popup;
    const draftId = current.value.id;
    void request(`/api/schema-drafts/${encodeURIComponent(draftId)}/inspect`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target: inspectionRequest.target, consent: true }) })
      .then(result => { if (popup === editor) editor.postMessage(JSON.stringify({ action: 'schema-inspect-result', requestId: inspectionRequest.requestId, ok: true, report: result }), origin); })
      .catch(() => { if (popup === editor) editor.postMessage(JSON.stringify({ action: 'schema-inspect-result', requestId: inspectionRequest.requestId, ok: false, error: 'Inspection failed. Check the target configuration and connection.' }), origin); });
    return;
  }
  const exportRequest = readSchemaExportRequest(event, origin, popup);
  if (exportRequest && editorReady) { handleSchemaExport(exportRequest, popup, origin); return; }
  const migrationRequest = readMigrationRequest(event, origin, popup);
  if (migrationRequest && editorReady) { void handleEditorMigration(migrationRequest, popup, origin); return; }
  const messageData = readEditorEvent(event, origin, popup);
  if (!messageData) return;
  if (messageData.event === 'autosave') { if (editorReady) publishLiveSchema(messageData.xml); return; }
  if (editorReady && (messageData.event === 'save' || messageData.event === 'invalid' || messageData.event === 'oversize')) editorSaveGeneration++;
  if (messageData.event === 'init' && !editorReady) {
    editorReady = true;
    if (handshakeTimer) clearTimeout(handshakeTimer);
    popup.postMessage(JSON.stringify({ action: 'load', xml: layout.value, autosave: 1 }), origin);
    popup.postMessage(JSON.stringify({ action: 'schema-migration-host' }), origin);
    sendRuntimeCounts();
    message.value = 'Draw.io is open. Your edits appear in the schema view under Show new changes. Nothing is saved until you choose Save draft.';
  } else if (messageData.event === 'oversize' && editorReady) {
    const failure = 'The editor message exceeds the size limit and was not retained. Reduce the diagram size in Draw.io and save again; the last valid draft is preserved.';
    error.value = failure;
    popup.postMessage(JSON.stringify({ action: 'schema-draft-save-result', ok: false, error: failure }), origin);
  } else if (messageData.event === 'invalid' && editorReady) {
    rememberRejectedLayout(messageData.xml);
    const failure = 'The diagram XML is invalid or contains a forbidden document type. The last valid draft is preserved; correct the diagram or download it for recovery.';
    error.value = failure;
    popup.postMessage(JSON.stringify({ action: 'schema-draft-save-result', ok: false, error: failure }), origin);
  } else if (messageData.event === 'save' && editorReady) {
    try {
      const schema = schemaFromDrawio(messageData.xml, editorSchemaBaseline, true);
      trackSchemaIdentities(schema, editorIdentities);
      text.value = JSON.stringify(schema, null, 2);
      layout.value = messageData.xml;
      rememberEdits(); error.value = ''; rememberRejectedLayout('');
      // Saving in Draw.io only brings the diagram into this page. The draft changes when the developer chooses Save draft.
      message.value = 'Draw.io changes are in the editor but not saved to the draft. Choose Save draft to keep them.';
      announceLiveSchema(schema);
      if (messageData.exit) { popup.close(); closeEditor(); }
    } catch (cause: any) {
      rememberRejectedLayout(messageData.xml);
      const failure = `${cause.message || 'Could not read the diagram.'} The last valid schema and layout are preserved. Correct the diagram and save again, or download the rejected diagram for recovery.`;
      error.value = failure;
      popup.postMessage(JSON.stringify({ action: 'schema-draft-save-result', ok: false, error: failure }), origin);
    }
  } else if (messageData.event === 'exit' && editorReady) {
    closeEditor();
    message.value = closedMessage();
  }
}
function openEditor(reservedWindow: Window | null = null): boolean {
  if (!current.value || editorActive.value || migrationBusy.value) return false;
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
    if (reservedWindow && !reservedWindow.closed) {
      popup = reservedWindow;
      popup.location.href = url.toString();
    } else if (reservedWindow === null) popup = window.open(url.toString(), '_blank');
    else popup = null;
    if (!popup) throw new Error('The browser blocked Draw.io. Allow popups and retry.');
    editorSchemaBaseline = savedSchema;
    editorIdentities = identities;
    // The diagram generated for the editor is not a user edit; treating it as one made the next
    // open ask "Discard unsaved draft changes?" and, when refused, close its new window.
    const layoutWasClean = layout.value === savedLayout.value;
    layout.value = initializedLayout;
    if (layoutWasClean) savedLayout.value = initializedLayout;
    rememberEdits();
    editorActive.value = true; editorReady = false;
    window.addEventListener('message', onEditorMessage);
    // Closing the window with the browser's own button sends no event; without this the panel
    // would think an editor is still open and refuse (and instantly close) every later open.
    const watched = popup;
    closedWatcher = setInterval(() => {
      if (popup !== watched || !watched.closed) return;
      closeEditor();
      message.value = closedMessage();
    }, 500);
    handshakeTimer = setTimeout(() => {
      if (!editorReady) { closeEditor(); error.value = 'Draw.io did not connect. Check the editor service and browser isolation, then retry. The draft is preserved.'; }
    }, 15000);
    return true;
  } catch (cause: any) {
    reservedWindow?.close();
    closeEditor();
    error.value = cause.message || 'Could not open Draw.io.';
    return false;
  }
}
function rebuildLayout() {
  if (!current.value || editorActive.value || migrationBusy.value || text.value !== savedText.value) return;
  error.value = ''; message.value = '';
  try {
    const rebuilt = rebuildDrawioDiagram(layout.value, current.value.schema, () => window.confirm('Rebuild the diagram from the saved schema? Existing positions and annotations will be lost.'));
    if (rebuilt === layout.value) return;
    if (!validLayout(rebuilt)) throw new Error('The rebuilt diagram is invalid or too large. The existing layout was preserved.');
    layout.value = rebuilt; rememberEdits();
    message.value = 'Diagram rebuilt from the saved schema. Save the draft to keep it.';
  } catch (cause: any) { error.value = cause.message || 'Could not rebuild the diagram. The existing layout was preserved.'; }
}

function canLoadRuntimeDraft(): boolean {
  if (busy.value || editorActive.value || migrationBusy.value) {
    error.value = 'Finish the current draft operation and close the Draw.io session before loading a Runtime schema.';
    return false;
  }
  return true;
}
function acceptRuntimeDraft(draft: SavedSchemaDraft, reservedWindow: Window | null): boolean {
  if (!canLoadRuntimeDraft() || !canReplace()) return false;
  rememberEdits();
  adopt(draft);
  if (!rows.value.some(row => row.id === draft.id)) rows.value.unshift(draft);
  message.value = 'Runtime schema saved as a new draft.';
  if (!reservedWindow) {
    error.value = 'The browser blocked Draw.io. Allow popups, then choose Open saved draft in Draw.io.';
    return false;
  }
  return openEditor(reservedWindow);
}
defineExpose({ acceptRuntimeDraft, canLoadRuntimeDraft, saveDraft, saveAsNewDraft, openEditor });

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
function adopt(draft: Draft, recover = false): boolean {
  let recovered = false;
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
        recovered = true;
      }
    } catch { error.value = 'Could not restore local draft recovery.'; }
  }
  return recovered;
}
function canReplace() {
  if (migrationBusy.value) { error.value = 'Wait for the current migration request to finish.'; return false; }
  if (editorActive.value) { error.value = 'Close the Draw.io session before switching drafts.'; return false; }
  if (!mayReplaceRejectedLayout(rejectedLayout.value, !rejectedRecoveryWarning.value, () => window.confirm('Local recovery is unavailable. Discard the rejected diagram and switch drafts? Download it first if you want to keep it.'))) return false;
  if (!dirty.value) return true;
  if (!window.confirm('This draft has unsaved changes. They are not copied to the draft you switch to or create next, and they will be lost. Cancel and choose Save draft (or Save as new draft) to keep them. Discard them?')) return false;
  return true;
}
async function openDraft(id = selected.value) {
  if (!id || !canReplace()) return;
  busy.value = true; error.value = ''; message.value = '';
  try {
    const draft = await request(`/api/schema-drafts/${encodeURIComponent(id)}`);
    if (current.value && dirty.value) clearRecovery(current.value.id);
    if (!adopt(draft, true)) message.value = 'Draft opened.';
  }
  catch (cause: any) { error.value = cause.message || 'Could not open draft.'; }
  finally { busy.value = false; }
}
async function deleteDraft(id = selected.value) {
  if (!id || busy.value) return;
  const isCurrent = current.value?.id === id;
  if (isCurrent && (editorActive.value || migrationBusy.value)) { error.value = 'Close the Draw.io session before deleting this draft.'; return; }
  const row = rows.value.find(item => item.id === id);
  if (!window.confirm(`Delete draft "${row ? draftLabel(row) : id}" permanently? Its diagram and edits cannot be recovered. The database it was loaded from is not touched.`)) return;
  busy.value = true; error.value = ''; message.value = '';
  try {
    await request(`/api/schema-drafts/${encodeURIComponent(id)}`, { method: 'DELETE' });
    clearRecovery(id);
    if (isCurrent) {
      current.value = null; selected.value = '';
      text.value = ''; savedText.value = ''; layout.value = ''; savedLayout.value = ''; rejectedLayout.value = '';
    } else if (selected.value === id) selected.value = '';
    message.value = 'Draft deleted.';
    await refresh();
  } catch (cause: any) { error.value = cause.message || 'Could not delete draft.'; }
  finally { busy.value = false; }
}
async function deleteAllDrafts() {
  if (!rows.value.length || busy.value) return;
  if (editorActive.value || migrationBusy.value) { error.value = 'Close the Draw.io session before removing drafts.'; return; }
  const count = rows.value.length;
  if (!window.confirm(`Remove all ${count} ${count === 1 ? 'draft' : 'drafts'} permanently? Their diagrams and any unsaved edits cannot be recovered. The databases they were loaded from are not touched.`)) return;
  busy.value = true; error.value = ''; message.value = '';
  const failures: string[] = [];
  try {
    for (const row of [...rows.value]) {
      try { await request(`/api/schema-drafts/${encodeURIComponent(row.id)}`, { method: 'DELETE' }); clearRecovery(row.id); }
      catch (cause: any) { failures.push(cause?.message || 'A draft could not be removed.'); }
    }
    await refresh();
    if (current.value && !rows.value.some(row => row.id === current.value?.id)) {
      current.value = null;
      text.value = ''; savedText.value = ''; layout.value = ''; savedLayout.value = ''; rejectedLayout.value = '';
    }
    if (!rows.value.some(row => row.id === selected.value)) selected.value = '';
    if (failures.length) error.value = `Removed ${count - failures.length} of ${count} drafts. ${failures[0]}`;
    else message.value = count === 1 ? 'Draft removed.' : `All ${count} drafts removed.`;
  } finally { busy.value = false; }
}
const restrictedTables = computed(() => (current.value?.provenance?.coverage.warnings ?? [])
  .map(warning => /for (\S+) before editing/.exec(warning)?.[1] ?? warning).join(', '));
function draftLabel(row: DraftRow & { provenance?: { database?: string; environment?: string; extracted_at?: string } }) {
  const when = row.provenance?.extracted_at ? new Date(row.provenance.extracted_at).toLocaleString() : '';
  return row.provenance
    ? `runtime · ${row.provenance.database} · ${row.provenance.environment} · ${when} · revision ${row.revision}`
    : `${row.source_kind} · ${row.analysis_id || row.id} · revision ${row.revision}`;
}
// A draft can only start from a real source: a saved analysis or an import, not from the draft being shown.
const sourceProblem = computed(() => {
  if (!props.schema) return 'Load a schema first.';
  if (props.schemaIsDraft) return 'No original source is loaded. Choose a saved analysis or import a schema to start a draft from it. To copy the open draft with your edits, use Save as new draft.';
  if (props.sourceType === 'saved' && !props.analysisId) return 'Choose a saved analysis first.';
  return '';
});
async function createDraft() {
  if (sourceProblem.value) { error.value = sourceProblem.value; return; }
  if (!props.schema || !canReplace()) return;
  busy.value = true; error.value = ''; message.value = '';
  try {
    const saved = props.sourceType === 'saved';
    const path = saved ? `/api/schema-drafts/analysis/${encodeURIComponent(props.analysisId)}` : '/api/schema-drafts/import';
    const body = saved ? undefined : JSON.stringify({ schema: importDraftSchema(props.schema) });
    const draft = await request(path, { method: 'POST', ...(body ? { headers: { 'Content-Type': 'application/json' }, body } : {}) });
    if (current.value && dirty.value) clearRecovery(current.value.id);
    adopt(draft); message.value = 'New draft created from the original source. It does not include edits made in other drafts; use Save as new draft to copy edits.'; await refresh();
  } catch (cause: any) { error.value = cause.message || 'Could not create draft.'; }
  finally { busy.value = false; }
}
async function saveDraft() {
  if (!current.value || migrationBusy.value) return;
  busy.value = true; error.value = ''; message.value = '';
  try {
    if (layout.value && !validLayout(layout.value)) throw new Error('The diagram layout is invalid or too large.');
    let schema: unknown;
    try { schema = JSON.parse(text.value); } catch { throw new Error('The draft is not valid JSON. Your edits are still here.'); }
    const draft = await request(`/api/schema-drafts/${encodeURIComponent(current.value.id)}`, { method: 'PUT',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ schema, layout: layout.value, revision: current.value.revision }) });
    clearRecovery(draft.id); adopt(draft); message.value = 'Draft saved.'; await refresh();
    if (editorActive.value && lastLiveSchema !== undefined) announceLiveSchema(lastLiveSchema);
  } catch (cause: any) { error.value = `${cause.message || 'Could not save draft.'} Your edits remain in the editor; copy them before reopening a newer revision.`; }
  finally { busy.value = false; }
}
// Copies what is in the editor now, unsaved edits included, into a new draft. The open draft stays as it was last saved.
async function saveAsNewDraft() {
  if (!current.value || busy.value || migrationBusy.value) return;
  busy.value = true; error.value = ''; message.value = '';
  try {
    if (layout.value && !validLayout(layout.value)) throw new Error('The diagram layout is invalid or too large.');
    let schema: unknown;
    try { schema = JSON.parse(text.value); } catch { throw new Error('The draft is not valid JSON. Your edits are still here.'); }
    const copy = await request(`/api/schema-drafts/${encodeURIComponent(current.value.id)}/copy`, { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ schema, layout: layout.value }) });
    if (dirty.value) clearRecovery(current.value.id);
    const wasOpenInEditor = editorActive.value;
    adopt(copy);
    message.value = wasOpenInEditor
      ? 'Saved as a new draft. Draw.io is still showing the previous draft: close it and open this draft in Draw.io to keep editing.'
      : 'Saved as a new draft. The previous draft is unchanged.';
    await refresh();
  } catch (cause: any) { error.value = `${cause.message || 'Could not save a new draft.'} Your edits remain in the editor.`; }
  finally { busy.value = false; }
}
// The panel holds Save draft and Remove all drafts, so it opens by itself once there is a draft to work with.
const panelOpen = ref(false);
watch(() => rows.value.length > 0 || !!current.value, (has) => { if (has) panelOpen.value = true; });
watch(savedText, (value) => {
  let parsed: unknown = null;
  try { parsed = value ? JSON.parse(value) : null; } catch { parsed = null; }
  emit('draftSchema', parsed);
}, { immediate: true });
watch([() => !!current.value, dirty, editorActive, busy, () => current.value?.revision ?? 0],
  ([hasDraft, isDirty, active, working, revision]) => emit('status', { hasDraft, dirty: isDirty, editorActive: active, busy: working, revision }),
  { immediate: true });
onMounted(() => { window.addEventListener('beforeunload', warnBeforeUnload); void refresh(); });
onUnmounted(() => { closeEditor(); window.removeEventListener('beforeunload', warnBeforeUnload); });
</script>

<template>
  <details class="sc-drafts" :open="panelOpen" @toggle="panelOpen = ($event.target as HTMLDetailsElement).open">
    <summary>
      <PencilRuler aria-hidden="true" /><b>Drafts and migrations</b>
      <span v-if="rows.length" class="sc-tag">{{ rows.length }} {{ rows.length === 1 ? 'draft' : 'drafts' }}</span>
      <span v-if="current" class="sc-tag" data-tone="run">revision {{ current.revision }}</span>
      <span v-if="dirty" class="sc-tag" data-tone="warn">unsaved changes</span>
      <span class="sc-hint sc-m0 sc-summary-hint">Edit in Draw.io and preview a framework migration</span>
      <ChevronDown class="sc-chev" aria-hidden="true" />
    </summary>
    <div class="sc-drafts-body">
      <p class="sc-hint sc-m0">Drafts need a global administrator. The explorer below shows the original source, or the saved draft when no source is loaded; unsaved edits appear there only through Show new changes. Schema coverage is unverified; imported indexes and complex constraints may be incomplete.</p>

      <div v-if="current?.provenance" class="sc-banner" :data-tone="current.provenance.coverage.complete ? 'run' : 'warn'">
        <div>
          <p class="sc-m0">Runtime baseline: {{ current.provenance.engine }} {{ current.provenance.engine_version }} · {{ current.provenance.database }} · {{ current.provenance.project_id }} / {{ current.provenance.environment }}</p>
          <p class="sc-hint sc-m0">Container {{ current.provenance.container_id.slice(0, 12) }} · extracted {{ new Date(current.provenance.extracted_at).toLocaleString() }}</p>
          <details v-if="current.provenance.coverage.warnings.length" class="sc-m0">
            <summary>{{ current.provenance.coverage.warnings.length }} tables keep catalog notes, so migrations are restricted for them</summary>
            <p class="sc-hint">Features this editor cannot model (such as engine options, collations or checks) stay as read-only notes on the table. Review them before editing: {{ restrictedTables }}.</p>
          </details>
        </div>
      </div>

      <ol class="sc-flow">
        <li class="sc-step" :data-state="current ? 'done' : 'active'">
          <p class="sc-step-head"><span class="sc-step-n"><Check v-if="current" aria-hidden="true" /><template v-else>1</template></span>Choose a draft</p>
          <div class="sc-row">
            <label for="schema-draft-select" class="sr-only">Saved draft</label>
            <select id="schema-draft-select" v-model="selected" class="sc-input sc-grow" :disabled="busy">
              <option value="">Choose a draft</option>
              <option v-for="row in rows" :key="row.id" :value="row.id">{{ draftLabel(row) }}</option>
            </select>
            <button type="button" class="sc-btn sc-btn-sm" :disabled="!selected || busy" @click="openDraft()">Open</button>
            <button type="button" class="sc-btn sc-btn-sm" :disabled="!selected || busy" @click="deleteDraft()">Delete</button>
          </div>
          <p class="sc-hint sc-m0">{{ sourceProblem || 'New from source starts from the original schema, not from the draft you have open. To keep your edits as a separate draft, use Save as new draft in step 2.' }}</p>
          <div class="sc-row">
            <button type="button" class="sc-btn sc-btn-sm" :disabled="!!sourceProblem || busy" :title="sourceProblem || 'Builds a draft from the original schema shown below. It does not include edits made in any draft.'" @click="createDraft"><Plus aria-hidden="true" />New from source</button>
            <button type="button" class="sc-btn sc-btn-sm sc-btn-icon" :disabled="busy" title="Refresh drafts" aria-label="Refresh drafts" @click="refresh"><RefreshCw aria-hidden="true" /></button>
            <button type="button" class="sc-btn sc-btn-sm" data-variant="danger" :disabled="!rows.length || busy || editorActive" title="Permanently delete every saved draft" @click="deleteAllDrafts"><Trash2 aria-hidden="true" />Remove all drafts</button>
          </div>
        </li>

        <li class="sc-step" :data-state="current ? 'active' : 'idle'">
          <p class="sc-step-head"><span class="sc-step-n">2</span>Edit the schema</p>
          <p class="sc-hint sc-m0">Edit in the structured editor below, or open Draw.io in a new tab to edit visually. Draw.io edits show up in the schema view as you make them; nothing is saved to a draft until you choose Save draft.</p>
          <div v-if="current" class="sc-row">
            <button type="button" class="sc-btn sc-btn-sm" data-variant="primary" :disabled="busy || migrationBusy || !dirty" @click="saveDraft"><Save aria-hidden="true" />Save draft</button>
            <button type="button" class="sc-btn sc-btn-sm" :disabled="busy || migrationBusy" title="Copies the schema and diagram as they are now, unsaved edits included, into a new draft. The open draft is not changed." @click="saveAsNewDraft"><Plus aria-hidden="true" />Save as new draft</button>
            <button type="button" class="sc-btn sc-btn-sm" :disabled="busy || editorActive || migrationBusy" @click="openEditor()"><ExternalLink aria-hidden="true" />Open in Draw.io</button>
            <button v-if="editorActive" type="button" class="sc-btn sc-btn-sm" :disabled="!editorReady || !selection" @click="revealSelection"><Eye aria-hidden="true" />Show selection in Draw.io</button>
            <button v-if="editorActive" type="button" class="sc-btn sc-btn-sm" @click="disconnectEditor"><Unplug aria-hidden="true" />Disconnect editor</button>
          </div>
          <p v-else class="sc-hint sc-m0">Open or create a draft first.</p>
        </li>

        <li class="sc-step" :data-state="preview ? 'active' : 'idle'">
          <p class="sc-step-head"><span class="sc-step-n">3</span>Preview a migration</p>
          <template v-if="current">
            <div class="sc-row">
              <label class="sr-only" for="sc-migration-framework">Framework</label>
              <select id="sc-migration-framework" v-model="framework" class="sc-input" :disabled="busy || editorActive || migrationBusy"><option value="laravel">Laravel</option><option value="symfony">Symfony / Doctrine</option></select>
              <label class="sr-only" for="sc-migration-database">Database</label>
              <select id="sc-migration-database" v-model="database" class="sc-input" :disabled="busy || editorActive || migrationBusy"><option value="mysql">MySQL</option><option value="postgresql">PostgreSQL</option></select>
            </div>
            <div class="sc-row">
              <label class="sr-only" for="sc-migration-timestamp">Migration timestamp (YYYY_MM_DD_HHMMSS)</label>
              <input id="sc-migration-timestamp" v-model="timestamp" class="sc-input sc-grow" placeholder="2026_11_02_120000" title="Migration timestamp (YYYY_MM_DD_HHMMSS)" :disabled="busy || editorActive || migrationBusy" />
              <button type="button" class="sc-btn sc-btn-sm" :disabled="dirty || !timestamp || busy || editorActive || migrationBusy" @click="previewMigration"><GitCompare aria-hidden="true" />Preview</button>
            </div>
            <p v-if="dirty" class="sc-hint sc-m0" data-tone="warn">Save the draft before generating a preview.</p>
          </template>
          <p v-else class="sc-hint sc-m0">Available once a draft is open.</p>
        </li>
      </ol>

      <div v-if="current" class="sc-draft-edit">
        <StructuredSchemaEditor v-model="text" :disabled="busy || editorActive || migrationBusy" @update:model-value="rememberEdits" />
        <details class="sc-json">
          <summary>Edit schema JSON</summary>
          <label for="schema-draft-json" class="sr-only">Schema JSON</label>
          <textarea id="schema-draft-json" v-model="text" spellcheck="false" :disabled="busy || editorActive || migrationBusy" @input="rememberEdits"></textarea>
        </details>

        <button v-if="rejectedLayout" type="button" class="sc-btn sc-btn-sm" data-variant="warn" @click="downloadRejectedLayout"><Download aria-hidden="true" />Download rejected diagram for recovery</button>
        <div v-if="layout" class="sc-banner" data-tone="warn">
          <AlertTriangle aria-hidden="true" />
          <div>
            <p class="sc-m0">The existing Draw.io layout isn't synchronized with the schema automatically and may be outdated. It may omit new entities or keep removed ones.</p>
            <p class="sc-m0">Rebuilding uses the saved schema and replaces existing positions and annotations. Save schema edits first.</p>
            <button type="button" class="sc-btn sc-btn-sm sc-mt-sm" data-variant="warn" :disabled="busy || editorActive || migrationBusy || text !== savedText" @click="rebuildLayout">Rebuild layout from saved schema</button>
          </div>
        </div>
        <p class="sc-hint">In Draw.io, use Extras to edit column properties, table keys and indexes, and relationships, or to preview a migration. Edit table and column labels directly. Save in Draw.io to bring the diagram into this page, then choose Save draft. The property editors use JSON and stable IDs. Leaving this view disconnects the editor.</p>

        <section v-if="preview" ref="previewSection" class="sc-migration" aria-label="Migration preview" tabindex="-1">
          <div class="sc-banner" data-tone="warn"><AlertTriangle aria-hidden="true" /><span>The baseline is unverified. Compare it with the target database and review the model-generated code before use. Validation accepts only a narrow set of supported migration operations.</span></div>
          <p v-if="preview.generation_blocked" role="alert" class="sc-banner" data-tone="bad"><AlertTriangle aria-hidden="true" /><span>Generation blocked: {{ preview.generation_error || 'No migration can be generated.' }}</span></p>
          <ul v-if="preview.warnings.length" class="sc-warn-list"><li v-for="warning in preview.warnings" :key="warning">{{ warning }}</li></ul>
          <h3 class="sc-sec-title sc-sec-title-sm"><GitCompare aria-hidden="true" />Operations (before and after)</h3>
          <p v-if="!preview.operations.length" class="sc-hint">No changes detected.</p>
          <div v-else class="sc-ops">
            <pre v-for="(operation, index) in preview.operations" :key="index" class="sc-op-card">{{ JSON.stringify(operation, null, 2) }}</pre>
          </div>
          <div v-for="file in preview.files" :key="file.name" class="sc-file">
            <p class="sc-file-head"><FileCode aria-hidden="true" />{{ file.name }}</p>
            <pre>{{ file.content }}</pre>
          </div>
          <label v-if="preview.requires_confirmation" class="sc-confirm"><input v-model="confirmation" type="checkbox" /> I reviewed the destructive or ambiguous changes in this preview.</label>
          <button type="button" class="sc-btn" data-variant="primary" :disabled="preview.generation_blocked || (preview.requires_confirmation && !confirmation)" @click="downloadMigration"><Download aria-hidden="true" />Download previewed migration</button>
        </section>
      </div>

      <p v-if="draftRecoveryWarning" role="alert" class="sc-hint" data-tone="warn">{{ draftRecoveryWarning }}</p>
      <p v-if="rejectedRecoveryWarning" role="alert" class="sc-hint" data-tone="warn">{{ rejectedRecoveryWarning }}</p>
    </div>
  </details>
</template>
