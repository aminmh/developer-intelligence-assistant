<script setup lang="ts">
/**
 * API contracts of the selected project: where its Swagger / OpenAPI documents come from,
 * their versions, and which contracts the project provides or consumes.
 * Spec: .archcore/frontend-assist/api-contract-sources-and-versions.spec.md
 */
import { computed, ref, watch } from 'vue';
import { CircleAlert, CircleCheck, ClipboardCheck, Code2, FileJson, Link2, LoaderCircle, Plus, RefreshCw, Trash2, UploadCloud, Unplug, ListTree } from 'lucide-vue-next';
import { makeRequest } from '../../ui/makeRequest';
import ContractCheckPanel from './ContractCheckPanel.vue';
import ContractCodePanel from './ContractCodePanel.vue';
import {
  MAX_CONTRACT_BYTES, changeSummary, kindLabel, sourceDetail, statusTone, versionLabel,
  type ContractAttachment, type ContractSource, type ContractVersion, type SourceKind, type SourceSummary,
} from '../../utils/apiContracts';

const props = defineProps<{ projectId: string }>();
const emit = defineEmits<{ changed: [hasContracts: boolean] }>();

const sources = ref<ContractSource[]>([]);
const attachments = ref<ContractAttachment[]>([]);
const attachable = ref<SourceSummary[]>([]);
const loading = ref(false);
const busy = ref('');
const message = ref('');
const error = ref('');

// New source form
const kind = ref<SourceKind>('upload');
const name = ref('');
const path = ref('');
const url = ref('');
const refreshMinutes = ref(0);
// New attachment form
const attachSourceId = ref('');
const attachRole = ref<'consumer' | 'provider'>('consumer');
// Expanded source: its versions and the operations of one version
const openSourceId = ref('');
const versions = ref<ContractVersion[]>([]);
const openVersion = ref<ContractVersion | null>(null);
const uploadTarget = ref('');
const checkAttachmentId = ref('');
const codeAttachmentId = ref('');
const showSourceCode = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);

const project = computed(() => props.projectId.trim());
const base = computed(() => `/api/projects/${encodeURIComponent(project.value)}/contracts`);
const attachChoices = computed(() => attachable.value.filter((source) =>
  !attachments.value.some((row) => row.source_id === source.source_id && row.role === attachRole.value)));

const request = async (target: string, init?: RequestInit) => {
  const response = await makeRequest(target, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The contract request failed.');
  return data;
};
const send = (method: string, body?: unknown): RequestInit => ({
  method, ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }) });

let loadSequence = 0;
const load = async () => {
  const sequence = ++loadSequence;
  if (!project.value) { sources.value = []; attachments.value = []; attachable.value = []; return; }
  loading.value = true;
  try {
    const [overview, visible] = await Promise.all([request(base.value), request('/api/contracts/sources')]);
    if (sequence !== loadSequence) return;
    sources.value = overview.sources || [];
    attachments.value = overview.attachments || [];
    attachable.value = visible.sources || [];
    emit('changed', sources.value.length + attachments.value.length > 0);
  } catch (cause: any) {
    if (sequence === loadSequence) error.value = cause?.message || 'API contracts are unavailable.';
  } finally {
    if (sequence === loadSequence) loading.value = false;
  }
};
watch(project, () => {
  message.value = ''; error.value = ''; openSourceId.value = ''; versions.value = []; openVersion.value = null;
  checkAttachmentId.value = ''; codeAttachmentId.value = ''; showSourceCode.value = false;
  void load();
}, { immediate: true });

