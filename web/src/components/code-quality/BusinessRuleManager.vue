<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { AlertCircle, Download, ExternalLink, LoaderCircle, RefreshCw, Unplug, Scale, KeyRound, PlugZap, Link2, FileText } from 'lucide-vue-next';
import { makeRequest } from '../../ui/makeRequest';
import { toastMessage } from '../../composables/useToasts';
import RuleKnowledgePanel from './RuleKnowledgePanel.vue';

type Collection = { id: string; name: string };
type Connection = { instance_url: string; collection_id: string; credential_id: string; updated_at: string } | null;
type SyncSummary = { status?: string; reason?: string; collection_id?: string; document_count?: number; imported_count?: number; omitted_count?: number; oversized_count?: number; synchronized_at?: string };
type OutlineDocument = { id: string; title: string; url: string; updated_at?: string };
const props = defineProps<{ projectId: string }>();
const instanceUrl = ref('http://outline:3000');
const collectionId = ref('');
const token = ref('');
const collections = ref<Collection[]>([]);
const documents = ref<OutlineDocument[]>([]);
const documentsLoading = ref(false);
const documentsError = ref('');
const collectionsForUrl = ref('');
const connection = ref<Connection>(null);
const credentialSaved = ref(false);
const credentialError = ref('');
const loading = ref(false);
const saving = ref(false);
const testing = ref(false);
const exporting = ref(false);
const error = toastMessage('error');
const notice = toastMessage('success');
const latestSync = ref<SyncSummary | null>(null);
let loadSequence = 0;
let documentsRequest = 0;

const hasProject = computed(() => !!props.projectId);
const connected = computed(() => !!connection.value);
const hostedOutlineUrl = computed(() => {
  const configuredUrl = connection.value?.instance_url || instanceUrl.value || 'http://outline:3000';
  try {
    const url = new URL(configuredUrl);
    if (url.hostname === 'outline' && url.port === '3000') {
      url.protocol = typeof window === 'undefined' ? 'http:' : window.location.protocol;
      url.hostname = typeof window === 'undefined' ? 'localhost' : window.location.hostname;
      url.port = '3020';
    }
    return url.toString();
  } catch {
    return 'http://localhost:3020';
  }
});
const savedInstanceMatches = computed(() => !connection.value || instanceUrl.value === connection.value.instance_url);
const selectedCollectionIsVerified = computed(() => collectionsForUrl.value === instanceUrl.value
  && collections.value.some((item) => item.id === collectionId.value));
const canSave = computed(() => !!props.projectId && !!instanceUrl.value.trim() && !!collectionId.value
  && (!connected.value || credentialSaved.value || !!token.value.trim())
  && (savedInstanceMatches.value || !!token.value.trim()) && selectedCollectionIsVerified.value && !saving.value);

