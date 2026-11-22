<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { LoaderCircle, Play, Plus, Save, Trash2 } from 'lucide-vue-next';
import { makeRequest } from '../../ui/makeRequest';
import { toastMessage } from '../../composables/useToasts';

type RuleSet = { rule_id: string; title: string; yaml: string; enabled: boolean; updated_at: string; content_hash: string };
type Preview = { revision_id: string; commit_sha?: string; scanned_files: number; truncated?: boolean; reason?: string; findings: Array<{ rule: string; category: string; severity: string; file: string; line: number; message: string }> };
const props = defineProps<{ projectId: string; revisionId: string }>();

const rules = ref<RuleSet[]>([]);
const selectedId = ref('');
const title = ref('');
const ruleYaml = ref('rules:\n  - id: avoid-select-star\n    message: Avoid selecting unused database columns.\n    severity: WARNING\n    languages: [generic]\n    pattern: "SELECT * FROM ..."\n    metadata:\n      category: performance\n');
const loading = ref(false);
const saving = ref(false);
const previewing = ref(false);
const error = toastMessage('error');
const message = toastMessage('success');
const previewResult = ref<Preview | null>(null);
const selected = computed(() => rules.value.find((item) => item.rule_id === selectedId.value) || null);

const request = async (path: string, init?: RequestInit) => {
  const response = await makeRequest(path, init);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || 'Custom rule request failed.');
  return body;
};
const loadRules = async () => {
  loading.value = true; error.value = '';
  try {
    const data = await request('/api/code-quality/rules');
    rules.value = Array.isArray(data.rules) ? data.rules : [];
    if (selectedId.value && !rules.value.some((item) => item.rule_id === selectedId.value)) newRule();
  } catch (cause: any) { error.value = cause?.message || 'Custom rules could not be loaded. Retry.'; }
  finally { loading.value = false; }
};
const newRule = () => {
  selectedId.value = ''; title.value = ''; previewResult.value = null; message.value = '';
  ruleYaml.value = 'rules:\n  - id: my-rule\n    message: Explain what this rule detects.\n    severity: WARNING\n    languages: [python]\n    pattern: "..."\n    metadata:\n      category: reliability\n';
};
const selectRule = (item: RuleSet) => {
  selectedId.value = item.rule_id; title.value = item.title; ruleYaml.value = item.yaml;
  previewResult.value = null; message.value = ''; error.value = '';
};
const saveRule = async () => {
  saving.value = true; error.value = ''; message.value = ''; previewResult.value = null;
  try {
    const path = selectedId.value ? `/api/code-quality/rules/${encodeURIComponent(selectedId.value)}` : '/api/code-quality/rules';
    const result = await request(path, { method: selectedId.value ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: title.value, yaml: ruleYaml.value }) });
    await loadRules();
    if (result.rule) selectRule(result.rule);
    message.value = 'Rule set saved. Future assessments will include it while it is enabled.';
  } catch (cause: any) { error.value = cause?.message || 'Rule set could not be saved. Your edits are still here.'; }
  finally { saving.value = false; }
};
const toggleRule = async (item: RuleSet) => {
  error.value = ''; message.value = '';
  try {
    await request(`/api/code-quality/rules/${encodeURIComponent(item.rule_id)}/enabled`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !item.enabled }) });
    await loadRules();
    message.value = `${item.title} ${item.enabled ? 'disabled' : 'enabled'}.`;
  } catch (cause: any) { error.value = cause?.message || 'Rule state could not be changed.'; }
};
const deleteRule = async () => {
  if (!selectedId.value) return;
  if (!window.confirm(`Delete “${title.value}”? Saved assessments keep their immutable rule snapshots.`)) return;
  saving.value = true; error.value = ''; message.value = '';
  try {
    await request(`/api/code-quality/rules/${encodeURIComponent(selectedId.value)}`, { method: 'DELETE' });
    newRule(); await loadRules(); message.value = 'Rule set deleted. Existing reports retain their saved rule snapshot.';
  } catch (cause: any) { error.value = cause?.message || 'Rule set could not be deleted.'; }
  finally { saving.value = false; }
};
const previewRule = async () => {
  if (!props.projectId || !props.revisionId) return;
  previewing.value = true; error.value = ''; message.value = ''; previewResult.value = null;
  try {
    previewResult.value = await request(`/api/code-quality/projects/${encodeURIComponent(props.projectId)}/revisions/${encodeURIComponent(props.revisionId)}/rules/preview`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ yaml: ruleYaml.value }),
    });
    message.value = 'Preview finished without saving an assessment report.';
  } catch (cause: any) { error.value = cause?.message || 'Rule preview could not complete. Check the rule and selected revision.'; }
  finally { previewing.value = false; }
};
onMounted(loadRules);
watch(() => [props.projectId, props.revisionId], () => { previewResult.value = null; });
</script>

