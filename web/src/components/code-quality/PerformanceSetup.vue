<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { LoaderCircle, Play, Square } from 'lucide-vue-next';
import { makeRequest } from '../../ui/makeRequest';
import { toast, toastMessage } from '../../composables/useToasts';
import TargetForm from './performance/TargetForm.vue';
import RoutePicker from './performance/RoutePicker.vue';
import RunResult from './performance/RunResult.vue';
import CompareRuns from './performance/CompareRuns.vue';
import { emptyLogin, emptyTarget, fillRoute, isLocalOrigin, peakUsers, perfRequest, projectPath, runSeconds, shortRevision, workloadLabel,
  type Job, type RouteSuggestion, type Run, type Selection, type Target } from './performance/performance';

const props = defineProps<{ projectId: string; revisionId: string }>();
const targets = ref<Target[]>([]);
const target = ref<Target>(emptyTarget());
const savedSnapshot = ref('');
const credentialSaved = ref(false);
const tokenInput = ref('');
const loginUsername = ref('');
const loginPassword = ref('');
const routes = ref<RouteSuggestion[]>([]);
const routesReason = ref('');
const routesWindow = ref<number | null>(null);
const routesLoading = ref(false);
const selections = ref<Selection[]>([]);
const history = ref<Run[]>([]);
const failedJobs = ref<Job[]>([]);
const job = ref<Job | null>(null);
const selectedRun = ref<Run | null>(null);
const historyFilter = ref({ target: '', revision: '' });
const baselineId = ref('');
const canOperate = ref(true);
const saving = ref(false);
const starting = ref(false);
const error = toastMessage('error');
const message = toastMessage('success');
let pollTimer: ReturnType<typeof setTimeout> | null = null;
let loadSequence = 0;

const snapshot = (value: Target) => JSON.stringify({ ...value, updated_at: undefined, unassigned: undefined });
const dirty = computed(() => !target.value.target_id || snapshot(target.value) !== savedSnapshot.value);
const running = computed(() => job.value?.state === 'running');
const filledSelections = computed(() => selections.value.map((item) => ({ ...item, path: fillRoute(item) })));
const missingParams = computed(() => filledSelections.value.filter((item) => item.path.includes('{')));
const runBlocker = computed(() => {
  if (!canOperate.value) return 'Viewers can read results; operators run benchmarks.';
  if (!props.revisionId) return 'Select an indexed project revision first.';
  if (!selections.value.length) return 'Select at least one route.';
  if (missingParams.value.length) return `Fill in the parameters of ${missingParams.value[0].template}.`;
  if (peakUsers(target.value) * selections.value.length > 100) return 'Virtual users × routes may not exceed 100.';
  if (target.value.auth_mode === 'bearer' && target.value.target_id && !credentialSaved.value) return 'Save the Bearer token first.';
  if (target.value.auth_mode === 'http_login' && (!loginUsername.value || !loginPassword.value)) return 'Enter the login username and password for this run.';
  return '';
});
const baselineKey = () => `devassistant.performance.baseline.${props.projectId}`;
const filteredHistory = computed(() => history.value.filter((run) => (!historyFilter.value.target || run.target_id === historyFilter.value.target)
  && (!historyFilter.value.revision || run.revision_id === historyFilter.value.revision)));
const historyRevisions = computed(() => [...new Map(history.value.filter((run) => run.revision_id).map((run) => [run.revision_id!, run])).values()]);