const request = async (path: string, init?: RequestInit) => {
  const response = await makeRequest(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || 'Outline request failed.');
  return data;
};
const basePath = () => '/api/code-quality/projects/' + encodeURIComponent(props.projectId) + '/outline';
const loadDocuments = async () => {
  const requestId = ++documentsRequest;
  const projectId = props.projectId;
  if (!projectId || !connection.value || !credentialSaved.value) {
    documents.value = [];
    return;
  }
  documentsLoading.value = true;
  documentsError.value = '';
  try {
    const path = '/api/code-quality/projects/' + encodeURIComponent(projectId) + '/outline/documents';
    const data = await request(path);
    if (requestId !== documentsRequest || projectId !== props.projectId) return;
    documents.value = Array.isArray(data.documents) ? data.documents : [];
  } catch (cause: any) {
    if (requestId === documentsRequest && projectId === props.projectId) documentsError.value = cause?.message || 'Outline business rules could not be loaded.';
  } finally { if (requestId === documentsRequest) documentsLoading.value = false; }
};
const documentHref = (item: OutlineDocument) => {
  try {
    const base = new URL(hostedOutlineUrl.value);
    const target = new URL(item.url, base);
    return target.origin === base.origin && target.pathname.startsWith('/doc/') ? target.href : base.href;
  } catch { return hostedOutlineUrl.value; }
};
const load = async () => {
  const sequence = ++loadSequence;
  if (!props.projectId) {
    connection.value = null; credentialSaved.value = false; credentialError.value = ''; collections.value = []; collectionsForUrl.value = ''; latestSync.value = null; documents.value = [];
    return;
  }
  loading.value = true; error.value = ''; notice.value = '';
  try {
    const data = await request(basePath());
    if (sequence !== loadSequence) return;
    connection.value = data.connection || null;
    credentialSaved.value = data.credential?.saved === true;
    credentialError.value = data.credential?.detail || '';
    if (connection.value) {
      instanceUrl.value = connection.value.instance_url;
      collectionId.value = connection.value.collection_id;
      try {
        const history = await request('/api/code-quality/projects/' + encodeURIComponent(props.projectId) + '/reports');
        if (sequence !== loadSequence) return;
        const reports = Array.isArray(history.reports) ? history.reports : [];
        latestSync.value = reports.find((report: any) => report.coverage?.business_rule_review)?.coverage.business_rule_review || null;
      } catch {
        latestSync.value = null;
      }
      if (sequence !== loadSequence) return;
      await loadDocuments();
    } else {
      instanceUrl.value = 'http://outline:3000'; collectionId.value = ''; latestSync.value = null; documents.value = [];
    }
  } catch (cause: any) {
    if (sequence === loadSequence) error.value = cause?.message || 'Outline settings could not be loaded.';
  } finally {
    if (sequence === loadSequence) loading.value = false;
  }
};
const loadCollections = async () => {
  if (!props.projectId || testing.value) return;
  testing.value = true; error.value = ''; notice.value = '';
  collections.value = []; collectionsForUrl.value = '';
  try {
    const data = await request(basePath() + '/collections', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instance_url: instanceUrl.value, token: token.value }),
    });
    collections.value = Array.isArray(data.collections) ? data.collections : [];
    collectionsForUrl.value = instanceUrl.value;
    if (!collections.value.some((item) => item.id === collectionId.value)) collectionId.value = '';
    notice.value = collections.value.length ? 'Outline is reachable. Choose the collection dedicated to this project’s business rules.'
      : 'Outline connected, but this account has no collections it can access.';
  } catch (cause: any) {
    error.value = cause?.message || 'Outline collections could not be loaded.';
  } finally { testing.value = false; }
};
const save = async () => {
  if (!canSave.value) return;
  saving.value = true; error.value = ''; notice.value = '';
  try {
    const data = await request(basePath(), {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instance_url: instanceUrl.value, collection_id: collectionId.value, token: token.value }),
    });
    connection.value = data.connection || null;
    credentialSaved.value = data.credential?.saved === true;
    token.value = '';
    notice.value = credentialSaved.value ? 'Outline connection saved. New assessments will read the selected collection.'
      : 'Connection settings were saved. Add an API token to the OS credential store to enable rule review.';
    await load();
  } catch (cause: any) { error.value = cause?.message || 'Outline connection could not be saved.'; }
  finally { saving.value = false; }
};
const saveToken = async () => {
  if (!props.projectId || !token.value.trim()) return;
  saving.value = true; error.value = ''; notice.value = '';
  try {
    await request(basePath() + '/credential', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: token.value }),
    });
    token.value = ''; credentialSaved.value = true; credentialError.value = '';
    notice.value = 'Outline API token saved to the OS credential store.';
    await loadDocuments();
  } catch (cause: any) { error.value = cause?.message || 'Outline token could not be saved.'; }
  finally { saving.value = false; }
};
const disconnect = async () => {
  if (!connected.value || saving.value) return;
  if (!window.confirm('Disconnect Outline for this project? Saved assessment reports keep their document snapshots.')) return;
  saving.value = true; error.value = ''; notice.value = '';
  try {
    await request(basePath(), { method: 'DELETE' });
    connection.value = null; credentialSaved.value = false; credentialError.value = ''; collections.value = []; collectionsForUrl.value = ''; collectionId.value = ''; token.value = ''; documents.value = []; documentsError.value = '';
    instanceUrl.value = 'http://outline:3000'; latestSync.value = null;
    notice.value = 'Outline disconnected. Existing assessment reports retain their snapshots.';
  } catch (cause: any) { error.value = cause?.message || 'Outline connection could not be removed.'; }
  finally { saving.value = false; }
};
const exportLocalRules = async () => {
  if (!props.projectId || exporting.value) return;
  exporting.value = true; error.value = '';
  try {
    const data = await request('/api/code-quality/projects/' + encodeURIComponent(props.projectId) + '/business-rules');
    const rules = Array.isArray(data.rules) ? data.rules : [];
    const lineBreak = '\n';
    const markdown = rules.length
      ? '# Migrated business rules' + lineBreak + lineBreak + rules.map((rule: any) =>
        '## ' + rule.title + lineBreak + lineBreak + 'Category: ' + rule.category + lineBreak + lineBreak + rule.statement)
        .join(lineBreak + lineBreak + '---' + lineBreak + lineBreak) + lineBreak
      : '# Migrated business rules' + lineBreak + lineBreak + 'No locally stored business rules were found.' + lineBreak;
    const link = document.createElement('a');
    link.href = URL.createObjectURL(new Blob([markdown], { type: 'text/markdown;charset=utf-8' }));
    link.download = props.projectId + '-business-rules.md';
    link.click(); URL.revokeObjectURL(link.href);
    notice.value = 'Local rules exported as Markdown. Copy them into the project’s Outline collection when ready.';
  } catch (cause: any) { error.value = cause?.message || 'Local business rules could not be exported.'; }
  finally { exporting.value = false; }
};
const formatDate = (value?: string) => value ? new Date(value).toLocaleString() : 'Not yet synchronized';

