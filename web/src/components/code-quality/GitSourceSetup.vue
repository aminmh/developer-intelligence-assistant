<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { GitBranch, LoaderCircle, RefreshCw, Save, ShieldCheck, Unplug, KeyRound, Download, Cpu, CircleAlert, Search } from 'lucide-vue-next';
import { makeRequest } from '../../ui/makeRequest';
import { toastMessage } from '../../composables/useToasts';

type Source = { project_id: string; repository: string; remote_url?: string | null; branch: string; status: string; last_commit_sha?: string | null; last_revision_id?: string | null; error_code?: string | null; credential_id: string };
type BranchInfo = { name: string; commit: string; is_default: boolean };
type RemoteSummary = { repository: string; host: string; name: string; transport: 'https' | 'ssh'; url: string; default_branch: string | null;
  branch_count: number; tag_count: number; head_commit: string | null; branches: BranchInfo[]; truncated: boolean };
const props = defineProps<{ defaultProjectId?: string }>();
const projectId = ref('');
const repository = ref('');
const branch = ref('main');
const token = ref('');
const source = ref<Source | null>(null);
const credentialSaved = ref(false);
const inspectToken = ref('');
const summary = ref<RemoteSummary | null>(null);
const inspectedUrl = ref('');
const inspecting = ref(false);
const isSsh = computed(() => /^(ssh:\/\/|[\w.-]+@[\w.-]+:(?!\/\/))/.test(repository.value.trim()));
// A branch can be saved only from the list fetched for the URL that is currently typed.
const inspectionCurrent = computed(() => summary.value !== null && inspectedUrl.value === repository.value.trim());
const selectedCommit = computed(() => summary.value?.branches.find((item) => item.name === branch.value)?.commit || '');
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
    repository.value = source.value.remote_url || source.value.repository;
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
  inspectToken.value = '';
  summary.value = null;
  inspectedUrl.value = '';
  credentialSaved.value = false;
  message.value = '';
  error.value = '';
  if (next) await loadSource().catch((cause: any) => { error.value = cause?.message || 'Git source operation failed.'; });
}, { immediate: true });
const fetchRepository = async () => {
  const url = repository.value.trim();
  if (!url || !projectId.value.trim()) return;
  inspecting.value = true; error.value = ''; message.value = '';
  try {
    const data: RemoteSummary = await request(`${sourceUrl()}/inspect`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repository: url, ...(inspectToken.value && !isSsh.value ? { token: inspectToken.value } : {}) }) });
    summary.value = data; inspectedUrl.value = url;
    const names = new Set(data.branches.map((item) => item.name));
    if (!names.has(branch.value)) branch.value = data.default_branch && names.has(data.default_branch) ? data.default_branch : (data.branches[0]?.name || '');
    if (!data.branches.length) error.value = 'This repository has no branches to fetch.';
  } catch (cause: any) { summary.value = null; inspectedUrl.value = ''; error.value = cause?.message || 'The repository could not be inspected.'; }
  finally { inspecting.value = false; }
};
// Typing another project ID drops the previous project's source and loads the new one's, if it has any.
const projectChanged = async () => {
  projectId.value = projectId.value.trim();
  source.value = null; credentialSaved.value = false; message.value = ''; error.value = '';
  if (projectId.value) await loadSource().catch((cause: any) => { error.value = cause?.message || 'Git source operation failed.'; });
};
const saveSource = async () => {
  busy.value = true; error.value = ''; message.value = '';
  try {
    const data = await request(sourceUrl(), { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repository: repository.value.trim(), branch: branch.value.trim() }) });
    source.value = data.source;
    const status = await request(`${sourceUrl()}/credential`);
    credentialSaved.value = status.saved === true;
    message.value = 'Repository and branch saved.';
    inspectToken.value = '';
  } catch (cause: any) { error.value = cause?.message || 'Repository settings could not be saved.'; }
  finally { busy.value = false; }
};
const saveToken = async () => {
  if (!token.value || !source.value) return;
  busy.value = true; error.value = ''; message.value = '';
  try {
    await request(`${sourceUrl()}/credential`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: token.value }) });
    token.value = ''; credentialSaved.value = true; message.value = 'Token saved to the OS credential store.';
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
  <section class="kb-two git-source-grid" aria-labelledby="git-source-heading">
    <div class="kb-panel">
      <h2 id="git-source-heading" class="kb-panel-title"><GitBranch aria-hidden="true" />Git source</h2>
      <p class="kb-lead">Enter an HTTPS or SSH repository URL, fetch its branches, then choose the one to clone into an immutable source revision. Tokens live in your OS credential store; the API never saves them.</p>
      <label class="kb-field">Project ID<input v-model="projectId" autocomplete="off" spellcheck="false" class="kb-input" placeholder="Project ID, or choose a project in the header" title="Defaults to the project chosen in the header; you can type another ID" @change="projectChanged" /></label>
      <label class="kb-field">Repository URL
        <span class="kb-inline">
          <input v-model="repository" autocomplete="off" spellcheck="false" class="kb-input git-url" placeholder="https://github.com/owner/repo.git or git@github.com:owner/repo.git" @keydown.enter.prevent="fetchRepository" />
          <button type="button" :disabled="inspecting || busy || !projectId.trim() || !repository.trim()" class="kb-btn" data-variant="primary" @click="fetchRepository"><LoaderCircle v-if="inspecting" class="kb-spin" aria-hidden="true" /><Search v-else aria-hidden="true" />Fetch</button>
        </span>
      </label>
      <label v-if="!isSsh" class="kb-field">Access token <em>(private HTTPS repositories, optional)</em><input v-model="inspectToken" type="password" autocomplete="new-password" class="kb-input" placeholder="Used once to list branches" /></label>
      <p v-else class="kb-hint"><KeyRound aria-hidden="true" />SSH uses the key loaded in your forwarded ssh-agent. Nothing is stored.</p>

      <div v-if="summary && inspectionCurrent" class="kb-status" aria-live="polite">
        <span class="kb-project-icon"><GitBranch aria-hidden="true" /></span>
        <div class="min-w-0">
          <p class="kb-status-name">{{ summary.name }} · {{ summary.host }}</p>
          <p class="kb-project-meta">{{ summary.transport.toUpperCase() }} · {{ summary.branch_count }} {{ summary.branch_count === 1 ? 'branch' : 'branches' }} · {{ summary.tag_count }} {{ summary.tag_count === 1 ? 'tag' : 'tags' }}<template v-if="summary.default_branch"> · default {{ summary.default_branch }}</template><template v-if="summary.head_commit"> · HEAD {{ summary.head_commit.slice(0, 12) }}</template></p>
        </div>
      </div>
      <label v-if="summary && inspectionCurrent && summary.branches.length" class="kb-field git-branch">Branch to clone
        <select v-model="branch" class="kb-input">
          <option v-for="item in summary.branches" :key="item.name" :value="item.name">{{ item.name }}{{ item.is_default ? ' (default)' : '' }} · {{ item.commit.slice(0, 7) }}</option>
        </select>
      </label>
      <p v-if="summary?.truncated && inspectionCurrent" class="kb-note">Showing the first {{ summary.branches.length }} of {{ summary.branch_count }} branches.</p>
      <p v-else-if="summary && !inspectionCurrent" class="kb-note">The URL changed. Fetch again to list its branches.</p>
      <div class="kb-btns git-actions">
        <button type="button" :disabled="busy || inspecting || !projectId.trim() || !inspectionCurrent || !branch" class="kb-btn" data-variant="primary" @click="saveSource"><LoaderCircle v-if="busy" class="kb-spin" aria-hidden="true" /><Save v-else aria-hidden="true" />Save repository<template v-if="inspectionCurrent && branch"> · {{ branch }}<template v-if="selectedCommit"> @ {{ selectedCommit.slice(0, 7) }}</template></template></button>
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
          <span v-if="!source.remote_url?.startsWith('ssh://')" class="kb-tag" :data-tone="credentialSaved ? 'ok' : 'warn'"><KeyRound aria-hidden="true" />{{ credentialSaved ? 'Token saved' : 'Token required' }}</span>
        </div>
        <p v-if="source.error_code" class="kb-rev-note" data-tone="warn"><CircleAlert aria-hidden="true" />The last sync needs attention ({{ source.error_code }}).</p>
        <label v-if="source.remote_url?.startsWith('ssh://')" class="kb-field kb-field-spaced">Authentication<span class="kb-note">SSH · forwarded ssh-agent</span></label>
        <label v-else class="kb-field kb-field-spaced">Access token (fine-grained, read-only Contents)
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
        <p class="kb-lead">Fetch a repository URL, pick a branch and save it to start. You can then fetch its latest commit whenever you like.</p>
      </template>

      <div class="kb-steps">
        <div class="kb-step"><b><Download aria-hidden="true" />1 · Fetch</b>The branch head becomes a new revision.</div>
        <div class="kb-step"><b><Cpu aria-hidden="true" />2 · Index</b>It's queued for indexing automatically.</div>
        <div class="kb-step"><b><ShieldCheck aria-hidden="true" />3 · Assess</b>Code quality can use it once indexing is ready.</div>
      </div>
      <p class="kb-note">Public HTTPS repositories work without a token. Private ones need an access token with read access, or use an SSH URL with a key in your ssh-agent. Push webhooks are optional notifications (set a webhook secret on the web server); sync always fetches the branch itself.</p>
    </div>
  </section>
</template>

<style scoped>
.git-source-grid { grid-template-columns: repeat(auto-fit, minmax(min(30rem, 100%), 1fr)); }
.git-url { min-width: 14rem; }
.kb-hint { display: flex; align-items: center; gap: 0.5rem; margin: 0 0 0.75rem; font-size: 0.75rem; line-height: 1.5; color: var(--shell-text-3); }
.kb-hint svg { width: 0.875rem; height: 0.875rem; flex: none; }
.kb-status { margin: 0.25rem 0 1rem; }
.git-branch { margin-bottom: 1rem; }
.git-actions { margin-top: 0.25rem; padding-top: 1rem; border-top: 1px solid var(--shell-line); }
</style>