<template>
  <section class="space-y-5" aria-label="Custom analyzer rules">
    <div class="rounded-2xl border border-slate-800 bg-slate-900/65 p-5 sm:p-6">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div><h2 class="text-xl font-semibold">Custom analyzer rules</h2><p class="mt-1 max-w-3xl text-sm leading-relaxed text-slate-400">Write offline Semgrep YAML rules for supported languages. Enabled rules run during assessment; previews scan the selected immutable revision and do not create a report.</p></div>
        <span class="rounded-full border border-indigo-400/20 bg-indigo-400/5 px-3 py-1 text-xs text-indigo-200">Built-in rules are read-only</span>
      </div>
      <div class="mt-5 grid gap-5 xl:grid-cols-[minmax(15rem,0.8fr)_minmax(0,1.5fr)]">
        <aside class="space-y-3">
          <div class="flex items-center justify-between gap-2"><h3 class="font-semibold">Your rule sets</h3><button type="button" class="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-slate-700 px-3 text-sm hover:bg-slate-800" @click="newRule"><Plus :size="15" />New</button></div>
          <div v-if="loading" class="flex items-center gap-2 text-sm text-slate-400"><LoaderCircle class="motion-safe:animate-spin" :size="16" />Loading rules…</div>
          <div v-else-if="rules.length" class="max-h-96 space-y-2 overflow-y-auto pr-1">
            <article v-for="item in rules" :key="item.rule_id" class="rounded-xl border p-3" :class="selectedId === item.rule_id ? 'border-indigo-400/50 bg-indigo-500/5' : 'border-slate-800 bg-slate-950/40'">
              <button type="button" class="w-full text-left" @click="selectRule(item)"><span class="block text-sm font-medium text-slate-100">{{ item.title }}</span><span class="mt-1 block text-[11px] text-slate-500">Updated {{ new Date(item.updated_at).toLocaleString() }}</span></button>
              <button type="button" class="mt-2 min-h-8 rounded-md border px-2.5 text-xs" :class="item.enabled ? 'border-emerald-500/30 text-emerald-200' : 'border-slate-700 text-slate-400'" :aria-label="`${item.enabled ? 'Disable' : 'Enable'} ${item.title}`" @click="toggleRule(item)">{{ item.enabled ? 'Enabled' : 'Disabled' }}</button>
            </article>
          </div>
          <p v-else-if="!loading" class="rounded-xl border border-dashed border-slate-700 p-4 text-sm text-slate-400">No custom rule sets yet. Add a rule to tailor static checks to your codebase.</p>
          <p class="text-xs leading-relaxed text-slate-500">Allowed rule fields: id, message, severity, languages, pattern, patterns, pattern-either, and metadata.category. Rules are limited to 16 KB per set and 100 sets per user.</p>
        </aside>
        <div class="space-y-4">
          <label class="block text-sm font-medium text-slate-300">Rule set name<input v-model="title" maxlength="100" class="mt-1.5 min-h-10 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 text-sm text-slate-100" placeholder="Database query checks" /></label>
          <label class="block text-sm font-medium text-slate-300">Semgrep rule YAML<textarea v-model="ruleYaml" spellcheck="false" rows="16" class="mt-1.5 w-full resize-y rounded-lg border border-slate-700 bg-slate-950 p-3 font-mono text-xs leading-relaxed text-slate-100" aria-describedby="rule-yaml-help" /></label>
          <p id="rule-yaml-help" class="text-xs leading-relaxed text-slate-500">Add a top-level <code>rules</code> list. Each rule needs one pattern form and <code>metadata.category</code> set to security, clean_code, maintainability, reliability, or performance. External rule downloads and commands are not allowed.</p>
          <div class="flex flex-wrap gap-2">
            <button type="button" :disabled="saving || previewing || !title.trim()" class="inline-flex min-h-10 items-center gap-2 rounded-lg bg-indigo-500 px-4 text-sm font-semibold text-white hover:bg-indigo-400 disabled:cursor-not-allowed disabled:opacity-50" @click="saveRule"><Save :size="15" />{{ saving ? 'Saving…' : 'Save rule set' }}</button>
            <button type="button" :disabled="saving || previewing || !projectId || !revisionId" class="inline-flex min-h-10 items-center gap-2 rounded-lg border border-slate-700 px-4 text-sm text-slate-200 disabled:cursor-not-allowed disabled:opacity-50" @click="previewRule"><LoaderCircle v-if="previewing" class="motion-safe:animate-spin" :size="15" /><Play v-else :size="15" />{{ previewing ? 'Previewing…' : 'Preview on selected revision' }}</button>
            <button v-if="selectedId" type="button" :disabled="saving || previewing" class="inline-flex min-h-10 items-center gap-2 rounded-lg border border-rose-500/30 px-3 text-sm text-rose-200 disabled:opacity-50" @click="deleteRule"><Trash2 :size="14" />Delete rule set</button>
          </div>
          <section v-if="previewResult" class="rounded-xl border border-slate-800 bg-slate-950/40 p-4" aria-label="Rule preview results">
            <div class="flex flex-wrap items-center justify-between gap-2"><h3 class="font-semibold">Preview results</h3><span class="text-xs text-slate-500">{{ previewResult.scanned_files }} files scanned · {{ previewResult.findings.length }} findings</span></div>
            <p v-if="previewResult.reason" class="mt-3 text-sm text-slate-400">{{ previewResult.reason }}</p>
            <div v-else-if="previewResult.findings.length" class="mt-3 max-h-72 space-y-2 overflow-y-auto pr-1">
              <article v-for="(finding, index) in previewResult.findings" :key="`${finding.file}:${finding.line}:${index}`" class="rounded-lg border border-slate-800 px-3 py-2.5"><p class="text-sm text-slate-200">{{ finding.message }}</p><p class="mt-1 font-mono text-xs text-slate-500">{{ finding.file }}:{{ finding.line }} · {{ finding.category }} · {{ finding.severity }}</p></article>
            </div>
            <p v-else class="mt-3 text-sm text-slate-400">No matches in this revision. A preview with zero findings does not prove the rule detects every issue.</p>
            <p v-if="previewResult.truncated" class="mt-2 text-xs text-amber-300">Showing the first 100 findings.</p>
          </section>
        </div>
      </div>
    </div>
  </section>
</template>
