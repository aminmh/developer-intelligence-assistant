<script setup lang="ts">
import { ref, watch } from 'vue';
import { GitBranch, LoaderCircle, RefreshCw, Save, ShieldCheck, Unplug } from 'lucide-vue-next';
import { makeRequest } from '../../ui/makeRequest';

type Source = { project_id: string; repository: string; branch: string; status: string; last_commit_sha?: string | null; last_revision_id?: string | null; error_code?: string | null; credential_id: string };
const props = defineProps<{ defaultProjectId?: string }>();
const projectId = ref('');
const repository = ref('');
const branch = ref('main');
const token = ref('');
const source = ref<Source | null>(null);
const credentialSaved = ref(false);
const busy = ref(false);
const message = ref('');
const error = ref('');
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
watch(() => props.defaultProjectId, async (value, previous) => {
  if (!value || (projectId.value && projectId.value !== previous)) return;
  projectId.value = value;
  await loadSource();
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
  <section class="space-y-5 rounded-2xl border border-slate-800 bg-slate-950/35 p-5" aria-labelledby="git-source-heading">
    <div class="flex items-start gap-3">
      <GitBranch class="mt-1 text-indigo-300" :size="19" />
      <div><h2 id="git-source-heading" class="text-base font-semibold text-slate-100">GitHub source synchronization</h2>
        <p class="mt-1 max-w-3xl text-sm leading-6 text-slate-400">Fetch a branch head into an immutable source revision. GitHub access uses a read-only token stored in the desktop OS credential store. The token is never saved by the API.</p></div>
    </div>
    <div class="grid gap-3 md:grid-cols-3">
      <label class="space-y-1 text-xs text-slate-400">Project ID<input v-model="projectId" autocomplete="off" class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100" placeholder="pizza" /></label>
      <label class="space-y-1 text-xs text-slate-400">GitHub repository<input v-model="repository" autocomplete="off" class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100" placeholder="owner/repository" /></label>
      <label class="space-y-1 text-xs text-slate-400">Branch<input v-model="branch" autocomplete="off" class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100" placeholder="main" /></label>
    </div>
    <div class="flex flex-wrap gap-2">
      <button type="button" :disabled="busy || !projectId.trim()" class="inline-flex min-h-10 items-center gap-2 rounded-lg border border-indigo-400/30 bg-indigo-500/10 px-4 text-sm text-indigo-100 disabled:opacity-50" @click="saveSource"><LoaderCircle v-if="busy" class="motion-safe:animate-spin" :size="15" /><Save v-else :size="15" />Save repository</button>
      <button type="button" :disabled="busy || !projectId.trim()" class="inline-flex min-h-10 items-center gap-2 rounded-lg border border-slate-700 px-4 text-sm text-slate-200 disabled:opacity-50" @click="loadSource"><RefreshCw :size="15" />Load settings</button>
    </div>
    <div v-if="source" class="space-y-3 rounded-xl border border-slate-800 bg-slate-900/40 p-4">
      <div class="flex flex-wrap items-center justify-between gap-2"><div><p class="text-sm font-medium text-slate-200">{{ source.repository }} · {{ source.branch }}</p><p class="mt-1 text-xs text-slate-500">Sync status: {{ source.status }}<template v-if="source.last_commit_sha"> · {{ source.last_commit_sha.slice(0, 12) }}</template></p><p v-if="source.error_code" class="mt-1 text-xs text-amber-300">Last sync needs attention ({{ source.error_code }}).</p></div><span class="rounded-full border px-2.5 py-1 text-xs" :class="credentialSaved ? 'border-emerald-400/30 text-emerald-200' : 'border-amber-400/30 text-amber-200'">{{ credentialSaved ? 'Token saved' : 'Token required' }}</span></div>
      <div class="flex flex-wrap items-end gap-2"><label class="min-w-64 flex-1 space-y-1 text-xs text-slate-400">Fine-grained GitHub token<input v-model="token" type="password" autocomplete="new-password" class="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100" placeholder="Read-only Contents access" /></label><button type="button" :disabled="busy || !token" class="inline-flex min-h-10 items-center gap-2 rounded-lg border border-slate-700 px-4 text-sm text-slate-200 disabled:opacity-50" @click="saveToken"><ShieldCheck :size="15" />Save token securely</button><button v-if="credentialSaved" type="button" :disabled="busy" class="inline-flex min-h-10 items-center gap-2 rounded-lg border border-rose-500/30 px-4 text-sm text-rose-200 disabled:opacity-50" @click="removeToken"><Unplug :size="15" />Remove token</button></div>
      <button type="button" :disabled="busy" class="inline-flex min-h-10 items-center gap-2 rounded-lg bg-indigo-500 px-4 text-sm font-medium text-white disabled:opacity-50" @click="synchronize"><LoaderCircle v-if="busy" class="motion-safe:animate-spin" :size="15" /><RefreshCw v-else :size="15" />Fetch branch head and reconcile</button>
      <p class="text-xs leading-5 text-slate-500">The API queues a fetched snapshot for indexing. Assessments can use it after indexing reaches ready. Public repositories work without a token; private repositories need a GitHub fine-grained personal access token with Contents: read permission.</p>
    </div>
    <p v-if="message" role="status" class="text-sm text-emerald-200">{{ message }}</p>
    <p v-if="error" role="alert" class="text-sm text-rose-200">{{ error }}</p>
    <p class="text-xs leading-5 text-slate-500">GitHub push webhooks are optional notifications. Configure a webhook secret in the Web server environment; synchronization always fetches the branch itself before accepting a revision.</p>
  </section>
</template>