/** Run one change, show its result, and reload. */
const act = async (key: string, work: () => Promise<string>) => {
  busy.value = key; message.value = ''; error.value = '';
  try { message.value = await work(); }
  catch (cause: any) { error.value = cause?.message || 'The contract request failed.'; }
  finally { busy.value = ''; await load(); if (openSourceId.value) await loadVersions(openSourceId.value); }
};
const outcomeText = (result: any, label: string) => {
  if (result.outcome === 'created') return `${label}: new version with ${result.version.operation_count} operations.`;
  if (result.outcome === 'unchanged') return `${label}: the document has not changed, so no new version was created.`;
  throw new Error(`${label}: ${(result.version?.errors || ['the document could not be read.']).slice(0, 3).join(' ')}`);
};

const createSource = () => act('create', async () => {
  const body: Record<string, unknown> = { kind: kind.value, name: name.value.trim() };
  if (kind.value === 'repo_path') body.path = path.value.trim();
  if (kind.value === 'url') { body.url = url.value.trim(); if (refreshMinutes.value) body.refresh_minutes = refreshMinutes.value; }
  const created = await request(`${base.value}/sources`, send('POST', body));
  const label = name.value.trim();
  name.value = ''; path.value = ''; url.value = '';
  if (kind.value === 'upload') return `${label} created. Upload its document to make the first version.`;
  // Read the first version straight away, so the contract is usable without a second step.
  return outcomeText(await request(`${base.value}/sources/${created.source.source_id}/refresh`, send('POST')), label);
});
const refresh = (source: ContractSource) => act(`refresh:${source.source_id}`, async () =>
  outcomeText(await request(`${base.value}/sources/${source.source_id}/refresh`, send('POST')), source.name));
const removeSource = (source: ContractSource) => {
  if (!window.confirm(`Remove the contract "${source.name}" and all of its versions? This cannot be undone.`)) return;
  if (openSourceId.value === source.source_id) openSourceId.value = '';
  void act(`remove:${source.source_id}`, async () => {
    await request(`${base.value}/sources/${source.source_id}`, send('DELETE'));
    return `${source.name} removed.`;
  });
};
const chooseFile = (source: ContractSource) => { uploadTarget.value = source.source_id; fileInput.value?.click(); };
const onFileChosen = (event: Event) => {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  const source = sources.value.find((row) => row.source_id === uploadTarget.value);
  input.value = '';
  if (!file || !source) return;
  if (file.size > MAX_CONTRACT_BYTES) { error.value = 'The document exceeds the 10 MB limit.'; return; }
  void act(`upload:${source.source_id}`, async () => outcomeText(await request(
    `${base.value}/sources/${source.source_id}/document`,
    { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: await file.text() }), source.name));
};

const loadVersions = async (sourceId: string) => {
  try {
    versions.value = (await request(`/api/contracts/sources/${sourceId}/versions`)).versions || [];
    const current = versions.value.find((row) => row.state === 'valid');
    const keep = openVersion.value && versions.value.some((row) => row.version_id === openVersion.value!.version_id);
    if (!keep) openVersion.value = null;
    if (!keep && current) await showVersion(sourceId, current.version_id);
  } catch (cause: any) { error.value = cause?.message || 'Contract versions are unavailable.'; }
};
const showVersion = async (sourceId: string, versionId: string) => {
  try { openVersion.value = (await request(`/api/contracts/sources/${sourceId}/versions/${versionId}`)).version; }
  catch (cause: any) { error.value = cause?.message || 'The contract version is unavailable.'; }
};
const toggleSource = async (sourceId: string) => {
  if (openSourceId.value === sourceId) { openSourceId.value = ''; return; }
  openSourceId.value = sourceId; versions.value = []; openVersion.value = null;
  await loadVersions(sourceId);
};

