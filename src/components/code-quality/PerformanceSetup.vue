<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { AlertCircle, CheckCircle2, LoaderCircle, Play, Save, Trash2 } from 'lucide-vue-next';
import { makeRequest } from '../../ui/makeRequest';

type Target = { target_id: string; origin: string; virtual_users: number; duration_seconds: number; auth_mode: 'public' | 'bearer' | 'http_login'; login: LoginConfig };
type LoginConfig = { login_path: string; username_field: string; password_field: string; csrf_path: string | null; csrf_cookie_name: string; csrf_field_name: string };
type Route = { method: 'GET'; route: string; samples: number };
type RouteResult = { path: string; count: number; throughput_rps: number; errors: number; network_errors?: number; error_rate: number; latency_ms: Record<string, number> };
type Run = { run_id: string; target_origin: string; routes: string[]; auth_mode: string; workload: { virtual_users: number; duration_seconds: number }; runner_version: string; complete: boolean; route_results: RouteResult[]; created_at: string };

const props = defineProps<{ projectId: string; revisionId: string }>();
const targets = ref<Target[]>([]);
const selectedTargetId = ref('');
const routes = ref<Route[]>([]);
const selectedRoutes = ref<string[]>([]);
const history = ref<Run[]>([]);
const selectedRun = ref<Run | null>(null);
const compareLeft = ref('');
const compareRight = ref('');
const comparison = ref<{ equivalent: boolean; differences: string[]; runs: [Run, Run] } | null>(null);
const credentialSaved = ref(false);
const tokenInput = ref('');
const loginUsername = ref('');
const loginPassword = ref('');
const target = ref<Target>({ target_id: '', origin: 'http://host.docker.internal:80', virtual_users: 1,
  duration_seconds: 30, auth_mode: 'public', login: { login_path: '/login', username_field: 'email',
    password_field: 'password', csrf_path: '', csrf_cookie_name: 'XSRF-TOKEN', csrf_field_name: '_token' } });
const loading = ref(false);
const saving = ref(false);
const running = ref(false);
const error = ref('');
const message = ref('');
const selectedTarget = computed(() => targets.value.find((item) => item.target_id === selectedTargetId.value) || null);
const selectedRouteCount = computed(() => selectedRoutes.value.length);

const request = async (path: string, init?: RequestInit) => {
  const response = await makeRequest(path, init);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || 'Performance setup request failed.');
  return body;
};
const targetPayload = () => ({ origin: target.value.origin, virtual_users: Number(target.value.virtual_users),
  duration_seconds: Number(target.value.duration_seconds), auth_mode: target.value.auth_mode,
  login: target.value.auth_mode === 'http_login' ? { ...target.value.login, csrf_path: target.value.login.csrf_path || null } : {} });

