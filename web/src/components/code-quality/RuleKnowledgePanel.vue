<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { AlertTriangle, Check, ChevronDown, ChevronRight, ClipboardCopy, Download, ExternalLink, FlaskConical,
  GitCompare, LoaderCircle, Link2, RefreshCw, ShieldCheck, Sparkles, X } from 'lucide-vue-next';
import { makeRequest } from '../../ui/makeRequest';
import ImpactResults from './RuleImpactResults.vue';
import ToggleCard from '../ToggleCard.vue';
import { outlineHref, ruleKnowledgePath, ruleRequest, symbolLabel, syncedAgo, type ImpactResult, type RuleDocument,
  type RuleLink, type RuleLinkState, type RuleRuntime, type RuleSync } from './ruleKnowledge';

const props = defineProps<{ projectId: string; credentialSaved: boolean }>();
type Revision = { project_id: string; revision_id: string; commit_sha?: string; source_ref?: string; state?: string; updated_at?: string };
type TestResult = { doc_id: string; framework: string; group: string; code: string; scenarios: { name: string; given: string; when: string; then: string }[] };

const sync = ref<RuleSync | null>(null);
const documents = ref<RuleDocument[]>([]);
const instanceUrl = ref<string | null>(null);
const allowRemote = ref(false);
const revisions = ref<Revision[]>([]);
const revisionId = ref('');
const links = ref<RuleLink[]>([]);
const runtime = ref<Record<string, RuleRuntime>>({});
const runtimeNote = ref('');
const tested = ref<Record<string, boolean>>({});
const expanded = ref<string | null>(null);
const busy = ref('');
const error = ref('');
const notice = ref('');
const filter = ref<'all' | 'needs_review' | 'unlinked'>('all');
const diff = ref('');
const impact = ref<ImpactResult | null>(null);
const tests = ref<TestResult | null>(null);
let sequence = 0;

const linksByRule = computed(() => {
  const map: Record<string, RuleLink[]> = {};
  for (const link of links.value) (map[link.doc_id] ||= []).push(link);
  return map;
});
const reviewed = (docId: string) => (linksByRule.value[docId] || []).filter((l) => l.state === 'confirmed' || l.state === 'manual');
const suggested = (docId: string) => (linksByRule.value[docId] || []).filter((l) => l.state === 'suggested');
const needsReview = (docId: string) => reviewed(docId).some((l) => l.needs_review);
const visibleDocuments = computed(() => documents.value.filter((doc) => filter.value === 'all'
  || (filter.value === 'needs_review' && needsReview(doc.doc_id))
  || (filter.value === 'unlinked' && !reviewed(doc.doc_id).length)));
const reviewCount = computed(() => documents.value.filter((doc) => needsReview(doc.doc_id)).length);