const attach = () => act('attach', async () => {
  const source = attachable.value.find((row) => row.source_id === attachSourceId.value);
  await request(`${base.value}/attachments`, send('POST', { source_id: attachSourceId.value, role: attachRole.value, mode: 'follow_latest' }));
  attachSourceId.value = '';
  return `${source?.name || 'Contract'} attached as ${attachRole.value}.`;
});
const detach = (row: ContractAttachment) => act(`detach:${row.attachment_id}`, async () => {
  await request(`${base.value}/attachments/${row.attachment_id}`, send('DELETE'));
  return `${row.source.name} detached.`;
});
// Pinning needs the source's versions; they load when the user opens the picker.
const pinChoices = ref<Record<string, ContractVersion[]>>({});
const loadPinChoices = async (row: ContractAttachment) => {
  if (pinChoices.value[row.source_id]) return;
  try {
    const data = await request(`/api/contracts/sources/${row.source_id}/versions`);
    pinChoices.value = { ...pinChoices.value, [row.source_id]: (data.versions || []).filter((v: ContractVersion) => v.state === 'valid') };
  } catch { /* the picker then offers only the current choice */ }
};
const setMode = (row: ContractAttachment, value: string) => act(`mode:${row.attachment_id}`, async () => {
  const body = value === 'follow_latest' ? { mode: 'follow_latest' } : { mode: 'pinned', pinned_version_id: value };
  await request(`${base.value}/attachments/${row.attachment_id}`, send('PUT', body));
  return value === 'follow_latest' ? `${row.source.name} now follows the latest version.` : `${row.source.name} pinned to one version.`;
});
</script>

