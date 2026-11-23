<script setup lang="ts">
/**
 * Replay for one regression check (@.archcore/regression/regression-check.spec.md clauses 10-18):
 * choose saved targets for the base and candidate versions, edit the probe set, record a baseline,
 * replay the candidate, and see differing responses. The developer runs both versions; the app
 * only sends HTTP to them.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { makeRequest } from '../../ui/makeRequest';

type Probe = { probe_id?: string; method: string; path: string; template?: string; body?: string | null; source?: string; note?: string };
type Settings = { baseline_target_id?: string; candidate_target_id?: string; disposable?: boolean; reset_path?: string | null; ignore?: string[] };
type Run = { run_id: string; kind: 'baseline' | 'candidate'; state: string; error: string | null; created_at: string;
  summary: { sent?: number; skipped?: { method: string; path: string; reason: string }[]; target_origin?: string } };
type Target = { target_id: string; name: string; origin: string; auth_mode: string; unassigned?: boolean };
type Unfilled = { route: string; params: string[]; reason: string };

const props = defineProps<{ projectId: string; checkId: string }>();
const emit = defineEmits<{ reportChanged: [] }>();

const targets = ref<Target[]>([]);
const probes = ref<Probe[]>([]);
const settings = ref<Settings>({ disposable: false, reset_path: '', ignore: [] });
const ignoreText = ref('');
const runs = ref<Run[]>([]);
const unfilled = ref<Unfilled[]>([]);
const busy = ref('');
const error = ref('');
const notice = ref('');
const dirty = ref(false);
let timer: ReturnType<typeof setTimeout> | null = null;

const METHODS = ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'];
const base = () => `/api/regression/projects/${encodeURIComponent(props.projectId)}/checks/${encodeURIComponent(props.checkId)}`;
const usable = computed(() => targets.value.filter((t) => !t.unassigned && t.auth_mode !== 'http_login'));
const running = computed(() => runs.value.find((run) => run.state === 'running'));
const hasBaseline = computed(() => runs.value.some((run) => run.kind === 'baseline' && run.state === 'completed'));
const writes = computed(() => probes.value.filter((p) => !['GET', 'HEAD', 'OPTIONS'].includes(p.method)).length);

async function request<T>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const { json, ...rest } = init || {};
  const response = await makeRequest(path, json === undefined ? rest : { ...rest, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(json) });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'The replay request failed.');
  return body as T;
}

async function load() {
  error.value = '';
  try {
    const [state, list] = await Promise.all([
      request<{ probes: Probe[]; settings: Settings; runs: Run[] }>(`${base()}/replay`),
      request<{ targets: Target[] }>(`/api/code-quality/projects/${encodeURIComponent(props.projectId)}/performance/targets`),
    ]);
    targets.value = list.targets;
    if (!dirty.value) probes.value = state.probes;
    settings.value = { disposable: false, reset_path: '', ignore: [], ...state.settings };
    ignoreText.value = (state.settings.ignore || []).join('\n');
    const wasRunning = Boolean(running.value);
    runs.value = state.runs;
    if (running.value) timer = setTimeout(() => void load(), 2000);
    else if (wasRunning) emit('reportChanged');
  } catch (cause: any) { error.value = cause.message; }
}

async function act(label: string, work: () => Promise<void>) {
  busy.value = label; error.value = ''; notice.value = '';
  try { await work(); } catch (cause: any) { error.value = cause.message; } finally { busy.value = ''; }
}
const saveSettings = () => act('settings', async () => {
  const ignore = ignoreText.value.split('\n').map((line) => line.trim()).filter(Boolean);
  settings.value = (await request<{ settings: Settings }>(`${base()}/replay-settings`, { method: 'PUT', json: { ...settings.value, ignore } })).settings;
  notice.value = 'Replay settings saved.';
});
const saveProbes = () => act('probes', async () => {
  probes.value = (await request<{ probes: Probe[] }>(`${base()}/probes`, { method: 'PUT', json: { probes: probes.value } })).probes;
  dirty.value = false;
  notice.value = `${probes.value.length} probes saved.`;
});
const generate = () => act('generate', async () => {
  const result = await request<{ probes: Probe[]; unfilled: Unfilled[] }>(`${base()}/probes/generate`, { method: 'POST' });
  probes.value = [...probes.value, ...result.probes];
  unfilled.value = result.unfilled;
  dirty.value = true;
  notice.value = `${result.probes.length} probes proposed from the changed routes. Review them, then save.`;
});
async function importFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  (event.target as HTMLInputElement).value = '';
  if (!file) return;
  if (!settings.value.baseline_target_id) { error.value = 'Choose the baseline target first: imported requests must match its origin.'; return; }
  await act('import', async () => {
    const result = await request<{ probes: Probe[]; imported: number; skipped: Record<string, number> }>(`${base()}/probes/import`,
      { method: 'POST', json: { text: await file.text(), target_id: settings.value.baseline_target_id, mode: 'append' } });
    probes.value = result.probes;
    dirty.value = false;
    const skipped = Object.entries(result.skipped).filter(([, n]) => n).map(([why, n]) => `${n} ${why.replace(/_/g, ' ')}`);
    notice.value = `Imported ${result.imported} requests${skipped.length ? `; skipped ${skipped.join(', ')}` : ''}.`;
  });
}
const startRun = (kind: 'baseline' | 'candidate') => act(kind, async () => {
  if (dirty.value) throw new Error('Save the probes before replaying.');
  await request(`${base()}/replay-runs`, { method: 'POST', json: { kind } });
  await load();
});
const addProbe = () => { probes.value.push({ method: 'GET', path: '/' }); dirty.value = true; };
const removeProbe = (index: number) => { probes.value.splice(index, 1); dirty.value = true; };

watch(() => [props.projectId, props.checkId], () => { dirty.value = false; unfilled.value = []; void load(); }, { immediate: true });
onBeforeUnmount(() => { if (timer) clearTimeout(timer); });
</script>

<template>
  <section class="mt-6 rounded-xl border border-slate-800 p-4" aria-label="Replay">
    <h4 class="text-sm font-semibold">Replay requests against both versions</h4>
    <p class="mt-1 text-xs text-slate-500">
      Start the old version and record a baseline, then start the new version and replay the same requests.
      Targets are your saved Performance targets (public or Bearer token). The app only sends HTTP; it never starts your project.
    </p>
    <p v-if="error" class="mt-2 text-sm text-rose-300" role="alert">{{ error }}</p>
    <p v-if="notice" class="mt-2 text-sm text-emerald-200" role="status">{{ notice }}</p>

    <div class="mt-3 grid gap-3 sm:grid-cols-2">
      <label class="text-xs text-slate-400">Baseline target (old version)
        <select v-model="settings.baseline_target_id" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm">
          <option value="">Choose a saved target</option>
          <option v-for="t in usable" :key="t.target_id" :value="t.target_id">{{ t.name }} · {{ t.origin }}</option>
        </select>
      </label>
      <label class="text-xs text-slate-400">Candidate target (new version)
        <select v-model="settings.candidate_target_id" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm">
          <option value="">Choose a saved target</option>
          <option v-for="t in usable" :key="t.target_id" :value="t.target_id">{{ t.name }} · {{ t.origin }}</option>
        </select>
      </label>
    </div>
    <p v-if="!usable.length" class="mt-2 text-xs text-amber-200">No usable target yet: add one under Code Quality → Performance (HTTP-login targets are not supported for replay).</p>
    <div class="mt-3 grid gap-3 sm:grid-cols-2">
      <label class="flex items-start gap-2 text-xs text-slate-300">
        <input v-model="settings.disposable" type="checkbox" class="mt-0.5" />
        <span>These targets are disposable: their data may be changed. Without this, only GET and HEAD requests are sent.</span>
      </label>
      <label class="text-xs text-slate-400">Reset path (optional, called with POST before each pass)
        <input v-model="settings.reset_path" :disabled="!settings.disposable" placeholder="/testing/reset" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm disabled:opacity-50" />
      </label>
    </div>
    <label class="mt-3 block text-xs text-slate-400">Ignore these response fields (one JSON path per line, e.g. $.data[*].updated_at or $..request_id)
      <textarea v-model="ignoreText" rows="2" class="mt-1.5 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-xs" />
    </label>
    <button type="button" class="mt-2 min-h-9 rounded-lg border border-slate-700 px-3 text-xs" :disabled="!!busy" @click="saveSettings">Save replay settings</button>

    <div class="mt-5 flex flex-wrap items-center gap-2">
      <h5 class="mr-auto text-sm font-semibold">Probes ({{ probes.length }}<span v-if="writes"> · {{ writes }} write{{ writes === 1 ? '' : 's' }}</span>)</h5>
      <button type="button" class="min-h-9 rounded-lg border border-slate-700 px-3 text-xs" :disabled="!!busy" @click="generate">{{ busy === 'generate' ? 'Proposing…' : 'Propose from changed routes' }}</button>
      <label class="min-h-9 cursor-pointer rounded-lg border border-slate-700 px-3 py-2 text-xs">Import HAR or request list<input type="file" accept=".har,.json,application/json" class="sr-only" @change="importFile" /></label>
      <button type="button" class="min-h-9 rounded-lg border border-slate-700 px-3 text-xs" @click="addProbe">Add probe</button>
      <button type="button" class="min-h-9 rounded-lg bg-slate-200 px-3 text-xs font-medium text-slate-900 disabled:opacity-50" :disabled="!!busy || !dirty" @click="saveProbes">Save probes</button>
    </div>
    <ul v-if="unfilled.length" class="mt-2 list-disc pl-5 text-xs text-amber-200">
      <li v-for="item in unfilled" :key="item.route">No values proposed for <span class="font-mono">{{ item.route }}</span>: {{ item.reason }}. Add a probe for it by hand.</li>
    </ul>
    <div v-if="probes.length" class="mt-2 overflow-x-auto rounded-lg border border-slate-800">
      <table class="w-full min-w-[40rem] text-left text-xs">
        <thead class="bg-slate-950/60 text-slate-400"><tr><th class="px-2 py-2">Method</th><th class="px-2 py-2">Path</th><th class="px-2 py-2">Body</th><th class="px-2 py-2">Source</th><th></th></tr></thead>
        <tbody class="divide-y divide-slate-800">
          <tr v-for="(probe, index) in probes" :key="probe.probe_id || index">
            <td class="px-2 py-1.5"><select v-model="probe.method" class="rounded border border-slate-700 bg-slate-950 px-1 py-1" @change="dirty = true"><option v-for="m in METHODS" :key="m">{{ m }}</option></select></td>
            <td class="px-2 py-1.5"><input v-model="probe.path" class="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono" aria-label="Request path" @input="dirty = true" /></td>
            <td class="px-2 py-1.5"><input v-model="probe.body" :disabled="probe.method === 'GET' || probe.method === 'HEAD'" class="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono disabled:opacity-40" aria-label="Request body" @input="dirty = true" /></td>
            <td class="px-2 py-1.5 text-slate-500" :title="probe.note">{{ probe.source || 'manual' }}</td>
            <td class="px-2 py-1.5"><button type="button" class="text-rose-300" aria-label="Remove probe" @click="removeProbe(index)">×</button></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="mt-5 flex flex-wrap gap-2">
      <button type="button" class="min-h-10 rounded-lg bg-indigo-500 px-4 text-sm font-medium text-white disabled:opacity-50"
              :disabled="!!busy || !!running || !probes.length || !settings.baseline_target_id" @click="startRun('baseline')">
        {{ busy === 'baseline' ? 'Starting…' : '1. Record baseline on the old version' }}
      </button>
      <button type="button" class="min-h-10 rounded-lg bg-indigo-500 px-4 text-sm font-medium text-white disabled:opacity-50"
              :disabled="!!busy || !!running || !hasBaseline || !settings.candidate_target_id" @click="startRun('candidate')">
        {{ busy === 'candidate' ? 'Starting…' : '2. Replay on the new version' }}
      </button>
    </div>
    <p v-if="running" class="mt-2 text-sm text-slate-300" role="status">Sending requests to the {{ running.kind }} target…</p>
    <ul v-if="runs.length" class="mt-3 space-y-1 text-xs">
      <li v-for="run in runs.slice(0, 6)" :key="run.run_id" :class="run.state === 'failed' ? 'text-rose-300' : 'text-slate-400'">
        {{ new Date(run.created_at).toLocaleString() }} · {{ run.kind }} · {{ run.state }}
        <template v-if="run.state === 'completed'"> · {{ run.summary.sent }} sent to {{ run.summary.target_origin }}<span v-if="run.summary.skipped?.length">, {{ run.summary.skipped.length }} writes skipped</span></template>
        <template v-if="run.error"> · {{ run.error }}</template>
      </li>
    </ul>
  </section>
</template>
