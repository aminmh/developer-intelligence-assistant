<script setup lang="ts">
import { ref, watch } from 'vue';
import { GitBranch, LoaderCircle, RefreshCw, Save, ShieldCheck, Unplug, KeyRound, Download, Cpu, CircleAlert } from 'lucide-vue-next';
import { makeRequest } from '../../ui/makeRequest';
import { toastMessage } from '../../composables/useToasts';

type Source = { project_id: string; repository: string; branch: string; status: string; last_commit_sha?: string | null; last_revision_id?: string | null; error_code?: string | null; credential_id: string };
const props = defineProps<{ defaultProjectId?: string }>();
const projectId = ref('');
const repository = ref('');
const branch = ref('main');
const token = ref('');
const source = ref<Source | null>(null);
const credentialSaved = ref(false);
const busy = ref(false);
const message = toastMessage('success');
const error = toastMessage('error');
const emit = defineEmits<{ revisionReady: [projectId: string, revisionId: string] }>();

const request = async (path: string, init?: RequestInit) => {
  const response = await makeRequest(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || 'Git source operation failed.');
  return data;
};
const sourceUrl = () => `/api/code-quality/projects/${encodeURIComponent(projectId.value.trim())}/git-source`;
const loadSource = async () => {
  if (!projectId.value.trim()) return;
  const data = await request(sourceUrl());
  source.value = data.source || null;
  if (source.value) {
    repository.value = source.value.repository;
    branch.value = source.value.branch;
    const credential = await request(`${sourceUrl()}/credential`);
    credentialSaved.value = credential.saved === true;
    if (source.value.status === 'ready' && source.value.last_revision_id) emit('revisionReady', source.value.project_id, source.value.last_revision_id);
  } else {
    credentialSaved.value = false;
  }
};
// Follows the project chosen in the header switcher.
watch(() => props.defaultProjectId, async (value) => {
  const next = (value || '').trim();
  if (next === projectId.value) return;
  projectId.value = next;
  source.value = null;
  repository.value = '';
  branch.value = 'main';
  token.value = '';
  credentialSaved.value = false;
  message.value = '';
  error.value = '';
  if (next) await loadSource().catch((cause: any) => { error.value = cause?.message || 'Git source operation failed.'; });
}, { immediate: true });
const saveSource = async () => {
  busy.value = true; error.value = ''; message.value = '';
  try {
    const data = await request(sourceUrl(), { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repository: repository.value.trim(), branch: branch.value.trim() }) });
    source.value = data.source;
    const status = await request(`${sourceUrl()}/credential`);
    credentialSaved.value = status.saved === true;
    message.value = 'Repository and branch saved.';
  } catch (cause: any) { error.value = cause?.message || 'Repository settings could not be saved.'; }
  finally { busy.value = false; }
};
const saveToken = async () => {
  if (!token.value || !source.value) return;
  busy.value = true; error.value = ''; message.value = '';
  try {
    await request(`${sourceUrl()}/credential`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: token.value }) });
    token.value = ''; credentialSaved.value = true; message.value = 'GitHub token saved to the OS credential store.';
  } catch (cause: any) { error.value = cause?.message || 'GitHub token could not be saved.'; }
  finally { busy.value = false; }
};
const removeToken = async () => {
  if (!source.value) return;
  busy.value = true; error.value = ''; message.value = '';
  try {
    await request(`${sourceUrl()}/credential`, { method: 'DELETE' });
    credentialSaved.value = false; message.value = 'Saved GitHub token removed.';
  } catch (cause: any) { error.value = cause?.message || 'GitHub token could not be removed.'; }
  finally { busy.value = false; }
};
const synchronize = async () => {
  if (!source.value) return;
  busy.value = true; error.value = ''; message.value = '';
  try {
    const result = await request(`${sourceUrl()}/sync`, { method: 'POST' });
    await loadSource();
    if (result.state === 'ready') emit('revisionReady', result.project_id, result.revision_id);
    message.value = `Fetched ${result.commit_sha.slice(0, 12)} · ${result.state}. Assessments unlock when indexing is ready.`;
  } catch (cause: any) { error.value = cause?.message || 'Git synchronization failed.'; }
  finally { busy.value = false; }
};
</script>