<template>
  <section class="kb-rules" aria-labelledby="api-contracts-heading">
    <p v-if="!project" class="kb-rev-note" data-tone="warn"><CircleAlert aria-hidden="true" />Choose a project in the header to manage its API contracts.</p>
    <template v-else>
      <p v-if="message" role="status" class="kb-rev-note" data-tone="ok"><CircleCheck aria-hidden="true" />{{ message }}</p>
      <p v-if="error" role="alert" class="kb-rev-note" data-tone="bad"><CircleAlert aria-hidden="true" />{{ error }}</p>
      <input ref="fileInput" type="file" accept=".json,.yaml,.yml,application/json,text/yaml" class="sr-only" tabindex="-1" aria-hidden="true" @change="onFileChosen" />

      <div class="kb-two">
        <div class="kb-panel">
          <h2 id="api-contracts-heading" class="kb-panel-title"><FileJson aria-hidden="true" />Add an API contract</h2>
          <p class="kb-lead">A Swagger 2.0 or OpenAPI 3 document that this project owns. Each change becomes a new version; other projects can attach to it.</p>
          <div class="kb-row3">
            <label class="kb-field">Name<input v-model="name" maxlength="120" autocomplete="off" class="kb-input" placeholder="Orders API" /></label>
            <label class="kb-field">Source
              <select v-model="kind" class="kb-input">
                <option value="upload">Upload a file</option>
                <option value="repo_path">File in this project's code</option>
                <option value="url">Public URL</option>
              </select>
            </label>
          </div>
          <label v-if="kind === 'repo_path'" class="kb-field">Path in the project<input v-model="path" autocomplete="off" class="kb-input" placeholder="docs/openapi.yaml" /></label>
          <div v-if="kind === 'url'" class="kb-row3">
            <label class="kb-field">HTTPS address<input v-model="url" type="url" autocomplete="off" class="kb-input" placeholder="https://api.example.com/openapi.json" /></label>
            <label class="kb-field">Check for changes
              <select v-model.number="refreshMinutes" class="kb-input">
                <option :value="0">Only when I ask</option>
                <option :value="60">Every hour</option>
                <option :value="360">Every 6 hours</option>
                <option :value="1440">Every day</option>
              </select>
            </label>
          </div>
          <p class="kb-note">
            <template v-if="kind === 'upload'">Upload the document after creating the contract. It must be one self-contained file of at most 10 MB.</template>
            <template v-else-if="kind === 'repo_path'">Read from every new Git or ZIP revision of this project. Working-tree syncs are skipped. The file may reference other files in the same revision.</template>
            <template v-else>Must be a public HTTPS address without a password. Internal and private network addresses are refused.</template>
          </p>
          <div class="kb-btns">
            <button type="button" class="kb-btn" data-variant="primary" :disabled="!!busy || !name.trim() || (kind === 'repo_path' && !path.trim()) || (kind === 'url' && !url.trim())" @click="createSource">
              <LoaderCircle v-if="busy === 'create'" class="kb-spin" aria-hidden="true" /><Plus v-else aria-hidden="true" />Add contract
            </button>
          </div>
        </div>

        <div class="kb-panel">
          <h2 class="kb-panel-title"><Link2 aria-hidden="true" />Contracts this project uses</h2>
          <p class="kb-lead">Attach a contract as <b>provider</b> if this project implements the API, or as <b>consumer</b> if it calls it.</p>
          <div class="kb-row3">
            <label class="kb-field">Contract
              <select v-model="attachSourceId" class="kb-input">
                <option value="" disabled>{{ attachChoices.length ? 'Choose a contract' : 'No contract available' }}</option>
                <option v-for="source in attachChoices" :key="source.source_id" :value="source.source_id">{{ source.name }} ({{ source.project_id }})</option>
              </select>
            </label>
            <label class="kb-field">This project is the
              <select v-model="attachRole" class="kb-input"><option value="consumer">Consumer</option><option value="provider">Provider</option></select>
            </label>
          </div>
          <div class="kb-btns">
            <button type="button" class="kb-btn" :disabled="!!busy || !attachSourceId" @click="attach"><LoaderCircle v-if="busy === 'attach'" class="kb-spin" aria-hidden="true" /><Link2 v-else aria-hidden="true" />Attach</button>
          </div>
          <ul v-if="attachments.length" class="ac-list" aria-label="Attached contracts">
            <li v-for="row in attachments" :key="row.attachment_id" class="kb-status">
              <div class="min-w-0 ac-grow">
                <p class="kb-status-name">{{ row.source.name }}</p>
                <p class="kb-project-meta">{{ row.role === 'provider' ? 'Provider' : 'Consumer' }} · owned by {{ row.source.project_id }} · {{ row.resolved_version ? versionLabel(row.resolved_version) : 'no valid version yet' }}</p>
              </div>
              <label class="ac-mode"><span class="sr-only">Version for {{ row.source.name }}</span>
                <select class="kb-input" :value="row.mode === 'pinned' ? row.pinned_version_id : 'follow_latest'" :disabled="!!busy"
                  @focus="loadPinChoices(row)" @change="setMode(row, ($event.target as HTMLSelectElement).value)">
                  <option value="follow_latest">Follow latest</option>
                  <option v-if="row.mode === 'pinned' && !pinChoices[row.source_id]" :value="row.pinned_version_id">Pinned version</option>
                  <option v-for="version in pinChoices[row.source_id] || []" :key="version.version_id" :value="version.version_id">Pin {{ versionLabel(version) }}</option>
                </select>
              </label>
              <button type="button" class="kb-btn" :aria-expanded="checkAttachmentId === row.attachment_id" @click="checkAttachmentId = checkAttachmentId === row.attachment_id ? '' : row.attachment_id"><ClipboardCheck aria-hidden="true" />{{ checkAttachmentId === row.attachment_id ? 'Hide check' : 'Check' }}</button>
              <button v-if="row.resolved_version" type="button" class="kb-btn" :aria-expanded="codeAttachmentId === row.attachment_id" @click="codeAttachmentId = codeAttachmentId === row.attachment_id ? '' : row.attachment_id"><Code2 aria-hidden="true" />{{ codeAttachmentId === row.attachment_id ? 'Hide code' : 'Generate code' }}</button>
              <button type="button" class="kb-btn" data-variant="danger" :disabled="!!busy" @click="detach(row)"><Unplug aria-hidden="true" />Detach</button>
              <ContractCheckPanel v-if="checkAttachmentId === row.attachment_id" :project-id="project" :attachment-id="row.attachment_id" :role="row.role" />
              <ContractCodePanel v-if="codeAttachmentId === row.attachment_id && row.resolved_version" :source-id="row.source_id" :version-id="row.resolved_version.version_id" />
            </li>
          </ul>
          <p v-else class="kb-note">Nothing attached yet.</p>
        </div>
      </div>

      <div class="kb-panel">
        <h2 class="kb-panel-title"><ListTree aria-hidden="true" />Contracts owned by {{ project }}</h2>
        <p v-if="loading && !sources.length" class="kb-note"><LoaderCircle class="kb-spin" aria-hidden="true" /> Loading contracts…</p>
        <p v-else-if="!sources.length" class="kb-note">No contracts yet. Add one above.</p>
        <ul v-else class="ac-list">
          <li v-for="source in sources" :key="source.source_id" class="ac-source">
            <div class="kb-status">
              <span class="kb-project-icon"><FileJson aria-hidden="true" /></span>
              <div class="min-w-0 ac-grow">
                <p class="kb-status-name">{{ source.name }}</p>
                <p class="kb-project-meta">{{ kindLabel(source.kind) }}<template v-if="sourceDetail(source)"> · {{ sourceDetail(source) }}</template></p>
                <p class="kb-project-meta">{{ source.current_version ? versionLabel(source.current_version) : 'No valid version yet' }}<template v-if="source.attachments.length"> · used by {{ source.attachments.map((row) => `${row.project_id} (${row.role})`).join(', ') }}</template></p>
              </div>
              <span class="kb-tag" :data-tone="statusTone(source.status)">{{ source.status === 'ok' ? 'Up to date' : source.status === 'empty' ? 'No document' : source.status === 'missing' ? 'File missing' : 'Last check failed' }}</span>
              <button v-if="source.kind === 'upload'" type="button" class="kb-btn" :disabled="!!busy" @click="chooseFile(source)"><LoaderCircle v-if="busy === `upload:${source.source_id}`" class="kb-spin" aria-hidden="true" /><UploadCloud v-else aria-hidden="true" />Upload document</button>
              <button v-else type="button" class="kb-btn" :disabled="!!busy" @click="refresh(source)"><LoaderCircle v-if="busy === `refresh:${source.source_id}`" class="kb-spin" aria-hidden="true" /><RefreshCw v-else aria-hidden="true" />Check now</button>
              <button type="button" class="kb-btn" :aria-expanded="openSourceId === source.source_id" @click="toggleSource(source.source_id)"><ListTree aria-hidden="true" />{{ openSourceId === source.source_id ? 'Hide versions' : 'Versions' }}</button>
              <button type="button" class="kb-btn" data-variant="danger" :disabled="!!busy" :aria-label="`Remove ${source.name}`" @click="removeSource(source)"><Trash2 aria-hidden="true" /></button>
            </div>
            <p v-if="source.status !== 'ok' && source.last_errors.length" class="kb-rev-note" data-tone="warn"><CircleAlert aria-hidden="true" />{{ source.last_errors.slice(0, 3).join(' ') }}<template v-if="source.current_version"> The last valid version stays in use.</template></p>

            <div v-if="openSourceId === source.source_id" class="ac-detail">
              <ol class="ac-versions" aria-label="Versions, newest first">
                <li v-for="version in versions" :key="version.version_id">
                  <button v-if="version.state === 'valid'" type="button" class="ac-version" :aria-current="openVersion?.version_id === version.version_id ? 'true' : undefined" @click="showVersion(source.source_id, version.version_id)">
                    <b>{{ versionLabel(version) }}</b><span>{{ changeSummary(version) }}</span>
                  </button>
                  <div v-else class="ac-version" data-state="failed">
                    <b>{{ version.state === 'missing' ? 'File missing' : 'Rejected' }} · {{ new Date(version.created_at).toLocaleString() }}</b><span>{{ version.errors.slice(0, 2).join(' ') }}</span>
                  </div>
                </li>
                <li v-if="!versions.length" class="kb-note">No versions yet.</li>
              </ol>
              <div v-if="openVersion" class="ac-operations">
                <p class="kb-project-meta">{{ openVersion.operations?.length || 0 }} operations · {{ openVersion.schemas?.length || 0 }} schemas<template v-if="openVersion.base_paths?.length"> · base {{ openVersion.base_paths.join(', ') }}</template></p>
                <table v-if="openVersion.operations?.length">
                  <thead><tr><th scope="col">Method</th><th scope="col">Path</th><th scope="col">Summary</th></tr></thead>
                  <tbody>
                    <tr v-for="operation in openVersion.operations" :key="operation.operation" :data-deprecated="operation.deprecated || undefined">
                      <td><span class="ac-method" :data-method="operation.method">{{ operation.method }}</span></td>
                      <td class="ac-path">{{ operation.path }}</td>
                      <td>{{ operation.summary }}<template v-if="operation.deprecated"> (deprecated)</template></td>
                    </tr>
                  </tbody>
                </table>
                <div class="kb-btns">
                  <button type="button" class="kb-btn" :aria-expanded="showSourceCode" @click="showSourceCode = !showSourceCode"><Code2 aria-hidden="true" />{{ showSourceCode ? 'Hide code' : 'Generate code from this version' }}</button>
                </div>
              </div>
              <ContractCodePanel v-if="showSourceCode && openVersion && openVersion.state === 'valid'" :source-id="source.source_id" :version-id="openVersion.version_id" />
            </div>
          </li>
        </ul>
      </div>
    </template>
  </section>