const loadIndex = async () => {
  const data = await ruleRequest<{ sync: RuleSync; documents: RuleDocument[]; instance_url: string | null; settings: { allow_remote: boolean } }>(
    ruleKnowledgePath(props.projectId));
  sync.value = data.sync; documents.value = data.documents || []; instanceUrl.value = data.instance_url;
  allowRemote.value = data.settings?.allow_remote === true;
};
const loadLinks = async () => {
  const data = await ruleRequest<{ links: RuleLink[] }>(ruleKnowledgePath(props.projectId, '/links'));
  links.value = data.links || [];
};
const loadRevisions = async () => {
  const response = await makeRequest('/api/code-quality/projects');
  const data = await response.json().catch(() => ({}));
  const rows: Revision[] = Array.isArray(data.projects) ? data.projects : [];
  revisions.value = rows.filter((row) => row.project_id === props.projectId && row.state === 'ready')
    .sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
  if (!revisions.value.some((row) => row.revision_id === revisionId.value)) revisionId.value = revisions.value[0]?.revision_id || '';
};
const loadRevisionData = async () => {
  if (!revisionId.value || !documents.value.length) { runtime.value = {}; tested.value = {}; return; }
  const query = `?revision_id=${encodeURIComponent(revisionId.value)}`;
  const [coverage, health] = await Promise.allSettled([
    ruleRequest<{ tested: Record<string, boolean> }>(ruleKnowledgePath(props.projectId, '/coverage' + query)),
    ruleRequest<{ rules: RuleRuntime[]; available: boolean; reason?: string }>(ruleKnowledgePath(props.projectId, '/runtime' + query)),
  ]);
  tested.value = coverage.status === 'fulfilled' ? coverage.value.tested || {} : {};
  if (health.status === 'fulfilled') {
    runtime.value = Object.fromEntries((health.value.rules || []).map((rule) => [rule.doc_id, rule]));
    runtimeNote.value = health.value.available ? '' : health.value.reason || 'No runtime telemetry for this revision.';
  } else { runtime.value = {}; runtimeNote.value = 'Runtime telemetry is unavailable.'; }
  if (coverage.status === 'fulfilled') await loadLinks(); // coverage refreshes code-drift flags
};
const load = async () => {
  const current = ++sequence;
  error.value = ''; notice.value = ''; impact.value = null; tests.value = null;
  if (!props.projectId) return;
  busy.value = 'load';
  try {
    await Promise.all([loadIndex(), loadLinks(), loadRevisions()]);
    if (current !== sequence) return;
    await loadRevisionData();
  } catch (cause: any) { if (current === sequence) error.value = cause?.message || 'Business rules could not be loaded.'; }
  finally { if (current === sequence) busy.value = ''; }
};

const run = async (key: string, action: () => Promise<void>) => {
  busy.value = key; error.value = ''; notice.value = '';
  try { await action(); } catch (cause: any) { error.value = cause?.message || 'The action failed.'; }
  finally { busy.value = ''; }
};
const syncNow = () => run('sync', async () => {
  const data = await ruleRequest<{ sync: RuleSync; events: { kind: string }[] }>(
    `/api/code-quality/projects/${encodeURIComponent(props.projectId)}/outline/sync`, { method: 'POST' });
  const changes = data.events?.length || 0;
  notice.value = changes ? `Synced: ${changes} document${changes === 1 ? '' : 's'} added, changed or removed.` : 'Synced: no changes since the last sync.';
  await loadIndex(); await loadLinks();
});
const toggleRemote = () => run('settings', async () => {
  const data = await ruleRequest<{ settings: { allow_remote: boolean } }>(ruleKnowledgePath(props.projectId, '/settings'),
    { method: 'PUT', json: { allow_remote: !allowRemote.value } });
  allowRemote.value = data.settings.allow_remote;
});
const suggest = (docIds?: string[]) => run('suggest', async () => {
  const data = await ruleRequest<{ rules_searched: number; rules_with_matches: number; suggestions_written: number }>(
    ruleKnowledgePath(props.projectId, '/links/suggest'), { method: 'POST', json: { revision_id: revisionId.value, ...(docIds ? { doc_ids: docIds } : {}) } });
  notice.value = `Searched the code for ${data.rules_searched} rule${data.rules_searched === 1 ? '' : 's'}; ${data.suggestions_written} new suggestion${data.suggestions_written === 1 ? '' : 's'}. Confirm the ones that really implement the rule.`;
  await loadLinks();
});
const setState = (link: RuleLink, state: RuleLinkState) => run(`link:${link.doc_id}:${link.symbol_id}`, async () => {
  await ruleRequest(ruleKnowledgePath(props.projectId, '/links'), {
    method: 'PUT', json: { doc_id: link.doc_id, symbol_id: link.symbol_id, state, revision_id: revisionId.value || undefined } });
  await loadLinks();
});
const manualSymbol = ref('');
const addManual = (docId: string) => run('manual', async () => {
  await ruleRequest(ruleKnowledgePath(props.projectId, '/links'), {
    method: 'PUT', json: { doc_id: docId, symbol_id: manualSymbol.value.trim(), state: 'manual', revision_id: revisionId.value || undefined } });
  manualSymbol.value = '';
  await loadLinks();
});
const checkDiff = () => run('impact', async () => {
  impact.value = await ruleRequest<ImpactResult>(ruleKnowledgePath(props.projectId, '/impact'),
    { method: 'POST', json: { revision_id: revisionId.value, diff: diff.value } });
});
const generateTests = (docId: string) => run(`tests:${docId}`, async () => {
  tests.value = await ruleRequest<TestResult>(ruleKnowledgePath(props.projectId, `/${encodeURIComponent(docId)}/tests`),
    { method: 'POST', json: { revision_id: revisionId.value } });
});
const copyTests = async () => { if (tests.value) { await navigator.clipboard.writeText(tests.value.code); notice.value = 'Test skeleton copied.'; } };
const downloadTests = () => {
  if (!tests.value) return;
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([tests.value.code], { type: 'text/x-php;charset=utf-8' }));
  link.download = `${tests.value.group}Test.php`;
  link.click(); URL.revokeObjectURL(link.href);
};
const titleOf = (docId: string) => documents.value.find((doc) => doc.doc_id === docId)?.title || docId;
const production = (docId: string) => runtime.value[docId];

