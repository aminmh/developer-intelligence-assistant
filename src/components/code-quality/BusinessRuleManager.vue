<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { AlertCircle, CheckCircle2, Download, ExternalLink, LoaderCircle, RefreshCw, Unplug } from 'lucide-vue-next';
import { makeRequest } from '../../ui/makeRequest';

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
const error = ref('');
const notice = ref('');
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
  <section class="space-y-5" aria-label="Outline business rules">
    <div class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5 sm:p-6">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 class="text-xl font-semibold">Business rules in Outline</h2>
          <p class="mt-1 max-w-3xl text-sm leading-relaxed text-slate-400">Outline is the source for plain-language rules. Each project reads one dedicated collection before an assessment; Code Quality never edits Outline documents.</p>
        </div>
        <a :href="hostedOutlineUrl" target="_blank" rel="noreferrer" class="inline-flex min-h-9 items-center gap-2 rounded-lg border border-slate-700 px-3 text-sm text-slate-300 hover:bg-slate-800"><ExternalLink :size="14" />Open Outline</a>
      </div>
      <div v-if="!hasProject" class="mt-5 rounded-lg border border-slate-800 p-3 text-sm text-slate-400">Select a project revision to configure its Outline collection.</div>
      <div v-else-if="loading" class="mt-5 flex items-center gap-2 text-sm text-slate-400"><LoaderCircle class="motion-safe:animate-spin" :size="16" />Loading project connection…</div>
      <div v-else class="mt-5 space-y-5">
        <div class="grid gap-4 lg:grid-cols-2">
          <label class="block text-sm font-medium text-slate-300">Outline instance URL
            <input v-model="instanceUrl" :disabled="saving || testing" type="url" maxlength="2048" placeholder="http://outline:3000" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm text-slate-100 outline-none focus:border-indigo-400 disabled:opacity-50" />
            <span class="mt-1 block text-xs font-normal text-slate-500">Use <code>http://outline:3000</code> from this app’s Docker network, or an HTTPS Outline address.</span>
          </label>
          <label class="block text-sm font-medium text-slate-300">Outline API token
            <input v-model="token" :disabled="saving || testing" type="password" autocomplete="new-password" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm text-slate-100 outline-none focus:border-indigo-400 disabled:opacity-50" :placeholder="credentialSaved ? 'Saved in the OS credential store; leave blank to keep it' : 'Paste a read-only Outline API token'" />
            <span class="mt-1 block text-xs font-normal text-slate-500">The token is stored in the host OS credential store and sent only for Outline API calls.</span>
          </label>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <button type="button" :disabled="!hasProject || testing || saving || !instanceUrl.trim() || (!token.trim() && (!credentialSaved || !savedInstanceMatches))" class="inline-flex min-h-10 items-center gap-2 rounded-lg border border-slate-700 px-4 text-sm font-medium text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-45" @click="loadCollections">
            <LoaderCircle v-if="testing" class="motion-safe:animate-spin" :size="15" /><RefreshCw v-else :size="15" />{{ testing ? 'Checking Outline…' : 'Load collections' }}
          </button>
          <button type="button" :disabled="!canSave" class="inline-flex min-h-10 items-center gap-2 rounded-lg bg-indigo-500 px-4 text-sm font-semibold text-white hover:bg-indigo-400 disabled:cursor-not-allowed disabled:opacity-45" @click="save">
            <LoaderCircle v-if="saving" class="motion-safe:animate-spin" :size="15" />{{ saving ? 'Saving…' : connected ? 'Save connection' : 'Connect collection' }}
          </button>
          <button v-if="connected && token.trim() && savedInstanceMatches" type="button" :disabled="saving" class="min-h-10 rounded-lg border border-slate-700 px-3 text-sm text-slate-300 hover:bg-slate-800 disabled:opacity-50" @click="saveToken">Save token only</button>
          <button v-if="connected" type="button" :disabled="saving" class="inline-flex min-h-10 items-center gap-2 rounded-lg border border-rose-500/30 px-3 text-sm text-rose-200 hover:bg-rose-500/10 disabled:opacity-50" @click="disconnect"><Unplug :size="14" />Disconnect</button>
        </div>
        <label v-if="collections.length" class="block max-w-2xl text-sm font-medium text-slate-300">Dedicated project collection
          <select v-model="collectionId" :disabled="saving" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm text-slate-100">
            <option value="" disabled>Choose a collection</option>
            <option v-for="item in collections" :key="item.id" :value="item.id">{{ item.name }}</option>
          </select>
          <span class="mt-1 block text-xs font-normal text-slate-500">Use a collection that contains only this project’s business rules.</span>
        </label>
        <div v-if="connected" class="rounded-xl border border-slate-800 bg-slate-950/60 p-4">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div><p class="font-medium text-slate-100">{{ credentialSaved ? 'Connected' : 'Connection needs a token' }}</p><p class="mt-1 text-xs text-slate-400">{{ connection?.instance_url }} · collection {{ connection?.collection_id }}</p><p v-if="credentialError" class="mt-1 text-xs text-amber-200">{{ credentialError }}</p></div>
            <span class="rounded-full px-3 py-1 text-xs" :class="credentialSaved ? 'bg-emerald-500/10 text-emerald-200' : 'bg-amber-500/10 text-amber-200'">{{ credentialSaved ? 'OS token saved' : 'Token not saved' }}</span>
          </div>
          <div class="mt-3 border-t border-slate-800 pt-3 text-xs text-slate-400">
            <template v-if="latestSync?.status === 'synchronized'">Latest assessment sync: {{ latestSync.imported_count }} rules read from {{ latestSync.document_count }} documents · {{ formatDate(latestSync.synchronized_at) }}<span v-if="latestSync.omitted_count"> · {{ latestSync.omitted_count }} documents omitted</span><span v-if="latestSync.oversized_count"> · {{ latestSync.oversized_count }} documents exceeded the 12,000 character limit</span>.</template>
            <template v-else-if="latestSync?.status === 'unavailable'">Latest assessment could not synchronize rules: {{ latestSync.reason }}</template>
            <template v-else>No assessment has synchronized this collection yet.</template>
          </div>
          <section class="mt-4 border-t border-slate-800 pt-4" aria-labelledby="outline-business-rule-documents">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <div><h3 id="outline-business-rule-documents" class="text-sm font-semibold text-slate-200">Business rule documents</h3><p class="mt-1 text-xs text-slate-500">Open a document in Outline to read or edit it using your Outline permissions.</p></div>
              <button type="button" :disabled="documentsLoading || !credentialSaved" class="inline-flex min-h-9 items-center gap-2 rounded-lg border border-slate-700 px-3 text-xs text-slate-300 disabled:opacity-50" @click="loadDocuments"><LoaderCircle v-if="documentsLoading" class="motion-safe:animate-spin" :size="13" /><RefreshCw v-else :size="13" />Refresh documents</button>
            </div>
            <p v-if="!credentialSaved" class="mt-3 text-xs text-amber-200">Save the Outline API token to load the collection’s documents.</p>
            <p v-else-if="documentsLoading && !documents.length" class="mt-3 text-xs text-slate-400">Loading Outline documents…</p>
            <p v-else-if="documentsError" role="alert" class="mt-3 text-xs text-rose-200">{{ documentsError }}</p>
            <p v-else-if="!documents.length" class="mt-3 text-xs text-slate-500">No business rule documents were found in this collection.</p>
            <ul v-else class="mt-3 grid gap-2 sm:grid-cols-2">
              <li v-for="item in documents" :key="item.id"><a :href="documentHref(item)" target="_blank" rel="noopener noreferrer" class="group flex min-h-12 items-center justify-between gap-3 rounded-lg border border-slate-700 bg-slate-950/40 px-3 py-2 text-sm text-slate-200 transition hover:border-indigo-400/50 hover:bg-indigo-500/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"><span class="min-w-0 truncate">{{ item.title }}</span><ExternalLink class="shrink-0 text-slate-500 group-hover:text-indigo-200" :size="14" /><span class="sr-only">Open in Outline in a new tab</span></a></li>
            </ul>
          </section>
        </div>
        <div v-if="!connected" class="rounded-xl border border-indigo-400/20 bg-indigo-400/5 p-4 text-sm leading-relaxed text-indigo-100">
          Create a dedicated Outline collection for this project, create a read-only API token, load the collection list, then save the connection. Each document becomes one rule; documents without a title or body are skipped. Up to 50 documents are synchronized and up to 20 are reviewed per assessment.
        </div>
        <div class="flex flex-wrap items-center justify-between gap-3 border-t border-slate-800 pt-4">
          <div><h3 class="text-sm font-medium text-slate-200">Migrate existing local rules</h3><p class="mt-1 text-xs text-slate-500">Export existing rules for manual copy into Outline. Saved reports retain their original snapshots.</p></div>
          <button type="button" :disabled="exporting" class="inline-flex min-h-9 items-center gap-2 rounded-lg border border-slate-700 px-3 text-sm text-slate-300 hover:bg-slate-800 disabled:opacity-50" @click="exportLocalRules"><LoaderCircle v-if="exporting" class="motion-safe:animate-spin" :size="14" /><Download v-else :size="14" />{{ exporting ? 'Exporting…' : 'Export local rules' }}</button>
        </div>
      </div>
      <div v-if="error" role="alert" class="mt-4 flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-200"><AlertCircle :size="16" class="mt-0.5 shrink-0" /><span>{{ error }}</span></div>
      <div v-if="notice" role="status" class="mt-4 flex items-start gap-2 rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-3 text-sm text-emerald-200"><CheckCircle2 :size="16" class="mt-0.5 shrink-0" /><span>{{ notice }}</span></div>
    </div>
  </section>
</template>