</template>

<style scoped>
.ac-list { display: flex; flex-direction: column; gap: 0.625rem; margin: 0.875rem 0 0; padding: 0; list-style: none; }
.ac-grow { flex: 1 1 12rem; }
.ac-mode { flex: 0 1 13rem; min-width: 9rem; }
.ac-source { display: flex; flex-direction: column; gap: 0.5rem; }
.ac-detail { display: grid; grid-template-columns: minmax(0, 18rem) minmax(0, 1fr); gap: 0.875rem; padding: 0.25rem 0 0.5rem; }
@media (max-width: 60rem) { .ac-detail { grid-template-columns: minmax(0, 1fr); } }
.ac-versions { display: flex; flex-direction: column; gap: 0.375rem; max-height: 26rem; margin: 0; padding: 0; overflow-y: auto; list-style: none; }
.ac-version { display: flex; flex-direction: column; gap: 0.125rem; width: 100%; padding: 0.5rem 0.625rem; border: 1px solid var(--shell-line); border-radius: 0.625rem; background: transparent; color: var(--shell-text); font-size: 0.8125rem; text-align: left; }
.ac-version span { color: var(--shell-text-2); overflow-wrap: anywhere; }
button.ac-version { cursor: pointer; }
button.ac-version:hover, .ac-version[aria-current="true"] { border-color: rgb(var(--shell-grad-a-rgb) / 0.6); background: rgb(var(--shell-grad-a-rgb) / 0.08); }
.ac-version[data-state="failed"] { border-style: dashed; }
.ac-operations { min-width: 0; max-height: 26rem; overflow: auto; }
.ac-operations table { width: 100%; border-collapse: collapse; font-size: 0.8125rem; }
.ac-operations th { position: sticky; top: 0; padding: 0.375rem 0.5rem; background: var(--kb-solid, var(--shell-bg)); color: var(--shell-text); font-weight: 600; text-align: left; }
.ac-operations td { padding: 0.375rem 0.5rem; border-top: 1px solid var(--shell-line); vertical-align: top; overflow-wrap: anywhere; }
.ac-operations tr[data-deprecated] td { opacity: 0.6; }
.ac-path { font-family: var(--font-mono); }
.ac-method { font-family: var(--font-mono); font-weight: 700; color: rgb(var(--shell-grad-a-rgb)); }
</style>
