<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { Database, ExternalLink, RefreshCw } from 'lucide-vue-next';
import { makeRequest } from '../../ui/makeRequest';
import { discoverRuntimeSchema, runtimeSchemaOperation, saveRuntimeSchema, type RuntimeSchemaSsh } from '../../utils/runtimeSchema';
import type { RuntimeSchemaDiscovery, RuntimeSchemaSource, SavedSchemaDraft } from '../../types';
import { toast, toastMessage } from '../../composables/useToasts';

const props = defineProps<{
  projectId: string;
  acceptDraft: (draft: SavedSchemaDraft, reservedWindow: Window | null) => boolean;
  canLoad: () => boolean;
}>();
const params = new URLSearchParams(window.location.search);
const project = ref(params.get('project_id') || props.projectId);
const environment = ref(params.get('environment') || '');
const wantedContainer = params.get('container_id') || '';
const sources = ref<RuntimeSchemaSource[]>([]);
const verification = ref<Record<string, { discovery?: RuntimeSchemaDiscovery; database?: string; error?: string }>>({});
const selected = ref('');
const discovery = ref<RuntimeSchemaDiscovery | null>(null);
const database = ref('');
const inspectedDatabase = ref('');
const schemas = ref<string[]>([]);
const busy = ref('');
const message = toastMessage('success');
const showError = (text: string) => toast.error(`${text} Your current draft and edits are preserved; retry after correcting the issue.`);
const savedDraft = ref<SavedSchemaDraft | null>(null);
const sourceKey = (source: RuntimeSchemaSource) => JSON.stringify([source.project_id, source.environment, source.host_id, source.container_id]);
const source = computed(() => sources.value.find(item => sourceKey(item) === selected.value));
const databaseGroups = computed(() => ['postgresql', 'mysql', 'mariadb'].map(engine => ({ engine,
  sources: sources.value.filter(item => verification.value[sourceKey(item)]?.discovery?.engine === engine),
})).filter(group => group.sources.length));
// Containers that are simply not databases (the app, a cache) are not worth a warning; only a
// database that could not be verified (remote host, missing client, permissions) needs attention.
const notADatabase = /not a verified PostgreSQL|No supported database engine/i;
const unverifiedSources = computed(() => sources.value.filter(item => {
  const entry = verification.value[sourceKey(item)];
  return !entry?.discovery && !(entry?.error && notADatabase.test(entry.error));
}));
const verifiedSelection = computed({ get: () => verification.value[selected.value]?.discovery ? selected.value : '', set: value => { selected.value = value; } });
const candidateSelection = computed({ get: () => verification.value[selected.value]?.discovery ? '' : selected.value, set: value => { selected.value = value; } });
let registryRevision = 0;
let sourceRevision = 0;
let selectionRevision = 0;
const registryIdentity = () => JSON.stringify([registryRevision, project.value.trim(), environment.value.trim()]);
const sourceIdentity = () => JSON.stringify([registryIdentity(), sourceRevision, selected.value]);
const selectionIdentity = () => JSON.stringify([sourceIdentity(), selectionRevision, database.value, [...schemas.value].sort()]);
const sshHost = ref('');
const sshUser = ref('');
const sshPort = ref('22');
const scanned = ref<{ host: string; ssh_user?: string; ssh_port?: number; fingerprint: string } | null>(null);
const confirmed = ref(false);
const canExtract = computed(() => !!source.value && !!discovery.value && !!database.value &&
  (discovery.value.engine !== 'postgresql' || (inspectedDatabase.value === database.value && schemas.value.length > 0)));
function resetDiscovery() { discovery.value = null; database.value = ''; inspectedDatabase.value = ''; schemas.value = []; }
function applyDiscovery(result: RuntimeSchemaDiscovery, chosenDatabase = '') {
  discovery.value = result;
  database.value = chosenDatabase || result.configured_database || result.databases[0] || '';
  inspectedDatabase.value = database.value;
  schemas.value = result.configured_schemas?.filter(name => result.schemas.includes(name)) || [];
  if (!schemas.value.length && result.schemas.includes('public')) schemas.value = ['public'];
}
watch(selected, () => {
  sourceRevision++;
  resetDiscovery(); scanned.value = null; confirmed.value = false;
  const cached = verification.value[selected.value];
  if (cached?.discovery) applyDiscovery(cached.discovery, cached.database);
}, { flush: 'sync' });
watch([project, environment], () => { registryRevision++; sources.value = []; verification.value = {}; selected.value = ''; resetDiscovery(); }, { flush: 'sync' });
watch(() => props.projectId, value => { project.value = value; }, { flush: 'sync' });
watch([sshHost, sshUser, sshPort], () => { scanned.value = null; confirmed.value = false; }, { flush: 'sync' });
watch(database, () => { selectionRevision++; schemas.value = []; }, { flush: 'sync' });
watch(schemas, () => { selectionRevision++; }, { deep: true, flush: 'sync' });