const loadTargets = async () => {
  try {
    const data = await request('/api/code-quality/performance/targets');
    targets.value = data.targets || [];
    if (selectedTargetId.value && !targets.value.some((item: Target) => item.target_id === selectedTargetId.value)) selectedTargetId.value = '';
    if (selectedTargetId.value) await loadCredentialStatus();
  } catch (cause: any) { error.value = cause?.message || 'Performance targets are unavailable.'; }
};
const loadCredentialStatus = async () => {
  const targetId = selectedTargetId.value;
  credentialSaved.value = false;
  if (!targetId || selectedTarget.value?.auth_mode !== 'bearer') return;
  try {
    const data = await request(`/api/code-quality/performance/targets/${encodeURIComponent(targetId)}/credential`);
    credentialSaved.value = data.saved === true;
  } catch (cause: any) { error.value = cause?.message || 'Bearer credential status is unavailable.'; }
};
const loadRoutes = async () => {
  routes.value = [];
  selectedRoutes.value = [];
  if (!props.projectId || !props.revisionId) return;
  try {
    const data = await request(`/api/code-quality/projects/${encodeURIComponent(props.projectId)}/revisions/${encodeURIComponent(props.revisionId)}/performance/routes`);
    routes.value = data.routes || [];
    if (!data.available && data.reason) message.value = `${data.reason} Route suggestions need Runtime request history.`;
  } catch (cause: any) { error.value = cause?.message || 'Sampled Runtime routes are unavailable.'; }
};
const loadHistory = async () => {
  if (!props.projectId) { history.value = []; return; }
  try {
    const data = await request(`/api/code-quality/projects/${encodeURIComponent(props.projectId)}/performance/runs`);
    history.value = data.runs || [];
    if (history.value.length && !compareLeft.value) compareLeft.value = history.value[0].run_id;
    if (history.value.length > 1 && !compareRight.value) compareRight.value = history.value[1].run_id;
  } catch (cause: any) { error.value = cause?.message || 'Performance history is unavailable.'; }
};
const saveTarget = async () => {
  saving.value = true; error.value = ''; message.value = '';
  try {
    const data = await request('/api/code-quality/performance/targets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(targetPayload()) });
    target.value = { ...target.value, ...data.target };
    selectedTargetId.value = data.target.target_id;
    await loadTargets();
    message.value = 'Performance target settings saved.';
  } catch (cause: any) { error.value = cause?.message || 'Performance target could not be saved.'; }
  finally { saving.value = false; }
};
const selectTarget = (id: string) => {
  selectedTargetId.value = id;
  const match = targets.value.find((item) => item.target_id === id);
  if (match) target.value = { ...match, login: { ...target.value.login, ...(match.login || {}) } };
  loadCredentialStatus();
};
const saveBearer = async () => {
  if (!selectedTargetId.value || !tokenInput.value) return;
  saving.value = true; error.value = ''; message.value = '';
  try {
    await request(`/api/code-quality/performance/targets/${encodeURIComponent(selectedTargetId.value)}/credential`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: tokenInput.value }),
    });
    tokenInput.value = '';
    credentialSaved.value = true;
    message.value = 'Bearer token saved in the OS credential store for this target.';
  } catch (cause: any) { error.value = cause?.message || 'Bearer token could not be saved.'; }
  finally { saving.value = false; }
};
const removeTarget = async () => {
  if (!selectedTargetId.value) return;
  saving.value = true; error.value = '';
  try {
    await request(`/api/code-quality/performance/targets/${encodeURIComponent(selectedTargetId.value)}`, { method: 'DELETE' });
    selectedTargetId.value = ''; credentialSaved.value = false;
    await loadTargets();
    message.value = 'Target and its saved Bearer token were removed.';
  } catch (cause: any) { error.value = cause?.message || 'Performance target could not be removed.'; }
  finally { saving.value = false; }
};
const runBenchmark = async () => {
  if (!selectedTargetId.value || !selectedRoutes.value.length) return;
  running.value = true; error.value = ''; message.value = ''; selectedRun.value = null;
  try {
    const body: Record<string, unknown> = { target_id: selectedTargetId.value, routes: selectedRoutes.value };
    if (target.value.auth_mode === 'http_login') body.login_credentials = { username: loginUsername.value, password: loginPassword.value };
    const result = await request(`/api/code-quality/projects/${encodeURIComponent(props.projectId)}/performance/runs`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    selectedRun.value = result;
    loginPassword.value = '';
    message.value = 'Benchmark finished. Results were saved separately from static assessment scores.';
    await loadHistory();
  } catch (cause: any) { error.value = cause?.message || 'Performance benchmark failed. Check the target and retry.'; }
  finally { running.value = false; }
};
const compareRuns = async () => {
  comparison.value = null;
  if (!compareLeft.value || !compareRight.value || compareLeft.value === compareRight.value) return;
  try {
    const data = await request(`/api/code-quality/projects/${encodeURIComponent(props.projectId)}/performance/compare`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ left_run_id: compareLeft.value, right_run_id: compareRight.value }),
    });
    comparison.value = { ...data.comparison, runs: data.runs };
  } catch (cause: any) { error.value = cause?.message || 'Selected benchmark runs could not be compared.'; }
};
const openRun = async (id: string) => {
  try { selectedRun.value = await request(`/api/code-quality/projects/${encodeURIComponent(props.projectId)}/performance/runs/${encodeURIComponent(id)}`); }
  catch (cause: any) { error.value = cause?.message || 'Benchmark details could not be loaded.'; }
};
const load = async () => {
  loading.value = true; error.value = ''; message.value = '';
  await Promise.all([loadTargets(), loadRoutes(), loadHistory()]);
  loading.value = false;
};
const fmt = (value?: number) => Number.isFinite(value) ? `${Number(value).toFixed(1)} ms` : '—';
onMounted(load);
watch(() => [props.projectId, props.revisionId], load);
</script>

