<script setup lang="ts">
/**
 * Regression check between a base and the selected candidate revision
 * (@.archcore/regression/regression-check.spec.md): static evidence, then replay below it.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { makeRequest } from '../../ui/makeRequest';
import type { ProjectRevision } from '../../code-quality/projectRevisions';
import ReplayPanel from './ReplayPanel.vue';

type Verdict = 'unchanged' | 'changed_expected' | 'suspected_regression' | 'not_covered';
type Reason = { aspect: string; text: string; side: string; path: string; line: number | null };
type Evidence = { kind: string; label?: string; reason?: string; reasons?: Reason[]; model_verdict?: string };
type SymbolRow = { fqn: string; path: string; line: number | null; change: string; language?: string; verdict: Verdict; coverage?: string[]; evidence: Evidence[] };
type Difference = { where: string; path: string; base: unknown; candidate: unknown };
type RouteRow = { method: string; path: string; handler_path: string; handler_line: number | null; verdict: Verdict; coverage?: string[]; depends_on?: string[];
  replay?: { probes: number; differing: number; differences: { path: string; differences: Difference[] }[] }; spans?: { flags: string[]; p95_change_pct: number | null } };
type OtherRequest = { method: string; path: string; equal: boolean | null; verdict: Verdict; differences: Difference[] };
type Uncovered = { kind: string; name: string; path: string; line: number | null; reason: string };
type Partial = { capability: string; status: string; affects: string; reasons: string[] };
type ChecklistItem = { kind: string; name: string; detail: string; file: string; line: number | null; source: string };
type ContractChange = { severity: string; code: string; message: string; operation: string | null };
type Report = { tier: string; coverage: string; symbols: SymbolRow[]; routes: RouteRow[]; uncovered: Uncovered[]; partial: Partial[];
  baseline_checklist: ChecklistItem[]; contract_changes: ContractChange[]; notes: string[]; replay_other?: OtherRequest[] };
type Check = { check_id: string; base_revision_id: string; candidate_revision_id: string; state: string; error: string | null;
  created_at: string; tier?: string; counts: Record<Verdict, number>; uncovered: number; report?: Report };

const props = defineProps<{ projectId: string; candidateRevisionId: string; revisions: ProjectRevision[] }>();

const checks = ref<Check[]>([]);
const current = ref<Check | null>(null);
const base = ref('');
const busy = ref(false);
const error = ref('');
let timer: ReturnType<typeof setTimeout> | null = null;

const VERDICT_LABEL: Record<Verdict, string> = {
  unchanged: 'Unchanged', changed_expected: 'Changed (expected)', suspected_regression: 'Suspected regression', not_covered: 'Not covered',
};
const VERDICT_CLASS: Record<Verdict, string> = {
  unchanged: 'bg-emerald-500/15 text-emerald-200', changed_expected: 'bg-sky-500/15 text-sky-200',
  suspected_regression: 'bg-rose-500/15 text-rose-200', not_covered: 'bg-slate-500/20 text-slate-300',
};
const verdictOrder: Verdict[] = ['suspected_regression', 'not_covered', 'changed_expected', 'unchanged'];

const label = (revision: ProjectRevision) => `${revision.commit_sha ? revision.commit_sha.slice(0, 12) : revision.revision_id} · ${revision.updated_at ? new Date(revision.updated_at).toLocaleString() : ''}`;
const bases = computed(() => props.revisions.filter((r) => r.revision_id && r.revision_id !== props.candidateRevisionId
  && r.state === 'ready' && r.commit_sha));
const pairChecks = computed(() => checks.value.filter((c) => c.candidate_revision_id === props.candidateRevisionId));
const report = computed(() => current.value?.report ?? null);
const running = computed(() => current.value && ['pending', 'running'].includes(current.value.state));
const byVerdict = <T extends { verdict: Verdict }>(rows: T[]) =>
  [...rows].sort((a, b) => verdictOrder.indexOf(a.verdict) - verdictOrder.indexOf(b.verdict));
const reasonsOf = (row: SymbolRow) => row.evidence.flatMap((e) => e.reasons ?? []);
const judgment = (row: SymbolRow) => row.evidence.find((e) => e.label)?.label;
const skipped = (row: SymbolRow) => row.evidence.find((e) => e.reason)?.reason;
const at = (path: string, line: number | null) => (path ? `${path}${line ? `:${line}` : ''}` : '');
const show = (value: unknown) => (typeof value === 'string' ? value : JSON.stringify(value));
const downloading = ref(false);
async function downloadTests() {
  if (!current.value) return;
  downloading.value = true; error.value = '';
  try {
    const response = await makeRequest(`${root()}/${encodeURIComponent(current.value.check_id)}/tests?format=zip`, { method: 'POST' });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(typeof body.detail === 'string' ? body.detail : 'Tests could not be generated.');
    }
    const url = URL.createObjectURL(await response.blob());
    const link = Object.assign(document.createElement('a'), { href: url, download: `regression-tests-${current.value.check_id}.zip` });
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (cause: any) { error.value = cause.message; } finally { downloading.value = false; }
}
const differingOthers = computed(() => (report.value?.replay_other || []).filter((row) => row.equal === false));

async function request<T>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const { json, ...rest } = init || {};
  const response = await makeRequest(path, json === undefined ? rest : { ...rest, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(json) });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(new Error(typeof body.detail === 'string' ? body.detail : 'The regression check request failed.'), { body });
  return body as T;
}
const root = () => `/api/regression/projects/${encodeURIComponent(props.projectId)}/checks`;

async function loadList() {
  if (!props.projectId) { checks.value = []; return; }
  try { checks.value = (await request<{ checks: Check[] }>(root())).checks; } catch (cause: any) { error.value = cause.message; }
}
async function open(checkId: string) {
  if (timer) { clearTimeout(timer); timer = null; }
  try {
    current.value = await request<Check>(`${root()}/${encodeURIComponent(checkId)}`);
    if (running.value) timer = setTimeout(() => void open(checkId), 3000);
    else void loadList();
  } catch (cause: any) { error.value = cause.message; }
}
async function start() {
  error.value = '';
  if (!base.value) return;
  busy.value = true;
  try {
    const check = await request<Check>(root(), { method: 'POST', json: { base_revision_id: base.value, candidate_revision_id: props.candidateRevisionId } });
    await loadList();
    await open(check.check_id);
  } catch (cause: any) {
    error.value = cause.message;
    if (cause.body?.active_check_id) void open(cause.body.active_check_id);
  } finally { busy.value = false; }
}

watch(() => [props.projectId, props.candidateRevisionId] as const, async () => {
  current.value = null;
  base.value = bases.value[0]?.revision_id || '';
  await loadList();
  const latest = pairChecks.value[0];
  if (latest) void open(latest.check_id);
}, { immediate: true });
watch(bases, (list) => { if (!list.some((r) => r.revision_id === base.value)) base.value = list[0]?.revision_id || ''; });
onBeforeUnmount(() => { if (timer) clearTimeout(timer); });
</script>

<template>
  <section class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5 sm:p-6" aria-label="Regression check">
    <h3 class="text-lg font-semibold">Regression check</h3>
    <p class="mt-1 text-xs text-slate-500">
      Compares the selected revision with a base revision: what changed, which routes depend on it, whether each change keeps its behavior,
      and what no test or runtime data covers. The app reads code only; it never runs your project.
    </p>

    <div class="mt-4 flex flex-wrap items-end gap-3">
      <label class="min-w-64 flex-1 text-xs text-slate-400">Base revision (before the change)
        <select v-model="base" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm" :disabled="!bases.length">
          <option v-if="!bases.length" value="">No other indexed Git revision of this project</option>
          <option v-for="revision in bases" :key="revision.revision_id!" :value="revision.revision_id!">{{ label(revision) }}</option>
        </select>
      </label>
      <button type="button" class="min-h-10 rounded-lg bg-indigo-500 px-4 text-sm font-medium text-white disabled:opacity-50"
              :disabled="!base || !candidateRevisionId || busy || !!running" @click="start">
        {{ busy ? 'Starting…' : 'Check for regressions' }}
      </button>
      <label v-if="pairChecks.length" class="text-xs text-slate-400">Earlier checks
        <select class="mt-1.5 min-h-10 rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm" :value="current?.check_id" @change="open(($event.target as HTMLSelectElement).value)">
          <option v-for="check in pairChecks" :key="check.check_id" :value="check.check_id">{{ new Date(check.created_at).toLocaleString() }} · base {{ check.base_revision_id.slice(0, 12) }} · {{ check.state }}</option>
        </select>
      </label>
    </div>
    <p v-if="error" class="mt-3 text-sm text-rose-300" role="alert">{{ error }}</p>

    <p v-if="running" class="mt-4 text-sm text-slate-300" role="status">Comparing revisions and reviewing changed code…</p>
    <p v-else-if="current?.state === 'failed'" class="mt-4 text-sm text-rose-300" role="alert">The check failed: {{ current.error }}</p>

    <template v-if="report && current?.state === 'done'">
      <div class="mt-5 flex flex-wrap gap-2 text-xs" aria-label="Summary">
        <span v-for="verdict in verdictOrder" :key="verdict" class="rounded-full px-2.5 py-1" :class="VERDICT_CLASS[verdict]">
          {{ VERDICT_LABEL[verdict] }}: {{ current.counts[verdict] }}
        </span>
        <span class="rounded-full bg-slate-800 px-2.5 py-1 text-slate-300">Evidence: {{ report.tier }}</span>
        <span v-if="report.coverage !== 'full'" class="rounded-full bg-amber-500/15 px-2.5 py-1 text-amber-200">Impact coverage: {{ report.coverage }}</span>
      </div>
      <ul v-if="report.notes.length" class="mt-3 list-disc pl-5 text-xs text-amber-200"><li v-for="note in report.notes" :key="note">{{ note }}</li></ul>

      <h4 class="mt-6 text-sm font-semibold">Routes ({{ report.routes.length }})</h4>
      <div v-if="report.routes.length" class="mt-2 overflow-x-auto rounded-lg border border-slate-800">
        <table class="w-full min-w-[40rem] text-left text-xs">
          <thead class="bg-slate-950/60 text-slate-400"><tr><th class="px-3 py-2">Route</th><th class="px-3 py-2">Verdict</th><th class="px-3 py-2">Covered by</th><th class="px-3 py-2">Replay</th><th class="px-3 py-2">Handler</th><th class="px-3 py-2">Depends on</th></tr></thead>
          <tbody class="divide-y divide-slate-800">
            <tr v-for="route in byVerdict(report.routes)" :key="route.method + route.path">
              <td class="px-3 py-2 font-mono">{{ route.method }} {{ route.path }}</td>
              <td class="px-3 py-2"><span class="rounded-full px-2 py-0.5" :class="VERDICT_CLASS[route.verdict]">{{ VERDICT_LABEL[route.verdict] }}</span></td>
              <td class="px-3 py-2">{{ route.coverage?.length ? route.coverage.join(', ') : '—' }}</td>
              <td class="px-3 py-2">
                <template v-if="route.replay">{{ route.replay.differing ? `${route.replay.differing} of ${route.replay.probes} differ` : `${route.replay.probes} match` }}</template>
                <template v-else>—</template>
                <div v-if="route.spans?.flags?.length" class="text-amber-200">{{ route.spans.flags.join(', ').replace(/_/g, ' ') }}</div>
                <details v-if="route.replay?.differing" class="mt-1">
                  <summary class="cursor-pointer text-rose-200">Show differences</summary>
                  <div v-for="probe in route.replay.differences" :key="probe.path" class="mt-1">
                    <div class="font-mono text-slate-400">{{ probe.path }}</div>
                    <div v-for="diff in probe.differences" :key="diff.where + diff.path" class="font-mono">
                      {{ diff.where }} {{ diff.path }}: <span class="text-slate-400">{{ show(diff.base) }}</span> → <span class="text-rose-200">{{ show(diff.candidate) }}</span>
                    </div>
                  </div>
                </details>
              </td>
              <td class="px-3 py-2 font-mono text-slate-400">{{ at(route.handler_path, route.handler_line) }}</td>
              <td class="px-3 py-2 font-mono text-slate-400">{{ route.depends_on?.join(', ') }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="mt-2 text-xs text-slate-500">No route reaches the changed code, or routes could not be read for this project.</p>

      <h4 class="mt-6 text-sm font-semibold">Changed code ({{ report.symbols.length }})</h4>
      <ul class="mt-2 space-y-2">
        <li v-for="row in byVerdict(report.symbols)" :key="row.fqn + row.change" class="rounded-lg border border-slate-800 p-3 text-xs">
          <div class="flex flex-wrap items-center gap-2">
            <span class="rounded-full px-2 py-0.5" :class="VERDICT_CLASS[row.verdict]">{{ VERDICT_LABEL[row.verdict] }}</span>
            <span class="font-mono">{{ row.fqn }}</span>
            <span class="text-slate-500">{{ row.change }} · {{ at(row.path, row.line) }}</span>
          </div>
          <ul v-if="reasonsOf(row).length" class="mt-2 list-disc pl-5 text-slate-300">
            <li v-for="reason in reasonsOf(row)" :key="reason.text">{{ reason.text }} <span class="font-mono text-slate-500">({{ reason.side }} {{ at(reason.path, reason.line) }})</span></li>
          </ul>
          <p v-if="judgment(row)" class="mt-1 text-[11px] text-slate-500">Equivalence verdict: {{ judgment(row) }}.</p>
          <p v-else-if="skipped(row)" class="mt-1 text-[11px] text-slate-500">Not reviewed: {{ skipped(row)?.replace(/_/g, ' ') }}.</p>
        </li>
      </ul>

      <template v-if="report.uncovered.length">
        <h4 class="mt-6 text-sm font-semibold">Not covered by tests or runtime data ({{ report.uncovered.length }})</h4>
        <p class="mt-1 text-xs text-slate-500">Verify these by hand, add tests before changing them, or replay requests against both versions below.</p>
        <ul class="mt-2 list-disc pl-5 text-xs text-slate-300">
          <li v-for="item in report.uncovered" :key="item.kind + item.name"><span class="font-mono">{{ item.name }}</span> <span class="text-slate-500">{{ at(item.path, item.line) }}</span></li>
        </ul>
      </template>

      <template v-if="differingOthers.length">
        <h4 class="mt-6 text-sm font-semibold">Other replayed requests that differ ({{ differingOthers.length }})</h4>
        <ul class="mt-2 space-y-1 text-xs">
          <li v-for="row in differingOthers" :key="row.method + row.path"><span class="font-mono">{{ row.method }} {{ row.path }}</span>
            <span v-for="diff in row.differences.slice(0, 3)" :key="diff.where + diff.path" class="ml-2 font-mono text-slate-400">{{ diff.where }} {{ diff.path }}: {{ show(diff.base) }} → <span class="text-rose-200">{{ show(diff.candidate) }}</span></span>
          </li>
        </ul>
      </template>

      <template v-if="report.contract_changes.length">
        <h4 class="mt-6 text-sm font-semibold">API contract changes</h4>
        <ul class="mt-2 list-disc pl-5 text-xs"><li v-for="item in report.contract_changes" :key="item.code + item.operation" :class="item.severity === 'breaking' ? 'text-rose-200' : 'text-slate-300'">{{ item.message }}</li></ul>
      </template>

      <template v-if="report.partial.length">
        <h4 class="mt-6 text-sm font-semibold">Incomplete evidence</h4>
        <ul class="mt-2 list-disc pl-5 text-xs text-amber-200">
          <li v-for="item in report.partial" :key="item.capability">{{ item.capability.replace(/_/g, ' ') }}: {{ item.affects }}. <span class="text-slate-400">{{ item.reasons.join(' ') }}</span></li>
        </ul>
      </template>

      <details v-if="report.baseline_checklist.length" class="mt-6 rounded-lg border border-slate-800 p-3 text-xs">
        <summary class="cursor-pointer text-sm font-semibold">How to start the base version</summary>
        <p class="mt-2 text-slate-500">Start the base and candidate versions yourself; these steps come from the project's files.</p>
        <ul class="mt-2 space-y-1">
          <li v-for="item in report.baseline_checklist" :key="item.kind + item.name"><span class="text-slate-500">{{ item.kind }}</span> <span class="font-mono">{{ item.name }}</span> — {{ item.detail }} <span v-if="item.file" class="text-slate-500">({{ at(item.file, item.line) }})</span></li>
        </ul>
      </details>

      <section class="mt-6 rounded-xl border border-slate-800 p-4" aria-label="Characterization tests">
        <h4 class="text-sm font-semibold">Characterization tests</h4>
        <p class="mt-1 text-xs text-slate-500">
          Tests that pin what the base version returned in the recorded baseline, written for this project's test runner, plus a request
          file replay can import. Add them to the project, run them on the base version first (they must pass), then on the changed version.
          Tests for changed code without a route are written by the model and must be reviewed.
        </p>
        <button type="button" class="mt-3 min-h-10 rounded-lg border border-slate-700 px-4 text-sm disabled:opacity-50" :disabled="downloading" @click="downloadTests">
          {{ downloading ? 'Writing tests…' : 'Download tests (ZIP)' }}
        </button>
      </section>

      <ReplayPanel :project-id="projectId" :check-id="current.check_id" @report-changed="open(current.check_id)" />
    </template>
  </section>
</template>