const loadRole = async () => {
  try {
    const response = await makeRequest('/api/auth/me');
    const data = await response.json();
    const roles: { project_id: string; role: string }[] = data.projects || [];
    const role = roles.find((item) => item.project_id === props.projectId || item.project_id === '*')?.role;
    canOperate.value = role === 'operator' || role === 'admin';
  } catch { canOperate.value = true; } // the server still enforces roles
};
const applyTarget = (value: Target | null) => {
  const next = value ? { ...emptyTarget(), ...value, login: { ...emptyLogin(), ...(value.login || {}) },
    stages: value.stages?.length ? value.stages.map((stage) => ({ ...stage })) : emptyTarget().stages, thresholds: { ...(value.thresholds || {}) },
    route_params: JSON.parse(JSON.stringify(value.route_params || {})) } : emptyTarget();
  target.value = next;
  savedSnapshot.value = value ? snapshot(next) : '';
  for (const selection of selections.value) selection.params = { ...(next.route_params[selection.template] || {}), ...selection.params };
  void loadCredentialStatus();
};
const loadTargets = async (selectId?: string) => {
  const data = await perfRequest<{ targets: Target[] }>(projectPath(props.projectId, '/targets'));
  targets.value = data.targets || [];
  const keep = selectId || target.value.target_id;
  applyTarget(targets.value.find((item) => item.target_id === keep) || targets.value.find((item) => !item.unassigned) || null);
};
const loadCredentialStatus = async () => {
  credentialSaved.value = false;
  if (!target.value.target_id || target.value.auth_mode !== 'bearer' || target.value.unassigned) return;
  try {
    const data = await perfRequest(projectPath(props.projectId, `/targets/${encodeURIComponent(target.value.target_id)}/credential`));
    credentialSaved.value = data.saved === true;
  } catch (cause: any) { error.value = cause?.message || 'Bearer credential status is unavailable.'; }
};
const loadRoutes = async () => {
  routes.value = []; routesReason.value = '';
  if (!props.revisionId) return;
  routesLoading.value = true;
  try {
    const data = await perfRequest(`/api/code-quality/projects/${encodeURIComponent(props.projectId)}/revisions/${encodeURIComponent(props.revisionId)}/performance/routes`);
    routes.value = data.routes || [];
    routesWindow.value = data.window_hours ?? null;
    if (!data.available) routesReason.value = data.reason || '';
  } catch (cause: any) { routesReason.value = cause?.message || 'Runtime routes are unavailable.'; }
  finally { routesLoading.value = false; }
};
const loadHistory = async () => {
  const data = await perfRequest<{ runs: Run[]; active: Job[]; failed: Job[] }>(projectPath(props.projectId, '/runs'));
  history.value = data.runs || [];
  failedJobs.value = data.failed || [];
  if (data.active?.length && !running.value) { job.value = data.active[0]; schedulePoll(); }
  if (selectedRun.value && !history.value.some((run) => run.run_id === selectedRun.value!.run_id)) selectedRun.value = null;
  if (!selectedRun.value && history.value.length) selectedRun.value = history.value[0];
};
const load = async () => {
  const sequence = ++loadSequence;
  stopPolling(); job.value = null; selectedRun.value = null; selections.value = []; error.value = ''; message.value = '';
  try { baselineId.value = localStorage.getItem(baselineKey()) || ''; } catch { baselineId.value = ''; }
  if (!props.projectId) return;
  try {
    await Promise.all([loadRole(), loadTargets(), loadRoutes(), loadHistory()]);
  } catch (cause: any) { if (sequence === loadSequence) error.value = cause?.message || 'Setup Performance could not be loaded.'; }
};

const targetPayload = () => ({ ...target.value, target_id: target.value.target_id || undefined,
  route_params: { ...target.value.route_params, ...Object.fromEntries(selections.value.filter((item) => Object.keys(item.params).length)
    .map((item) => [item.template, item.params])) },
  login: target.value.auth_mode === 'http_login' ? { ...target.value.login, csrf_path: target.value.login.csrf_path || null, check_path: target.value.login.check_path || null } : {} });