<template>
  <section class="space-y-5" aria-label="Setup Performance">
    <div class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5 sm:p-6">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div><h2 class="text-xl font-semibold">Setup Performance</h2><p class="mt-1 max-w-3xl text-sm leading-relaxed text-slate-400">Run a bounded live HTTP benchmark against explicitly selected GET routes from sampled Runtime history. These measurements stay separate from the static Performance score.</p></div>
        <span class="rounded-full border border-indigo-400/20 bg-indigo-400/5 px-3 py-1 text-xs text-indigo-200">Pinned k6 runner</span>
      </div>
      <div v-if="error" role="alert" class="mt-4 flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-200"><AlertCircle :size="16" class="mt-0.5 shrink-0" /><span>{{ error }}</span></div>
      <div v-if="message" role="status" class="mt-4 flex items-start gap-2 rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-3 text-sm text-emerald-200"><CheckCircle2 :size="16" class="mt-0.5 shrink-0" /><span>{{ message }}</span></div>
      <div class="mt-5 grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(18rem,0.8fr)]">
        <div class="space-y-4">
          <label class="block text-sm font-medium text-slate-300">Saved target
            <select :value="selectedTargetId" class="mt-1.5 block min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm" @change="selectTarget(($event.target as HTMLSelectElement).value)"><option value="">New target</option><option v-for="item in targets" :key="item.target_id" :value="item.target_id">{{ item.origin }} · {{ item.auth_mode }}</option></select>
          </label>
          <label class="block text-sm font-medium text-slate-300">Target origin
            <input v-model="target.origin" type="url" required placeholder="http://host.docker.internal:8080" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm text-slate-100" />
            <span class="mt-1 block text-xs text-slate-500">Local Docker default: host.docker.internal. Use the port published by the target application.</span>
          </label>
          <fieldset><legend class="text-sm font-medium text-slate-300">Authentication</legend>
            <div class="mt-2 grid gap-2 sm:grid-cols-3">
              <label v-for="mode in [{ id: 'public', name: 'Public' }, { id: 'bearer', name: 'Bearer token' }, { id: 'http_login', name: 'HTTP login' }]" :key="mode.id" class="flex min-h-10 items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/60 px-3 text-sm text-slate-300"><input v-model="target.auth_mode" type="radio" :value="mode.id" />{{ mode.name }}</label>
            </div>
          </fieldset>
          <div v-if="target.auth_mode === 'bearer'" class="space-y-2 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
            <p class="text-sm text-slate-300">{{ credentialSaved ? 'A target scoped token is saved in the OS credential store.' : 'Save the token once in the OS credential store.' }}</p>
            <label class="block text-xs text-slate-500">{{ credentialSaved ? 'Replace Bearer token' : 'Bearer token' }}
              <input v-model="tokenInput" type="password" autocomplete="new-password" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm text-slate-100" />
            </label>
            <button type="button" :disabled="saving || !tokenInput" class="inline-flex min-h-9 items-center gap-2 rounded-lg border border-slate-700 px-3 text-sm disabled:opacity-50" @click="saveBearer"><Save :size="14" />Save securely</button>
          </div>
          <div v-if="target.auth_mode === 'http_login'" class="grid gap-3 rounded-lg border border-slate-800 bg-slate-950/40 p-3 sm:grid-cols-2">
            <label class="text-xs text-slate-400">Login path<input v-model="target.login.login_path" class="mt-1 block min-h-9 w-full rounded border border-slate-700 bg-slate-950 px-2 text-sm text-slate-100" /></label>
            <label class="text-xs text-slate-400">CSRF bootstrap path (optional)<input v-model="target.login.csrf_path" class="mt-1 block min-h-9 w-full rounded border border-slate-700 bg-slate-950 px-2 text-sm text-slate-100" /></label>
            <label class="text-xs text-slate-400">Username field<input v-model="target.login.username_field" class="mt-1 block min-h-9 w-full rounded border border-slate-700 bg-slate-950 px-2 text-sm text-slate-100" /></label>
            <label class="text-xs text-slate-400">Password field<input v-model="target.login.password_field" class="mt-1 block min-h-9 w-full rounded border border-slate-700 bg-slate-950 px-2 text-sm text-slate-100" /></label>
            <label class="text-xs text-slate-400 sm:col-span-2">CSRF cookie name<input v-model="target.login.csrf_cookie_name" class="mt-1 block min-h-9 w-full rounded border border-slate-700 bg-slate-950 px-2 text-sm text-slate-100" /></label>
            <label class="text-xs text-slate-400 sm:col-span-2">CSRF form field<input v-model="target.login.csrf_field_name" class="mt-1 block min-h-9 w-full rounded border border-slate-700 bg-slate-950 px-2 text-sm text-slate-100" /></label>
            <label class="text-xs text-slate-400">Run username<input v-model="loginUsername" autocomplete="username" class="mt-1 block min-h-9 w-full rounded border border-slate-700 bg-slate-950 px-2 text-sm text-slate-100" /></label>
            <label class="text-xs text-slate-400">Run password<input v-model="loginPassword" type="password" autocomplete="current-password" class="mt-1 block min-h-9 w-full rounded border border-slate-700 bg-slate-950 px-2 text-sm text-slate-100" /></label>
            <p class="text-xs leading-relaxed text-slate-500 sm:col-span-2">Login and optional CSRF bootstrap are repeated once per virtual user and excluded from route timings. Run credentials are held for this request only.</p>
          </div>
          <div class="grid gap-3 sm:grid-cols-2">
            <label class="text-sm text-slate-300">Virtual users <input v-model.number="target.virtual_users" type="number" min="1" max="50" class="mt-1.5 block min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3" /></label>
            <label class="text-sm text-slate-300">Duration (seconds) <input v-model.number="target.duration_seconds" type="number" min="5" max="300" class="mt-1.5 block min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3" /></label>
          </div>
          <div class="flex flex-wrap gap-2">
            <button type="button" :disabled="saving" class="inline-flex min-h-10 items-center gap-2 rounded-lg border border-slate-700 px-4 text-sm font-medium disabled:opacity-50" @click="saveTarget"><Save :size="15" />{{ saving ? 'Saving…' : 'Save target settings' }}</button>
            <button v-if="selectedTargetId" type="button" :disabled="saving" class="inline-flex min-h-10 items-center gap-2 rounded-lg border border-rose-500/30 px-3 text-sm text-rose-200 disabled:opacity-50" @click="removeTarget"><Trash2 :size="14" />Remove target</button>
          </div>
        </div>
        <div class="rounded-xl border border-slate-800 bg-slate-950/40 p-4">
          <div class="flex items-start justify-between gap-3"><div><h3 class="font-semibold">Sampled GET routes</h3><p class="mt-1 text-xs leading-relaxed text-slate-500">Select routes from this indexed revision's Runtime history. They are not invoked until you start a benchmark.</p></div><span class="text-xs text-slate-500">{{ selectedRouteCount }} selected</span></div>
          <div v-if="loading" class="mt-4 flex items-center gap-2 text-sm text-slate-400"><LoaderCircle class="motion-safe:animate-spin" :size="16" />Loading setup…</div>
          <div v-else-if="routes.length" class="mt-4 max-h-72 space-y-2 overflow-auto pr-1">
            <label v-for="item in routes" :key="item.route" class="flex min-h-12 cursor-pointer items-center gap-3 rounded-xl border px-3 py-3 text-left transition-colors" :class="selectedRoutes.includes(item.route) ? 'border-emerald-400/40 bg-emerald-500/5' : 'border-slate-700 bg-slate-900/30 hover:bg-slate-800/60'"><input v-model="selectedRoutes" type="checkbox" :value="item.route" class="h-4 w-4 shrink-0 accent-emerald-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400" /><span class="min-w-0 flex-1"><span class="block break-all font-mono text-xs text-slate-200">GET {{ item.route }}</span><span class="mt-0.5 block text-[10px] text-slate-500">{{ item.samples }} recorded samples</span></span><span class="shrink-0 font-mono text-[10px]" :class="selectedRoutes.includes(item.route) ? 'text-emerald-300' : 'text-slate-500'">{{ selectedRoutes.includes(item.route) ? 'On' : 'Off' }}</span></label>
          </div>
          <div v-else class="mt-4 rounded-lg border border-dashed border-slate-700 p-4 text-sm text-slate-400">No sampled GET routes are available. Run the project and collect Runtime telemetry, then retry.</div>
          <button type="button" :disabled="running || saving || !selectedTargetId || !selectedRouteCount || (target.auth_mode === 'bearer' && !credentialSaved) || (target.auth_mode === 'http_login' && (!loginUsername || !loginPassword))" class="mt-4 inline-flex min-h-10 w-full items-center justify-center gap-2 rounded-lg bg-indigo-500 px-4 text-sm font-semibold text-white hover:bg-indigo-400 disabled:cursor-not-allowed disabled:opacity-50" @click="runBenchmark"><LoaderCircle v-if="running" class="motion-safe:animate-spin" :size="16" /><Play v-else :size="16" />{{ running ? 'Running benchmark…' : 'Run selected routes' }}</button>
          <p v-if="running" role="status" aria-live="polite" class="mt-2 text-center text-xs text-slate-400">{{ target.virtual_users }} virtual users · up to {{ target.duration_seconds }} seconds. Login time is excluded from route results.</p>
        </div>
      </div>
    </div>

    <section class="grid gap-5 xl:grid-cols-[minmax(0,1fr)_20rem]">
      <div class="space-y-5">
        <section v-if="selectedRun" class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5 sm:p-6">
          <div class="flex flex-wrap items-start justify-between gap-3"><div><h3 class="text-lg font-semibold">Live benchmark results</h3><p class="mt-1 text-xs text-slate-500">{{ selectedRun.target_origin }} · {{ selectedRun.workload.virtual_users }} VUs · {{ selectedRun.workload.duration_seconds }} seconds · {{ selectedRun.runner_version }}</p></div><span class="rounded-full border border-emerald-500/20 bg-emerald-500/5 px-2.5 py-1 text-xs text-emerald-200">{{ selectedRun.complete ? 'Complete' : 'Partial' }}</span></div>
          <div class="mt-4 overflow-x-auto rounded-xl border border-slate-800"><table class="w-full min-w-[42rem] text-left text-sm"><thead class="bg-slate-950/60 text-xs text-slate-400"><tr><th class="px-3 py-2.5">GET route</th><th class="px-3 py-2.5">Requests/s</th><th class="px-3 py-2.5">Errors</th><th class="px-3 py-2.5">p50</th><th class="px-3 py-2.5">p90</th><th class="px-3 py-2.5">p95</th><th class="px-3 py-2.5">p99</th></tr></thead><tbody class="divide-y divide-slate-800"><tr v-for="item in selectedRun.route_results" :key="item.path"><td class="max-w-72 break-all px-3 py-3 font-mono text-xs">{{ item.path }}</td><td class="px-3 py-3">{{ item.throughput_rps.toFixed(1) }}</td><td class="px-3 py-3" :class="item.errors ? 'text-rose-300' : 'text-emerald-300'">{{ item.errors }} · {{ (item.error_rate * 100).toFixed(1) }}%</td><td class="px-3 py-3">{{ fmt(item.latency_ms?.med) }}</td><td class="px-3 py-3">{{ fmt(item.latency_ms?.['p(90)']) }}</td><td class="px-3 py-3">{{ fmt(item.latency_ms?.['p(95)']) }}</td><td class="px-3 py-3">{{ fmt(item.latency_ms?.['p(99)']) }}</td></tr></tbody></table></div>
        </section>
        <section class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5 sm:p-6">
          <h3 class="text-lg font-semibold">Compare benchmark runs</h3>
          <div class="mt-3 grid gap-3 sm:grid-cols-2"><label class="text-xs text-slate-400">Earlier run<select v-model="compareLeft" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm"><option value="">Select a run</option><option v-for="item in history" :key="item.run_id" :value="item.run_id">{{ new Date(item.created_at).toLocaleString() }} · {{ item.target_origin }}</option></select></label><label class="text-xs text-slate-400">Later run<select v-model="compareRight" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm"><option value="">Select a run</option><option v-for="item in history" :key="item.run_id" :value="item.run_id">{{ new Date(item.created_at).toLocaleString() }} · {{ item.target_origin }}</option></select></label></div>
          <button type="button" class="mt-3 min-h-9 rounded-lg border border-slate-700 px-3 text-sm disabled:opacity-50" :disabled="!compareLeft || !compareRight || compareLeft === compareRight" @click="compareRuns">Compare</button>
          <template v-if="comparison">
            <p class="mt-3 text-sm" :class="comparison.equivalent ? 'text-emerald-200' : 'text-amber-200'">{{ comparison.equivalent ? 'Inputs are equivalent.' : `Non-equivalent inputs: ${comparison.differences.join(', ')}. Interpret differences with care.` }}</p>
            <div class="mt-3 overflow-x-auto rounded-lg border border-slate-800"><table class="w-full min-w-[34rem] text-left text-xs"><thead class="bg-slate-950/60 text-slate-400"><tr><th class="px-3 py-2">Route</th><th class="px-3 py-2">Earlier p95</th><th class="px-3 py-2">Later p95</th><th class="px-3 py-2">Earlier errors</th><th class="px-3 py-2">Later errors</th></tr></thead><tbody class="divide-y divide-slate-800"><tr v-for="route in [...new Set([...comparison.runs[0].route_results.map((item) => item.path), ...comparison.runs[1].route_results.map((item) => item.path)])]" :key="route"><td class="max-w-56 break-all px-3 py-2 font-mono">{{ route }}</td><td class="px-3 py-2">{{ fmt(comparison.runs[0].route_results.find((item) => item.path === route)?.latency_ms?.['p(95)']) }}</td><td class="px-3 py-2">{{ fmt(comparison.runs[1].route_results.find((item) => item.path === route)?.latency_ms?.['p(95)']) }}</td><td class="px-3 py-2">{{ comparison.runs[0].route_results.find((item) => item.path === route)?.errors ?? '—' }}</td><td class="px-3 py-2">{{ comparison.runs[1].route_results.find((item) => item.path === route)?.errors ?? '—' }}</td></tr></tbody></table></div>
          </template>
        </section>
      </div>
      <aside class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5">
        <div class="flex items-center justify-between gap-2"><div><h3 class="font-semibold">Benchmark history</h3><p class="mt-1 text-xs text-slate-500">{{ history.length }} saved runs</p></div><button class="text-xs text-indigo-200 hover:text-indigo-100" @click="loadHistory">Refresh</button></div>
        <div v-if="history.length" class="mt-4 max-h-[34rem] space-y-2 overflow-y-auto pr-1"><button v-for="item in history" :key="item.run_id" type="button" class="w-full rounded-xl border p-3 text-left transition" :class="selectedRun?.run_id === item.run_id ? 'border-indigo-400/50 bg-indigo-500/10' : 'border-slate-800 bg-slate-950/40 hover:border-slate-700'" @click="openRun(item.run_id)"><span class="block text-sm font-medium">{{ item.target_origin }}</span><span class="mt-1 block text-xs text-slate-400">{{ item.routes.length }} routes · {{ item.workload.virtual_users }} VUs · {{ item.workload.duration_seconds }}s</span><span class="mt-1 block text-[11px] text-slate-500">{{ new Date(item.created_at).toLocaleString() }}</span></button></div>
        <p v-else class="mt-4 text-sm leading-relaxed text-slate-500">Completed runs are saved here with their target, routes, workload, authentication mode, and runner version.</p>
      </aside>
    </section>
  </section>
</template>