<template>
  <section class="kb-two" aria-labelledby="git-source-heading">
    <div class="kb-panel">
      <h2 id="git-source-heading" class="kb-panel-title"><GitBranch aria-hidden="true" />GitHub source</h2>
      <p class="kb-lead">Fetch a branch head into an immutable source revision. The read-only token lives in your OS credential store; the API never saves it.</p>
      <div class="kb-row3">
        <label class="kb-field">Project ID<input :value="projectId" readonly class="kb-input" placeholder="Choose a project in the header" title="Change the project in the header" /></label>
        <label class="kb-field">Repository<input v-model="repository" autocomplete="off" class="kb-input" placeholder="owner/repository" /></label>
        <label class="kb-field">Branch<input v-model="branch" autocomplete="off" class="kb-input" placeholder="main" /></label>
      </div>
      <div class="kb-btns">
        <button type="button" :disabled="busy || !projectId.trim()" class="kb-btn" @click="saveSource"><LoaderCircle v-if="busy" class="kb-spin" aria-hidden="true" /><Save v-else aria-hidden="true" />Save repository</button>
        <button type="button" :disabled="busy || !projectId.trim()" class="kb-btn" @click="loadSource"><RefreshCw aria-hidden="true" />Load settings</button>
      </div>
    </div>

    <div class="kb-panel">
      <template v-if="source">
        <div class="kb-status">
          <span class="kb-project-icon"><GitBranch aria-hidden="true" /></span>
          <div class="min-w-0">
            <p class="kb-status-name">{{ source.repository }} · {{ source.branch }}</p>
            <p class="kb-project-meta">Sync status: {{ source.status }}<template v-if="source.last_commit_sha"> · {{ source.last_commit_sha.slice(0, 12) }}</template></p>
          </div>
          <span class="kb-tag" :data-tone="credentialSaved ? 'ok' : 'warn'"><KeyRound aria-hidden="true" />{{ credentialSaved ? 'Token saved' : 'Token required' }}</span>
        </div>
        <p v-if="source.error_code" class="kb-rev-note" data-tone="warn"><CircleAlert aria-hidden="true" />The last sync needs attention ({{ source.error_code }}).</p>
        <label class="kb-field kb-field-spaced">Fine-grained GitHub token
          <span class="kb-inline">
            <input v-model="token" type="password" autocomplete="new-password" class="kb-input" placeholder="Read-only Contents access" />
            <button type="button" :disabled="busy || !token" class="kb-btn" @click="saveToken"><ShieldCheck aria-hidden="true" />Save token</button>
            <button v-if="credentialSaved" type="button" :disabled="busy" class="kb-btn" data-variant="danger" @click="removeToken"><Unplug aria-hidden="true" />Remove</button>
          </span>
        </label>
        <button type="button" :disabled="busy" class="kb-btn kb-btn-wide" data-variant="primary" @click="synchronize"><LoaderCircle v-if="busy" class="kb-spin" aria-hidden="true" /><RefreshCw v-else aria-hidden="true" />Fetch branch head and reconcile</button>
      </template>
      <template v-else>
        <h3 class="kb-panel-title kb-panel-title-sm">How syncing works</h3>
        <p class="kb-lead">Save a repository and branch to start. You can then fetch its latest commit whenever you like.</p>
      </template>

      <div class="kb-steps">
        <div class="kb-step"><b><Download aria-hidden="true" />1 · Fetch</b>The branch head becomes a new revision.</div>
        <div class="kb-step"><b><Cpu aria-hidden="true" />2 · Index</b>It's queued for indexing automatically.</div>
        <div class="kb-step"><b><ShieldCheck aria-hidden="true" />3 · Assess</b>Code quality can use it once indexing is ready.</div>
      </div>
      <p class="kb-note">Public repositories work without a token. Private ones need a fine-grained token with Contents: read. Push webhooks are optional notifications (set a webhook secret on the web server); sync always fetches the branch itself.</p>
    </div>
  </section>
</template>