watch(() => props.projectId, () => { documentsRequest += 1; documentsLoading.value = false; token.value = ''; collections.value = []; collectionsForUrl.value = ''; documents.value = []; documentsError.value = ''; void load(); }, { immediate: true });
</script>

<template>
  <section class="kb-rules" aria-label="Outline business rules">
    <div v-if="!hasProject" class="kb-panel kb-notice">Choose a project ID in Files and indexing to configure its Outline collection.</div>
    <div v-else-if="loading" class="kb-panel kb-notice"><LoaderCircle class="kb-spin" aria-hidden="true" />Loading the project's connection…</div>

    <div v-else class="kb-two">
      <div class="kb-panel">
        <div class="kb-panel-head">
          <h2 class="kb-panel-title"><Scale aria-hidden="true" />Business rules in Outline</h2>
          <a :href="hostedOutlineUrl" target="_blank" rel="noreferrer" class="kb-btn"><ExternalLink aria-hidden="true" />Open Outline</a>
        </div>
        <p class="kb-lead">Outline holds the plain-language rules. Each project reads one dedicated collection before an assessment; this app never edits Outline documents.</p>

        <label class="kb-field">Outline instance URL
          <input v-model="instanceUrl" :disabled="saving || testing" type="url" maxlength="2048" placeholder="http://outline:3000" class="kb-input" />
          <span class="kb-help">Use <code>http://outline:3000</code> from this app's Docker network, or an HTTPS Outline address.</span>
        </label>
        <label class="kb-field">Outline API token
          <input v-model="token" :disabled="saving || testing" type="password" autocomplete="new-password" class="kb-input" :placeholder="credentialSaved ? 'Saved in the OS credential store; leave blank to keep it' : 'Paste a read-only Outline API token'" />
          <span class="kb-help">Stored in the host OS credential store and sent only for Outline API calls.</span>
        </label>
        <label v-if="collections.length" class="kb-field">Dedicated project collection
          <select v-model="collectionId" :disabled="saving" class="kb-input">
            <option value="" disabled>Choose a collection</option>
            <option v-for="item in collections" :key="item.id" :value="item.id">{{ item.name }}</option>
          </select>
          <span class="kb-help">Use a collection that contains only this project's business rules.</span>
        </label>

        <div class="kb-btns">
          <button type="button" :disabled="!hasProject || testing || saving || !instanceUrl.trim() || (!token.trim() && (!credentialSaved || !savedInstanceMatches))" class="kb-btn" @click="loadCollections">
            <LoaderCircle v-if="testing" class="kb-spin" aria-hidden="true" /><RefreshCw v-else aria-hidden="true" />{{ testing ? 'Checking Outline…' : 'Load collections' }}
          </button>
          <button type="button" :disabled="!canSave" class="kb-btn" data-variant="primary" @click="save">
            <LoaderCircle v-if="saving" class="kb-spin" aria-hidden="true" /><Link2 v-else aria-hidden="true" />{{ saving ? 'Saving…' : connected ? 'Save connection' : 'Connect collection' }}
          </button>
          <button v-if="connected && token.trim() && savedInstanceMatches" type="button" :disabled="saving" class="kb-btn" @click="saveToken"><KeyRound aria-hidden="true" />Save token only</button>
          <button v-if="connected" type="button" :disabled="saving" class="kb-btn" data-variant="danger" @click="disconnect"><Unplug aria-hidden="true" />Disconnect</button>
        </div>

      </div>

      <div class="kb-panel">
        <template v-if="connected">
          <div class="kb-status">
            <span class="kb-project-icon"><PlugZap aria-hidden="true" /></span>
            <div class="min-w-0">
              <p class="kb-status-name">{{ credentialSaved ? 'Connected' : 'Connection needs a token' }}</p>
              <p class="kb-project-meta truncate">{{ connection?.instance_url }} · collection {{ connection?.collection_id }}</p>
            </div>
            <span class="kb-tag" :data-tone="credentialSaved ? 'ok' : 'warn'"><KeyRound aria-hidden="true" />{{ credentialSaved ? 'Token saved' : 'Token not saved' }}</span>
          </div>
          <p v-if="credentialError" class="kb-rev-note" data-tone="warn"><AlertCircle aria-hidden="true" />{{ credentialError }}</p>
          <div class="kb-sync" :data-tone="latestSync?.status === 'unavailable' ? 'warn' : latestSync?.status === 'synchronized' ? 'ok' : undefined">
            <template v-if="latestSync?.status === 'synchronized'"><b>Last assessment sync:</b> {{ latestSync.imported_count }} rules from {{ latestSync.document_count }} documents · {{ formatDate(latestSync.synchronized_at) }}<span v-if="latestSync.omitted_count"> · {{ latestSync.omitted_count }} documents omitted</span><span v-if="latestSync.oversized_count"> · {{ latestSync.oversized_count }} over the 12,000 character limit</span></template>
            <template v-else-if="latestSync?.status === 'unavailable'"><b>The last assessment couldn't sync rules:</b> {{ latestSync.reason }}</template>
            <template v-else>No assessment has synchronized this collection yet.</template>
          </div>

          <section class="kb-docs-section" aria-labelledby="outline-business-rule-documents">
            <div class="kb-panel-head">
              <div>
                <h3 id="outline-business-rule-documents" class="kb-panel-title kb-panel-title-sm">Rule documents</h3>
                <p class="kb-help">Open one in Outline to read or edit it with your Outline permissions.</p>
              </div>
              <button type="button" :disabled="documentsLoading || !credentialSaved" class="kb-btn" @click="loadDocuments"><LoaderCircle v-if="documentsLoading" class="kb-spin" aria-hidden="true" /><RefreshCw v-else aria-hidden="true" />Refresh</button>
            </div>
            <p v-if="!credentialSaved" class="kb-rev-note" data-tone="warn">Save the Outline API token to load the collection's documents.</p>
            <p v-else-if="documentsLoading && !documents.length" class="kb-help">Loading Outline documents…</p>
            <p v-else-if="documentsError" role="alert" class="kb-rev-note" data-tone="bad">{{ documentsError }}</p>
            <p v-else-if="!documents.length" class="kb-help">No business rule documents were found in this collection.</p>
            <ul v-else class="kb-docs">
              <li v-for="item in documents" :key="item.id">
                <a :href="documentHref(item)" target="_blank" rel="noopener noreferrer" class="kb-doc">
                  <FileText aria-hidden="true" /><span class="truncate">{{ item.title }}</span><ExternalLink aria-hidden="true" /><span class="sr-only">Open in Outline in a new tab</span>
                </a>
              </li>
            </ul>
          </section>
        </template>
        <div v-else class="kb-howto">
          <h3 class="kb-panel-title kb-panel-title-sm">Connect a collection</h3>
          <ol>
            <li>Create a dedicated Outline collection for this project.</li>
            <li>Create a read-only Outline API token and paste it here.</li>
            <li>Load the collections, pick this project's one, and connect it.</li>
          </ol>
          <p class="kb-help">Each document becomes one rule; documents without a title or body are skipped. Up to 50 documents are synchronized and up to 20 are reviewed per assessment.</p>
        </div>

        <div class="kb-migrate">
          <div>
            <h3 class="kb-panel-title kb-panel-title-sm">Migrate local rules</h3>
            <p class="kb-help">Export existing rules to copy into Outline. Saved reports keep their original snapshots.</p>
          </div>
          <button type="button" :disabled="exporting" class="kb-btn" @click="exportLocalRules"><LoaderCircle v-if="exporting" class="kb-spin" aria-hidden="true" /><Download v-else aria-hidden="true" />{{ exporting ? 'Exporting…' : 'Export' }}</button>
        </div>
      </div>
    </div>

    <RuleKnowledgePanel v-if="hasProject && connected && !loading" :project-id="props.projectId" :credential-saved="credentialSaved" />
  </section>
</template>