const saveTarget = async (quiet = false) => {
  saving.value = true; error.value = ''; if (!quiet) message.value = '';
  try {
    const data = await perfRequest<{ target: Target }>(projectPath(props.projectId, '/targets'), { method: 'POST', json: targetPayload() });
    await loadTargets(data.target.target_id);
    if (!quiet) message.value = 'Target saved.';
    return true;
  } catch (cause: any) { error.value = cause?.message || 'The target could not be saved.'; return false; }
  finally { saving.value = false; }
};
const newTarget = () => { applyTarget(null); message.value = ''; };
const selectTarget = (id: string) => { if (!id) newTarget(); else applyTarget(targets.value.find((item) => item.target_id === id) || null); };
const removeTarget = async () => {
  if (!target.value.target_id || !window.confirm(`Remove target ${target.value.name}? Its saved Bearer token is removed too; past runs stay.`)) return;
  saving.value = true; error.value = '';
  try {
    await perfRequest(projectPath(props.projectId, `/targets/${encodeURIComponent(target.value.target_id)}`), { method: 'DELETE' });
    target.value.target_id = '';
    await loadTargets();
    message.value = 'Target removed.';
  } catch (cause: any) { error.value = cause?.message || 'The target could not be removed.'; }
  finally { saving.value = false; }
};
const saveToken = async (token: string) => {
  saving.value = true; error.value = '';
  try {
    await perfRequest(projectPath(props.projectId, `/targets/${encodeURIComponent(target.value.target_id)}/credential`), { method: 'POST', json: { token } });
    tokenInput.value = ''; credentialSaved.value = true; message.value = 'Bearer token saved in the OS credential store.';
  } catch (cause: any) { error.value = cause?.message || 'The token could not be saved.'; }
  finally { saving.value = false; }
};

const stopPolling = () => { if (pollTimer) clearTimeout(pollTimer); pollTimer = null; };
const schedulePoll = () => { stopPolling(); pollTimer = setTimeout(() => void poll(), 2000); };
const poll = async () => {
  const current = job.value;
  if (!current || current.state !== 'running') return;
  try {
    const data = await perfRequest<Run | Job>(projectPath(props.projectId, `/runs/${encodeURIComponent(current.run_id)}`));
    if (job.value?.run_id !== current.run_id) return;
    if ('route_results' in data) {
      job.value = null;
      await loadHistory();
      selectedRun.value = history.value.find((run) => run.run_id === data.run_id) || (data as Run);
      message.value = (data as Run).complete ? 'Benchmark finished.' : 'Benchmark finished with partial results: see the notes above the table.';
      return;
    }
    job.value = data as Job;
    if (data.state === 'failed') { error.value = (data as Job).error || 'The benchmark failed.'; job.value = null; await loadHistory(); return; }
    schedulePoll();
  } catch { schedulePoll(); }
};
const startRun = async () => {
  if (runBlocker.value || starting.value) return;
  error.value = ''; message.value = '';
  if (!isLocalOrigin(target.value.origin)) {
    const load = target.value.executor === 'constant_arrival_rate'
      ? `about ${(Number(target.value.rate_per_second) * runSeconds(target.value) * selections.value.length).toLocaleString()} requests`
      : `${peakUsers(target.value) * selections.value.length} concurrent users for ${runSeconds(target.value)}s`;
    if (!window.confirm(`Send ${load} to ${target.value.origin}? Only benchmark systems you own or are allowed to test.`)) return;
  }
  starting.value = true;
  try {
    if (dirty.value && !(await saveTarget(true))) return;
    const body: Record<string, unknown> = { target_id: target.value.target_id, revision_id: props.revisionId,
      routes: selections.value.map((item) => ({ template: item.template, params: item.params })) };
    if (target.value.auth_mode === 'http_login') body.login_credentials = { username: loginUsername.value, password: loginPassword.value };
    job.value = await perfRequest<Job>(projectPath(props.projectId, '/runs'), { method: 'POST', json: body });
    loginPassword.value = '';
    schedulePoll();
  } catch (cause: any) {
    error.value = cause?.message || 'The benchmark could not be started.';
    if (cause?.status === 409 && cause.activeRunId) { await loadHistory(); }
  } finally { starting.value = false; }
};
const cancelRun = async () => {
  if (!job.value) return;
  try {
    await perfRequest(projectPath(props.projectId, `/runs/${encodeURIComponent(job.value.run_id)}`), { method: 'DELETE' });
    toast.info('Stopping the benchmark; its partial result will be saved.');
  } catch (cause: any) { error.value = cause?.message || 'The benchmark could not be cancelled.'; }
};
const toggleBaseline = (run: Run) => {
  baselineId.value = baselineId.value === run.run_id ? '' : run.run_id;
  try { baselineId.value ? localStorage.setItem(baselineKey(), baselineId.value) : localStorage.removeItem(baselineKey()); } catch { /* per-browser convenience only */ }
};
const runDeleted = async () => { selectedRun.value = null; await loadHistory(); };