async function run(label: string, action: () => Promise<void>, stillCurrent = () => true) {
  if (busy.value) return;
  busy.value = label; message.value = '';
  try { await action(); }
  catch (cause: any) { if (stillCurrent()) showError(cause.message || 'The schema operation failed. Check your selection and retry.'); }
  finally { busy.value = ''; }
}
async function findContainers() {
  const identity = registryIdentity();
  const stillCurrent = () => identity === registryIdentity();
  await run('Finding and verifying database containers…', async () => {
    if (!project.value.trim()) throw new Error('Choose a project before finding its containers.');
    const query = new URLSearchParams({ project_id: project.value.trim() });
    if (environment.value.trim()) query.set('environment', environment.value.trim());
    const response = await makeRequest(`/api/runtime/schema-sources?${query}`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Could not list reporting containers. Sign in as a global administrator and retry.');
    if (!stillCurrent()) return;
    const candidates: RuntimeSchemaSource[] = data.sources || [];
    const results = await Promise.all(candidates.map(async item => {
      try {
        const result = await discoverRuntimeSchema(makeRequest, { source: item });
        if (!['postgresql', 'mysql', 'mariadb'].includes(result.engine)) throw new Error('No supported database engine was verified.');
        return [sourceKey(item), { discovery: result }] as const;
      } catch (cause: any) { return [sourceKey(item), { error: cause.message || 'Database verification failed.' }] as const; }
    }));
    if (!stillCurrent()) return;
    selected.value = ''; resetDiscovery();
    sources.value = candidates;
    verification.value = Object.fromEntries(results);
    const verified = candidates.filter(item => verification.value[sourceKey(item)]?.discovery);
    const preferred = verified.find(item => item.container_id === wantedContainer) || verified[0];
    selected.value = preferred ? sourceKey(preferred) : '';
    if (verified.length && discovery.value?.databases.length) message.value = 'Database containers verified. Review the database and schemas before loading.';
    else toast.info(verified.length
      ? 'No readable databases were found. Ensure the database container is included in Runtime setup and its configured database user can read the catalog.'
      : 'No PostgreSQL, MySQL, or MariaDB container was verified. The database container must be included in Runtime setup and reporting for this project and environment. For a remote host, associate its SSH target below and retry verification.');
  }, stillCurrent);
}
async function discover(chosenDatabase = '') {
  const identity = sourceIdentity();
  const previousDatabase = database.value;
  const stillCurrent = () => identity === sourceIdentity() && previousDatabase === database.value;
  await run(chosenDatabase ? 'Discovering PostgreSQL schemas…' : 'Discovering database engine…', async () => {
    if (!source.value) throw new Error('Choose a reporting container first.');
    const result = await discoverRuntimeSchema(makeRequest, { source: source.value, ...(chosenDatabase ? { database: chosenDatabase } : {}) });
    if (!stillCurrent()) return;
    verification.value[selected.value] = { discovery: result, database: chosenDatabase };
    applyDiscovery(result, chosenDatabase);
    if (result.databases.length) message.value = `${result.engine} ${result.version}. Review the database${result.engine === 'postgresql' ? ' and PostgreSQL schemas' : ''} before loading.`;
    else toast.info('No readable databases were found. Ensure the database container is included in Runtime setup and its configured database user can read the catalog.');
  }, stillCurrent);
}
function sshInput(): RuntimeSchemaSsh {
  const port = Number(sshPort.value);
  if (!sshHost.value.trim() || !Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Enter an SSH host and a port between 1 and 65535.');
  return { host: sshHost.value.trim(), ...(sshUser.value.trim() ? { ssh_user: sshUser.value.trim() } : {}), ssh_port: port };
}
async function scanHost() {
  const identity = sourceIdentity();
  const sshIdentity = JSON.stringify([sshHost.value, sshUser.value, sshPort.value]);
  const stillCurrent = () => identity === sourceIdentity() && sshIdentity === JSON.stringify([sshHost.value, sshUser.value, sshPort.value]);
  await run('Scanning SSH host key…', async () => {
    if (!source.value) throw new Error('Choose the reporting container whose host you want to associate.');
    confirmed.value = false;
    const result = await runtimeSchemaOperation<typeof scanned.value>(makeRequest, 'ssh-scan', { source: source.value }, sshInput());
    if (stillCurrent()) scanned.value = result;
  }, stillCurrent);
}
async function associateHost() {
  const identity = sourceIdentity();
  const stillCurrent = () => identity === sourceIdentity();
  await run('Verifying and associating SSH host…', async () => {
    if (!source.value || !scanned.value || !confirmed.value) throw new Error('Scan the SSH host and confirm its fingerprint first.');
    await runtimeSchemaOperation(makeRequest, 'associate', { source: source.value }, {
      ...sshInput(), host_fingerprint: scanned.value.fingerprint, confirmed: true,
    });
    if (!stillCurrent()) return;
    resetDiscovery();
    message.value = 'SSH host associated with the reporting host. Discover the container database to continue.';
  }, stillCurrent);
}
function openSavedDraft() {
  if (!savedDraft.value) return;
  // Check before opening a window, so a refusal never flashes a blank window.
  if (!props.canLoad()) { showError('Close the open Draw.io window or finish the current draft operation, then try again.'); return; }
  const reserved = window.open('about:blank', '_blank');
  if (!props.acceptDraft(savedDraft.value, reserved)) {
    reserved?.close();
    showError('Draw.io was not opened. Save or discard pending draft changes and close any open editor window, then try again.');
  }
}
function loadSchema() {
  if (busy.value || !canExtract.value || !source.value) return;
  if (!props.canLoad()) { showError('Close the open Draw.io window or finish the current draft operation, then try again.'); return; }
  // Reserve while the click still has browser activation; extraction takes time.
  const reserved = window.open('about:blank', '_blank');
  const selection = { source: source.value, database: database.value, schemas: discovery.value?.engine === 'postgresql' ? [...schemas.value] : [] };
  const identity = selectionIdentity();
  const stillCurrent = () => identity === selectionIdentity();
  void run('Reading database catalog and saving a draft…', async () => {
    try {
      const draft = await saveRuntimeSchema(makeRequest, selection);
      savedDraft.value = draft;
      if (!stillCurrent()) {
        reserved?.close();
        message.value = 'The schema draft was saved for the previous source. Open the saved draft when you are ready; your current draft and edits are preserved.';
        return;
      }
      const opened = props.acceptDraft(draft, reserved);
      if (!opened) reserved?.close();
      message.value = opened ? 'Runtime schema saved as a new draft. Connecting Draw.io…' : 'The new draft is saved. Open saved draft in Draw.io when you are ready.';
    } catch (cause) { reserved?.close(); throw cause; }
  }, stillCurrent);
}
</script>

<template>
  <section class="sc-card runtime-schema-loader" aria-labelledby="runtime-schema-title">
    <div class="sc-sec-head"><h2 id="runtime-schema-title" class="sc-sec-title"><Database aria-hidden="true" />Load database schema from Runtime</h2></div>
    <div class="runtime-schema-body">
      <p class="sc-hint sc-m0">Read a running PostgreSQL, MySQL, or MariaDB catalog into a new draft. Find and verify database containers, then review the database selection. The database container must be included in Runtime setup. Global administrator access and the local Runtime helper are required.</p>
      <fieldset :disabled="!!busy" class="runtime-schema-fields">
        <legend class="sr-only">Runtime source</legend>
        <label>Project<input v-model="project" class="sc-input" placeholder="Project ID" /></label>
        <label>Environment<input v-model="environment" class="sc-input" placeholder="All environments" /></label>
        <button type="button" class="sc-btn" @click="findContainers"><RefreshCw aria-hidden="true" />Find containers</button>
        <label v-if="databaseGroups.length" class="runtime-schema-wide">Verified database container<select v-model="verifiedSelection" class="sc-input"><option disabled value="">Choose a verified database container</option><optgroup v-for="group in databaseGroups" :key="group.engine" :label="group.engine"><option v-for="item in group.sources" :key="sourceKey(item)" :value="sourceKey(item)">{{ item.service }} · {{ item.environment }} · {{ item.container_id.slice(0, 12) }} · host {{ item.host_id.slice(0, 16) }}</option></optgroup></select></label>
        <button v-if="source" type="button" class="sc-btn" @click="discover()">{{ discovery ? 'Refresh database discovery' : 'Verify selected container' }}</button>
      </fieldset>
      <details v-if="unverifiedSources.length" class="runtime-schema-help" :open="!databaseGroups.length">
        <summary>A database was not verified? Check it or connect a remote host</summary>
        <p class="sc-hint">These reporting containers could not be verified as databases. Select one to see why. A database on another machine needs its Docker host associated over SSH first.</p>
        <fieldset :disabled="!!busy" class="runtime-schema-fields"><legend class="sr-only">Unverified reporting candidates</legend><label class="runtime-schema-wide">Container<select v-model="candidateSelection" class="sc-input"><option disabled value="">Choose a container</option><option v-for="item in unverifiedSources" :key="sourceKey(item)" :value="sourceKey(item)">{{ item.service }} · {{ item.environment }} · {{ item.container_id.slice(0, 12) }}</option></select></label></fieldset>
        <template v-if="source && !discovery">
          <p class="sc-hint runtime-schema-id">Why: {{ verification[selected]?.error || 'The database engine has not been verified.' }}</p>
          <p class="sc-hint">Remote Docker host over SSH: uses this computer's SSH configuration and credentials. Verify the scanned host key before confirming.</p>
          <fieldset :disabled="!!busy" class="runtime-schema-fields"><legend class="sr-only">SSH host association</legend>
            <label>SSH host or alias<input v-model="sshHost" class="sc-input" autocomplete="off" /></label>
            <label>SSH user (optional)<input v-model="sshUser" class="sc-input" autocomplete="off" /></label>
            <label>SSH port<input v-model="sshPort" type="number" min="1" max="65535" class="sc-input" /></label>
            <button type="button" class="sc-btn" @click="scanHost">Scan SSH fingerprint</button>
            <template v-if="scanned"><p class="runtime-schema-wide runtime-schema-id">{{ scanned.host }} · {{ scanned.ssh_user || 'SSH configured user' }} · port {{ scanned.ssh_port }}<br />{{ scanned.fingerprint }}</p><label class="runtime-schema-checkbox runtime-schema-wide"><input v-model="confirmed" type="checkbox" />I verified this fingerprint for the selected reporting host.</label><button type="button" class="sc-btn" :disabled="!confirmed" @click="associateHost">Confirm host association</button></template>
          </fieldset>
          <button type="button" class="sc-btn" :disabled="!!busy" @click="discover()">Verify again</button>
        </template>
      </details>
      <p v-if="source" class="sc-hint sc-m0 runtime-schema-id">Host {{ source.host_id }} · image {{ source.image_id }}</p>
      <fieldset v-if="discovery" :disabled="!!busy" class="runtime-schema-fields">
        <legend>{{ discovery.engine }} {{ discovery.version }}</legend>
        <label>Database<select v-model="database" class="sc-input"><option v-for="name in discovery.databases" :key="name" :value="name">{{ name }}</option></select></label>
        <template v-if="discovery.engine === 'postgresql'">
          <button v-if="database !== inspectedDatabase" type="button" class="sc-btn" @click="discover(database)">Discover PostgreSQL schemas</button>
          <fieldset v-else class="runtime-schema-fields"><legend>PostgreSQL schemas</legend><label v-for="name in discovery.schemas" :key="name" class="runtime-schema-checkbox"><input v-model="schemas" type="checkbox" :value="name" />{{ name }}</label><p v-if="!discovery.schemas.length" class="sc-hint sc-m0">No readable user schemas were found in this database.</p></fieldset>
        </template>
      </fieldset>
      <div class="sc-row"><button type="button" class="sc-btn" data-variant="primary" :disabled="!!busy || !canExtract" @click="loadSchema"><ExternalLink aria-hidden="true" />Load schema in Draw.io</button><button v-if="savedDraft" type="button" class="sc-btn" :disabled="!!busy" @click="openSavedDraft">Open saved draft in Draw.io</button></div>
      <p v-if="busy" role="status" class="sc-hint sc-m0">{{ busy }}</p>
    </div>
  </section>
</template>
<style scoped>
.runtime-schema-body { display: grid; gap: 1rem; padding: 1rem; }
.runtime-schema-fields { display: flex; align-items: end; flex-wrap: wrap; gap: .75rem; border: 0; padding: 0; min-width: 0; }
.runtime-schema-fields legend { margin-bottom: .5rem; font-size: .85rem; font-weight: 600; }
.runtime-schema-fields > label { display: grid; gap: .35rem; font-size: .8rem; max-width: 100%; }
.runtime-schema-fields .runtime-schema-checkbox { display: flex; align-items: center; gap: .5rem; }
.runtime-schema-wide { flex-basis: 100%; }
.runtime-schema-id { overflow-wrap: anywhere; font-size: .75rem; }
.runtime-schema-loader select { max-width: 100%; }
.runtime-schema-loader summary { cursor: pointer; font-size: .85rem; }
</style>