watch(() => [props.projectId, props.credentialSaved], () => void load(), { immediate: true });
watch(revisionId, (value, previous) => { if (previous && value !== previous) void run('revision', loadRevisionData); });
</script>

<template>
  <section class="space-y-4 rounded-2xl border border-slate-800 bg-slate-900/65 p-5 sm:p-6" aria-labelledby="rule-knowledge-title">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 id="rule-knowledge-title" class="text-lg font-semibold">Rules in the code</h2>
        <p class="mt-1 max-w-3xl text-sm text-slate-400">A synced, read-only copy of the collection: link each rule to the code that implements it, then see it in chat, diff checks, runtime and tests.</p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <span class="text-xs" :class="sync?.status === 'unavailable' ? 'text-amber-200' : 'text-slate-400'" role="status">
          {{ sync?.status === 'unavailable' ? `Last sync failed: ${sync.reason}` : syncedAgo(sync?.synced_at) }}<template v-if="sync?.document_count"> · {{ sync.document_count }} rules</template>
        </span>
        <button type="button" :disabled="!!busy || !credentialSaved" class="inline-flex min-h-9 items-center gap-2 rounded-lg border border-slate-700 px-3 text-sm text-slate-200 hover:bg-slate-800 disabled:opacity-50" @click="syncNow">
          <LoaderCircle v-if="busy === 'sync'" :size="14" class="motion-safe:animate-spin" /><RefreshCw v-else :size="14" />Sync now
        </button>
      </div>
    </div>

    <div class="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto]">
      <label class="block text-sm text-slate-300">Code revision
        <select v-model="revisionId" :disabled="!revisions.length || !!busy" class="mt-1 min-h-9 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm">
          <option v-if="!revisions.length" value="">No indexed revision for this project</option>
          <option v-for="item in revisions" :key="item.revision_id" :value="item.revision_id">{{ (item.commit_sha || item.source_ref || item.revision_id).slice(0, 12) }} · {{ item.updated_at ? new Date(item.updated_at).toLocaleString() : 'ready' }}</option>
        </select>
      </label>
      <ToggleCard
        class="self-end"
        density="compact"
        :model-value="allowRemote"
        :disabled="!!busy"
        label="Allow rule text to go to the remote model"
        @update:model-value="toggleRemote"
      >
        <template #icon><ShieldCheck :size="13" aria-hidden="true" /></template>
      </ToggleCard>
    </div>
    <p v-if="!allowRemote" class="-mt-2 text-xs text-slate-500">Rule text stays with the local model and local embeddings. Chat answers using the remote model will not include rules.</p>

    <div v-if="busy === 'load'" class="flex items-center gap-2 text-sm text-slate-400"><LoaderCircle :size="15" class="motion-safe:animate-spin" />Loading rules…</div>
    <p v-else-if="!documents.length" class="rounded-lg border border-slate-800 p-3 text-sm text-slate-400">No synced rules yet. Press <strong>Sync now</strong> to copy the collection.</p>
    <template v-else>
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div role="radiogroup" aria-label="Filter rules" class="inline-flex rounded-lg border border-slate-800 p-0.5 text-xs">
          <button v-for="option in (['all', 'needs_review', 'unlinked'] as const)" :key="option" type="button" role="radio" :aria-checked="filter === option" class="min-h-7 rounded-md px-2.5" :class="filter === option ? 'bg-slate-800 text-slate-100' : 'text-slate-400'" @click="filter = option">
            {{ option === 'all' ? 'All' : option === 'needs_review' ? `Needs review (${reviewCount})` : 'No confirmed code' }}
          </button>
        </div>
        <button type="button" :disabled="!!busy || !revisionId" class="inline-flex min-h-9 items-center gap-2 rounded-lg bg-indigo-500 px-3 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-50" @click="suggest()">
          <LoaderCircle v-if="busy === 'suggest'" :size="14" class="motion-safe:animate-spin" /><Sparkles v-else :size="14" />Suggest code links
        </button>
      </div>
      <p v-if="runtimeNote" class="text-xs text-slate-500">{{ runtimeNote }}</p>
      <div class="overflow-x-auto rounded-xl border border-slate-800">
        <table class="w-full min-w-[44rem] text-left text-sm">
          <thead class="bg-slate-950/60 text-xs uppercase tracking-wide text-slate-500">
            <tr><th class="px-3 py-2">Rule</th><th class="px-3 py-2">Code</th><th class="px-3 py-2">Production</th><th class="px-3 py-2">Tests</th></tr>
          </thead>
          <tbody>
            <template v-for="doc in visibleDocuments" :key="doc.doc_id">
              <tr class="border-t border-slate-800 align-top">
                <td class="px-3 py-2">
                  <button type="button" class="inline-flex items-center gap-1 text-left font-medium text-slate-100 hover:text-indigo-200" :aria-expanded="expanded === doc.doc_id" @click="expanded = expanded === doc.doc_id ? null : doc.doc_id">
                    <ChevronDown v-if="expanded === doc.doc_id" :size="14" /><ChevronRight v-else :size="14" />{{ doc.title }}
                  </button>
                  <a :href="outlineHref(instanceUrl, doc.url)" target="_blank" rel="noopener noreferrer" class="ml-1 inline-flex align-middle text-slate-500 hover:text-indigo-200" :aria-label="`Open ${doc.title} in Outline`"><ExternalLink :size="12" /></a>
                </td>
                <td class="px-3 py-2 text-xs text-slate-300">
                  <span>{{ reviewed(doc.doc_id).length }} confirmed</span><span v-if="suggested(doc.doc_id).length" class="text-slate-500"> · {{ suggested(doc.doc_id).length }} suggested</span>
                  <span v-if="needsReview(doc.doc_id)" class="ml-1 inline-flex items-center gap-1 rounded-full bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-200"><AlertTriangle :size="10" />needs review</span>
                </td>
                <td class="px-3 py-2 text-xs">
                  <template v-if="production(doc.doc_id)">
                    <span v-if="!production(doc.doc_id).linked_symbols" class="text-slate-500">—</span>
                    <span v-else-if="production(doc.doc_id).never_seen" class="rounded-full bg-slate-700/50 px-1.5 py-0.5 text-slate-300">never executed</span>
                    <span v-else class="text-slate-300">{{ production(doc.doc_id).calls.toLocaleString() }} calls · {{ (production(doc.doc_id).error_rate * 100).toFixed(1) }}% errors<template v-if="production(doc.doc_id).p95_ms"> · p95 {{ Math.round(production(doc.doc_id).p95_ms!) }} ms</template>
                      <span v-if="production(doc.doc_id).high_error_rate" class="ml-1 rounded-full bg-rose-500/10 px-1.5 py-0.5 text-rose-200">high error rate</span></span>
                  </template>
                  <span v-else class="text-slate-500">—</span>
                </td>
                <td class="px-3 py-2 text-xs">
                  <span v-if="tested[doc.doc_id]" class="rounded-full bg-emerald-500/10 px-1.5 py-0.5 text-emerald-200">tested</span>
                  <span v-else class="text-slate-500">no tagged test</span>
                </td>
              </tr>
              <tr v-if="expanded === doc.doc_id" class="border-t border-slate-800/60 bg-slate-950/30">
                <td colspan="4" class="px-4 py-3">
                  <div class="flex flex-wrap items-center gap-2">
                    <button type="button" :disabled="!!busy || !revisionId" class="inline-flex min-h-8 items-center gap-1.5 rounded-lg border border-slate-700 px-2.5 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-50" @click="suggest([doc.doc_id])"><Sparkles :size="12" />Suggest for this rule</button>
                    <button type="button" :disabled="!!busy || !revisionId" class="inline-flex min-h-8 items-center gap-1.5 rounded-lg border border-slate-700 px-2.5 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-50" @click="generateTests(doc.doc_id)">
                      <LoaderCircle v-if="busy === `tests:${doc.doc_id}`" :size="12" class="motion-safe:animate-spin" /><FlaskConical v-else :size="12" />Generate test skeleton
                    </button>
                    <form class="ml-auto flex items-center gap-1.5" @submit.prevent="addManual(doc.doc_id)">
                      <label class="sr-only" :for="`manual-${doc.doc_id}`">Symbol, route or table ID</label>
                      <input :id="`manual-${doc.doc_id}`" v-model="manualSymbol" maxlength="512" placeholder="App\Http\Controllers\OrderController::store" class="min-h-8 w-72 rounded-lg border border-slate-700 bg-slate-950 px-2 font-mono text-xs" />
                      <button type="submit" :disabled="!!busy || !manualSymbol.trim()" class="inline-flex min-h-8 items-center gap-1 rounded-lg border border-slate-700 px-2.5 text-xs text-slate-300 disabled:opacity-50"><Link2 :size="12" />Link</button>
                    </form>
                  </div>
                  <p v-if="!(linksByRule[doc.doc_id] || []).some((l) => l.state !== 'rejected')" class="mt-3 text-xs text-slate-500">No implementation found yet. Suggest links, or link a method, <code>route:GET /path</code> or <code>table:connection.name</code> by hand.</p>
                  <ul v-else class="mt-3 space-y-1.5">
                    <li v-for="link in (linksByRule[doc.doc_id] || []).filter((l) => l.state !== 'rejected')" :key="link.symbol_id" class="flex flex-wrap items-center gap-2 text-xs">
                      <code class="text-slate-200">{{ symbolLabel(link.symbol_id) }}</code>
                      <span v-if="link.file" class="text-slate-500">{{ link.file }}<template v-if="link.line">:{{ link.line }}</template></span>
                      <span class="rounded-full px-1.5 py-0.5 text-[10px]" :class="link.state === 'suggested' ? 'bg-slate-700/60 text-slate-300' : 'bg-emerald-500/10 text-emerald-200'">{{ link.state }}</span>
                      <span v-if="link.rule_changed" class="rounded-full bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-200">rule changed since confirmed</span>
                      <span v-if="link.code_changed" class="rounded-full bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-200">code changed since confirmed</span>
                      <span class="ml-auto inline-flex gap-1">
                        <button v-if="link.state === 'suggested' || link.needs_review" type="button" :disabled="!!busy" class="inline-flex min-h-6 items-center gap-1 rounded border border-emerald-500/30 px-1.5 text-[11px] text-emerald-200 hover:bg-emerald-500/10 disabled:opacity-50" @click="setState(link, 'confirmed')"><Check :size="11" />{{ link.needs_review ? 'Still holds' : 'Confirm' }}</button>
                        <button type="button" :disabled="!!busy" class="inline-flex min-h-6 items-center gap-1 rounded border border-slate-700 px-1.5 text-[11px] text-slate-400 hover:bg-slate-800 disabled:opacity-50" @click="setState(link, 'rejected')"><X :size="11" />Not related</button>
                      </span>
                    </li>
                  </ul>
                  <div v-if="tests && tests.doc_id === doc.doc_id" class="mt-3 rounded-lg border border-slate-800 bg-slate-950/70 p-3">
                    <div class="flex flex-wrap items-center justify-between gap-2">
                      <p class="text-xs text-slate-400">{{ tests.framework === 'pest' ? 'Pest' : 'PHPUnit' }} skeleton · group <code>{{ tests.group }}</code> · review it, then add it to your test suite. Nothing was written to the repository.</p>
                      <span class="inline-flex gap-1">
                        <button type="button" class="inline-flex min-h-7 items-center gap-1 rounded border border-slate-700 px-2 text-xs text-slate-300" @click="copyTests"><ClipboardCopy :size="12" />Copy</button>
                        <button type="button" class="inline-flex min-h-7 items-center gap-1 rounded border border-slate-700 px-2 text-xs text-slate-300" @click="downloadTests"><Download :size="12" />Download</button>
                      </span>
                    </div>
                    <ul v-if="tests.scenarios.length" class="mt-2 space-y-1 text-xs text-slate-300">
                      <li v-for="scenario in tests.scenarios" :key="scenario.name"><strong>{{ scenario.name }}:</strong> given {{ scenario.given }}, when {{ scenario.when }}, then {{ scenario.then }}.</li>
                    </ul>
                    <pre class="mt-2 max-h-80 overflow-auto rounded bg-slate-950 p-2 text-[11px] leading-relaxed text-slate-200"><code>{{ tests.code }}</code></pre>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>

      <section class="rounded-xl border border-slate-800 p-4" aria-labelledby="rule-impact-title">
        <h3 id="rule-impact-title" class="flex items-center gap-2 text-sm font-semibold text-slate-200"><GitCompare :size="15" />Check a change against the rules</h3>
        <p class="mt-1 text-xs text-slate-500">Paste the output of <code>git diff</code>. The changed lines are matched to methods in the selected revision, and only rules linked to those methods are checked.</p>
        <label class="sr-only" for="rule-impact-diff">Unified diff</label>
        <textarea id="rule-impact-diff" v-model="diff" rows="6" spellcheck="false" class="mt-2 w-full rounded-lg border border-slate-700 bg-slate-950 p-2 font-mono text-xs" placeholder="diff --git a/app/... b/app/..." />
        <button type="button" :disabled="!!busy || !revisionId || !diff.trim()" class="mt-2 inline-flex min-h-9 items-center gap-2 rounded-lg border border-slate-700 px-3 text-sm text-slate-200 hover:bg-slate-800 disabled:opacity-50" @click="checkDiff">
          <LoaderCircle v-if="busy === 'impact'" :size="14" class="motion-safe:animate-spin" /><GitCompare v-else :size="14" />Check change
        </button>
        <ImpactResults v-if="impact" class="mt-3" :result="impact" :instance-url="instanceUrl" />
      </section>
    </template>

    <p v-if="error" role="alert" class="rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-200">{{ error }}</p>
    <p v-if="notice" role="status" class="rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-3 text-sm text-emerald-200">{{ notice }}</p>
  </section>
</template>