watch(() => [props.projectId, props.revisionId], () => void load(), { immediate: true });
onBeforeUnmount(stopPolling);
</script>

<template>
  <section class="space-y-5" aria-label="Setup Performance">
    <div class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5 sm:p-6">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 class="text-xl font-semibold">Setup Performance</h2>
          <p class="mt-1 max-w-3xl text-sm leading-relaxed text-slate-400">Load-test selected GET routes of a running deployment with k6, then compare runs across code revisions. Results stay separate from the static Performance score.</p>
        </div>
        <label class="text-xs text-slate-400">Target
          <select :value="target.target_id" class="mt-1 block min-h-9 min-w-56 rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm" @change="selectTarget(($event.target as HTMLSelectElement).value)">
            <option value="">+ New target</option>
            <option v-for="item in targets" :key="item.target_id" :value="item.target_id">{{ item.name }}{{ item.unassigned ? ' (from before projects; save to use)' : '' }} · {{ item.auth_mode }}</option>
          </select>
        </label>
      </div>
      <p v-if="!canOperate" class="mt-4 rounded-lg border border-slate-700 p-3 text-sm text-slate-400">You have view access to this project: you can read and compare results, but only operators change targets or start benchmarks.</p>

      <div class="mt-5 grid gap-6 xl:grid-cols-[minmax(0,1.1fr)_minmax(20rem,0.9fr)]">
        <TargetForm v-model="target" v-model:token="tokenInput" v-model:username="loginUsername" v-model:password="loginPassword"
          :credential-saved="credentialSaved" :saving="saving" :dirty="dirty && !!target.target_id" :selected-route-count="selections.length" :can-operate="canOperate"
          @save="saveTarget()" @remove="removeTarget" @save-token="saveToken" />
        <div class="flex flex-col rounded-xl border border-slate-800 bg-slate-950/40 p-4">
          <RoutePicker v-model="selections" :routes="routes" :loading="routesLoading" :reason="routesReason" :window-hours="routesWindow" :remembered="target.route_params" :disabled="!canOperate || running" />
          <div class="mt-auto pt-4">
            <div v-if="running && job" class="space-y-2" role="status" aria-live="polite">
              <div class="flex items-center justify-between text-xs text-slate-300">
                <span class="inline-flex items-center gap-1.5"><LoaderCircle :size="13" class="motion-safe:animate-spin" />Running on {{ job.target_name }}{{ job.progress?.requests != null ? ` · ${job.progress.requests.toLocaleString()} requests · ${job.progress.requests_per_second} req/s` : '' }}</span>
                <span>{{ job.progress?.elapsed_seconds ?? 0 }}s / ~{{ Math.max(0, (job.progress?.expected_seconds ?? job.expected_seconds) - 30) }}s</span>
              </div>
              <div class="h-2 overflow-hidden rounded-full bg-slate-800"><div class="h-full bg-indigo-400 transition-all" :style="{ width: `${job.progress?.percent ?? 0}%` }" /></div>
              <button type="button" class="inline-flex min-h-9 w-full items-center justify-center gap-2 rounded-lg border border-rose-500/40 text-sm text-rose-200 hover:bg-rose-500/10" @click="cancelRun"><Square :size="14" />Stop and keep partial results</button>
            </div>
            <template v-else>
              <button type="button" :disabled="!!runBlocker || starting || saving" class="inline-flex min-h-10 w-full items-center justify-center gap-2 rounded-lg bg-indigo-500 px-4 text-sm font-semibold text-white hover:bg-indigo-400 disabled:cursor-not-allowed disabled:opacity-50" @click="startRun">
                <LoaderCircle v-if="starting" class="motion-safe:animate-spin" :size="16" /><Play v-else :size="16" />{{ dirty && target.target_id ? 'Save and run' : !target.target_id ? 'Create target and run' : 'Run benchmark' }}
              </button>
              <p class="mt-2 text-center text-xs" :class="runBlocker ? 'text-amber-200' : 'text-slate-500'">{{ runBlocker || `${selections.length} route${selections.length === 1 ? '' : 's'} · ${peakUsers(target) * selections.length} users at most · ~${runSeconds(target) + (target.warmup_seconds || 0)}s. You can leave this page while it runs.` }}</p>
            </template>
          </div>
        </div>
      </div>
    </div>

    <div class="grid gap-5 2xl:grid-cols-[minmax(0,1fr)_20rem]">
      <div class="min-w-0 space-y-5">
        <RunResult v-if="selectedRun" :project-id="projectId" :run="selectedRun" :production="routes" :baseline="baselineId === selectedRun.run_id"
          :can-operate="canOperate" @baseline="toggleBaseline(selectedRun)" @deleted="runDeleted" />
        <CompareRuns :project-id="projectId" :history="history" :baseline-id="baselineId" />
      </div>
      <aside class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5" aria-label="Benchmark history">
        <div class="flex items-center justify-between gap-2"><h3 class="font-semibold">History</h3><button type="button" class="text-xs text-indigo-200 hover:text-indigo-100" @click="loadHistory">Refresh</button></div>
        <div class="mt-3 grid gap-2">
          <select v-model="historyFilter.target" class="min-h-8 rounded-lg border border-slate-700 bg-slate-950 px-2 text-xs" aria-label="Filter by target"><option value="">All targets</option><option v-for="item in targets" :key="item.target_id" :value="item.target_id">{{ item.name }}</option></select>
          <select v-model="historyFilter.revision" class="min-h-8 rounded-lg border border-slate-700 bg-slate-950 px-2 text-xs" aria-label="Filter by revision"><option value="">All revisions</option><option v-for="run in historyRevisions" :key="run.revision_id!" :value="run.revision_id!">{{ shortRevision(run) }}</option></select>
        </div>
        <ul v-if="failedJobs.length" class="mt-3 space-y-1.5">
          <li v-for="item in failedJobs" :key="item.run_id" class="rounded-lg border border-rose-500/20 bg-rose-500/5 p-2 text-xs text-rose-200">Failed {{ new Date(item.started_at).toLocaleString() }}: {{ item.error }}</li>
        </ul>
        <div v-if="filteredHistory.length" class="mt-3 max-h-[36rem] space-y-2 overflow-y-auto pr-1">
          <button v-for="run in filteredHistory" :key="run.run_id" type="button" class="w-full rounded-xl border p-3 text-left transition" :class="selectedRun?.run_id === run.run_id ? 'border-indigo-400/60 bg-indigo-400/15 ring-1 ring-indigo-400/30' : 'border-slate-800 bg-slate-950/40 hover:border-slate-700'" @click="selectedRun = run">
            <span class="flex items-center justify-between gap-2 text-sm font-medium"><span class="truncate">{{ run.target_name }}</span>
              <span class="shrink-0 rounded-full px-1.5 py-0.5 text-[10px]" :class="run.complete ? 'bg-emerald-500/10 text-emerald-200' : 'bg-amber-500/10 text-amber-200'">{{ run.complete ? 'complete' : 'partial' }}</span></span>
            <span class="mt-1 block text-xs text-slate-300">rev {{ shortRevision(run) }} · {{ run.routes.length }} routes · {{ workloadLabel(run.workload) }}</span>
            <span class="mt-1 block text-[11px] text-slate-400">{{ new Date(run.created_at).toLocaleString() }}{{ run.run_id === baselineId ? ' · baseline' : '' }}</span>
          </button>
        </div>
        <p v-else class="mt-4 text-sm leading-relaxed text-slate-500">Finished runs appear here with their revision, target, routes and workload.</p>
      </aside>
    </div>
  </section>
</template>
